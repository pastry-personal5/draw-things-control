"""Resolve and accept a resume: a new queue entry that continues an interrupted, failed, or cancelled one from its
last succeeded run, with the original seed and run numbering."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.errors import InputError, NotFoundError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.services.queue_submit import parse_snapshot
from draw_things_control.state.execution_rows import ExecutionRow
from draw_things_control.state.ids import execution_id_text
from draw_things_control.state.queue import NewQueueEntry, QueueRow, QueueState
from draw_things_control.state.store import Store

RESUMABLE_STATES = (QueueState.INTERRUPTED, QueueState.FAILED, QueueState.CANCELLED)


class ResumeRefusedError(InputError):
    """A resume was refused, naming the reason. Not a ``NotFoundError``: the entry exists, so this is not a 404 for a
    front end that maps error codes to statuses (Milestone 02); it is invalid to resume it, right now, for the
    reason given."""

    code = "invalid_state"


@dataclass(frozen=True)
class ResumeChain:
    """The last succeeded run of a resumed entry's chain, walked back through its own resumes."""

    first_run: int
    input: Path
    seed: int
    # The execution number this resume point's last succeeded run came from, shown as "the execution it resumes" on
    # JobStarted, the manifest, and the resumed execution's own row (never the same as ``entry.execution_number``
    # once the chain has been walked back past an ancestor that never itself succeeded).
    execution_number: int


def resume_entry(store: Store, entry_id: int, global_config: GlobalConfig, params_directory: Path, *, clock: Clock = datetime.now, before_submit: Callable[[JobDefinition, int], None] | None = None) -> QueueRow:
    """Resolve and accept a resume of the entry ``entry_id``; returns the new ``queued`` entry, at the back of the
    FIFO queue like any submission. Raises ``ResumeRefusedError``, naming the reason, when it cannot be resumed.

    ``before_submit``, when given, is called with the resumed job and the runs it has left (Milestone 02's own API
    rules and limits, which count only what a resume still has to do, not the whole chain) after the resume point is
    resolved but before anything is stored; it raising refuses the resume and stores nothing.
    """
    entry = store.queue.get(entry_id)
    if entry is None:
        raise NotFoundError(f"No queue entry {entry_id}")
    if entry.state not in RESUMABLE_STATES:
        raise ResumeRefusedError(f"{entry.label} cannot be resumed: it is {entry.state}")
    if _resumed_by_some_entry(store, entry.queue_number):
        raise ResumeRefusedError(f"{entry.label} already has a resume; resume the newest one instead")
    _check_own_input(entry, global_config, params_directory)
    chain = _resolve_chain(store, entry)
    if before_submit is not None:
        job = parse_snapshot(entry, global_config, params_directory)()
        before_submit(job, job.run_count - chain.first_run + 1)
    new = NewQueueEntry(
        job_path=entry.job_path,
        job_text=entry.job_text,
        config_file=entry.config_file,
        config_text=entry.config_text,
        input_directory=entry.input_directory,
        output_directory=entry.output_directory,
        cooldown_default=entry.cooldown_default,
        settings=entry.settings,
        submitted_at=local_timestamp(clock()),
        resumes=entry.queue_number,
        resumes_execution=chain.execution_number,
        resume_first_run=chain.first_run,
        resume_input=str(chain.input),
        resume_seed=chain.seed,
    )
    return store.queue.submit(new)


@dataclass(frozen=True)
class ResumePreview:
    """Whether an entry can be resumed right now, without accepting one: from which run, or why not
    (``GET /queue/{id}``, Milestone 02)."""

    resumable: bool
    from_run: int | None = None
    reason: str | None = None


def preview_resume(store: Store, entry: QueueRow, global_config: GlobalConfig, params_directory: Path) -> ResumePreview:
    """The same checks ``resume_entry`` makes, without accepting a resume or storing anything, so the reason given
    here always matches what an actual resume attempt would raise."""
    if entry.state not in RESUMABLE_STATES:
        return ResumePreview(False, reason=f"{entry.label} cannot be resumed: it is {entry.state}")
    if _resumed_by_some_entry(store, entry.queue_number):
        return ResumePreview(False, reason=f"{entry.label} already has a resume; resume the newest one instead")
    try:
        _check_own_input(entry, global_config, params_directory)
        chain = _resolve_chain(store, entry)
    except ResumeRefusedError as error:
        return ResumePreview(False, reason=str(error))
    return ResumePreview(True, from_run=chain.first_run)


def _resumed_by_some_entry(store: Store, queue_number: int) -> bool:
    """Whether some entry already resumes ``queue_number``: an entry can be resumed once."""
    return any(candidate.resumes == queue_number for candidate in store.queue.list())


def _check_own_input(entry: QueueRow, global_config: GlobalConfig, params_directory: Path) -> None:
    """Refuse, naming the path, when the job's own first input is gone: parsing the snapshot resolves the job's size
    from it, exactly as the first submission did."""
    try:
        parse_snapshot(entry, global_config, params_directory)()
    except InputError as error:
        raise ResumeRefusedError(f"{entry.label}'s job cannot be resolved: {error}") from error


def _resolve_chain(store: Store, entry: QueueRow) -> ResumeChain:
    """Walk back through ``entry``'s own resumes to the last succeeded run of the chain; refuses, naming the reason,
    when no run of the chain ever succeeded, or the file it would start from is gone."""
    current = entry
    execution = _linked_execution(store, current)
    succeeded = execution.succeeded_runs if execution is not None else ()
    while not succeeded and current.resumes is not None:
        ancestor = store.queue.by_number(current.resumes)
        if ancestor is None:
            raise ResumeRefusedError(f"{current.label}'s ancestor Q{current.resumes:04d} is gone; it cannot be resumed")
        current = ancestor
        execution = _linked_execution(store, current)
        succeeded = execution.succeeded_runs if execution is not None else ()
    if not succeeded:
        raise ResumeRefusedError(f"{entry.label} has no succeeded run in its chain; submit the job again instead of resuming it")
    last = succeeded[-1]
    assert execution is not None
    if execution.total_runs is not None and last.number >= execution.total_runs:
        raise ResumeRefusedError(f"{entry.label}: the chain already finished all {execution.total_runs} runs; nothing to resume")
    input_path = execution.run_file(last.last_frame or last.output)
    if input_path is None or not input_path.is_file():
        raise ResumeRefusedError(f"{entry.label}: run {last.number}'s output is gone: {input_path or '(no file)'}")
    return ResumeChain(first_run=last.number + 1, input=input_path, seed=execution.seed or 0, execution_number=execution.execution_number)


def _linked_execution(store: Store, entry: QueueRow) -> ExecutionRow | None:
    """The entry's linked execution, with its runs. None when it never started (no runs to resume from): either it
    truly has no link, or a number was reserved for it but the row was never created (the job failed before
    ``JobStarted``, which ``QueueWorker._fail_to_start`` clears the link for) -- both read the same way here, since
    neither has a run to resume from. A linked execution whose row once existed and was since pruned by retention is
    refused instead, naming it, since that chain did run and is not simply resumable from further back."""
    if entry.execution_number is None:
        return None
    execution = store.executions.by_number(entry.execution_number)
    if execution is None:
        raise ResumeRefusedError(f"{entry.label}'s execution {execution_id_text(entry.execution_number)} was pruned; it cannot be resumed")
    return execution
