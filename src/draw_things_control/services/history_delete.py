"""Deleting executions from the history (Milestone 06): which ones a queued or running entry still uses, which resumes
a deletion ends, and the deletion itself, checked and done under one lock."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field

from draw_things_control.services.queue_resume import RESUMABLE_STATES
from draw_things_control.services.resume_chain import walk_chain
from draw_things_control.state.ids import execution_id_text
from draw_things_control.state.queue import QueueState, ResumeLink
from draw_things_control.state.store import Store

_ACTIVE_STATES = (QueueState.QUEUED, QueueState.RUNNING)


@dataclass(frozen=True)
class DeleteReport:
    """What a deletion did, or, for a dry run, would do now: the executions deleted, those refused with the reason,
    the numbers no execution has, the entries each deletion leaves unresumable, and the manifests that could not be
    deleted (never any in a dry run), as (execution number, path)."""

    deleted: list[int] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)
    missing: list[int] = field(default_factory=list)
    resumes_ended: dict[int, list[str]] = field(default_factory=dict)
    manifests_kept: list[tuple[int, str]] = field(default_factory=list)


class _ChainBroken(Exception):
    """The walk reached an entry that is gone, or a finished entry whose execution is gone: a resume refuses there."""


def delete_executions(store: Store, numbers: Sequence[int], *, dry_run: bool = False, lock: AbstractContextManager[object] | None = None) -> DeleteReport:
    """Delete the executions ``numbers`` names, refusing a running or in-use one; ``dry_run`` reports what a deletion
    would do now and deletes nothing. The check and the transaction run under ``lock`` (the worker's, so no claim,
    job start, or submission lands between them); the logs and manifests are deleted after it is released."""
    with lock if lock is not None else nullcontext():
        links = store.queue.resume_links()
        deletion = store.executions.delete(numbers, in_use=_in_use(links), dry_run=dry_run)
        deleted = [execution.number for execution in deletion.deleted]
        ended = _ended(links, set(deleted))
    kept = [] if dry_run else store.delete_execution_files(deletion.deleted)
    return DeleteReport(deleted, deletion.refused, deletion.missing, ended, kept)


def _in_use(links: list[ResumeLink]) -> dict[int, str]:
    """Each execution a ``queued`` or ``running`` entry uses, with the reason naming that entry: its own execution, the
    execution it resumes, and each one its resume chain reads between the two."""
    by_number = {link.queue_number: link for link in links}
    in_use: dict[int, str] = {}
    for link in links:
        if link.state not in _ACTIVE_STATES:
            continue
        if link.execution_number is not None:
            in_use.setdefault(link.execution_number, f"{link.label} is {link.state} with {execution_id_text(link.execution_number)}")
        if link.resumes_execution is not None:
            in_use.setdefault(link.resumes_execution, _resume_reason(link, "from", link.resumes_execution))
        for number in _chain(link, by_number)[0]:
            in_use.setdefault(number, _resume_reason(link, "through", number))
    return in_use


def _resume_reason(link: ResumeLink, word: str, number: int) -> str:
    doing = "is queued to resume" if link.state == QueueState.QUEUED else "is running a resume"
    return f"{link.label} {doing} {word} {execution_id_text(number)}"


def _ended(links: list[ResumeLink], numbers: set[int]) -> dict[int, list[str]]:
    """The entries (Q0007) each of ``numbers`` would leave unresumable if it were deleted: each entry a resume would
    accept now whose chain reads it. The output file a resume would start from is not checked."""
    by_number = {link.queue_number: link for link in links}
    resumed = {link.resumes for link in links if link.resumes is not None}
    ended: dict[int, list[str]] = {}
    for link in links:
        if link.state not in RESUMABLE_STATES or link.queue_number in resumed:
            continue
        read, point = _chain(link, by_number)
        if point is None or point.last_succeeded is None:
            continue
        if point.execution_total_runs is not None and point.last_succeeded >= point.execution_total_runs:
            continue
        for number in read:
            if number in numbers:
                ended.setdefault(number, []).append(link.label)
    return ended


def _chain(entry: ResumeLink, by_number: dict[int, ResumeLink]) -> tuple[list[int], ResumeLink | None]:
    """The executions ``entry``'s chain reads, as ``walk_chain`` walks it, and the entry whose execution the walk ended
    at; None when the chain is broken (an ancestor, or a finished entry's execution, is gone) or has no execution."""

    def linked(link: ResumeLink) -> ResumeLink | None:
        if link.execution_number is None:
            return None
        if not link.execution_exists:
            # A running entry links its number before JobStarted creates the row; a finished one's row was deleted.
            if link.state in _ACTIVE_STATES:
                return None
            raise _ChainBroken
        return link

    def ancestor(link: ResumeLink) -> ResumeLink:
        found = by_number.get(link.resumes) if link.resumes is not None else None
        if found is None:
            raise _ChainBroken
        return found

    read: list[int] = []
    last: ResumeLink | None = None
    try:
        for _entry, execution in walk_chain(entry, linked=linked, ancestor=ancestor, succeeded=lambda link: link.last_succeeded is not None):
            if execution is not None and execution.execution_number is not None:
                read.append(execution.execution_number)
                last = execution
    except _ChainBroken:
        return read, None
    return read, last
