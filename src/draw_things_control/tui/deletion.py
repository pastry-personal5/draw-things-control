"""Deleting executions from the TUI (Milestone 06): ``d`` on the Execution History widget and ``/delete``. The
selection is read from the store in full, checked with a dry run of ``POST /v1/executions/delete`` (the server's own
refusals and resume warnings, and proof it can be reached, before any dialog opens), then asked about one execution
at a time and deleted as the person confirms."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.text import Text

from draw_things_control.services.history import NO_HISTORY, HistoryFilter
from draw_things_control.state.executions import ExecutionRow
from draw_things_control.state.ids import execution_id_text
from draw_things_control.tui.delete_dialog import DeleteDialog
from draw_things_control.tui.queue_client import ApiError, delete_executions
from draw_things_control.tui.text.history import delete_summary

if TYPE_CHECKING:
    from draw_things_control.tui.app import DrawThingsApp

# The most IDs one request may name (the server's MAX_LIMIT).
DELETE_BATCH = 200


@dataclass(frozen=True)
class DeleteSelection:
    """What to delete: the executions ``numbers`` names, or, given ``history_filter``, every execution it shows (an
    empty filter: the whole history)."""

    numbers: tuple[int, ...] = ()
    history_filter: HistoryFilter | None = None


@dataclass
class _Outcome:
    """The answers of the requests sent so far, merged."""

    deleted: list[str] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    resumes_ended: dict[str, list[str]] = field(default_factory=dict)
    manifests_kept: list[dict[str, Any]] = field(default_factory=list)

    def add(self, body: dict[str, Any]) -> None:
        self.deleted += body["deleted"]
        self.refused += body["refused"]
        self.missing += body["missing"]
        self.resumes_ended.update({row["execution_id"]: row["queue_ids"] for row in body["resumes_ended"]})
        self.manifests_kept += body["manifests_kept"]


class DeleteFlow:
    """One deletion, from reading the selection to the Messages that report it."""

    def __init__(self, app: DrawThingsApp) -> None:
        self.app = app

    def say(self, text: Text | str, style: str = "") -> None:
        self.app.say(text, style)

    async def run(self, selection: DeleteSelection) -> None:
        main = self.app.main
        if main is None or main.reader is None:
            return
        rows = await self._read(selection)
        if rows is None:
            return
        try:
            plan = await self._send([row.label for row in rows], _Outcome(), dry_run=True)
        except ApiError as error:
            self.say(error.text, "red")
            return
        # Left out before the dialog opens, so it never offers one it cannot delete.
        self._say_not_deleted(plan)
        planned = set(plan.deleted)
        deletable = [row for row in rows if row.label in planned]
        if not deletable:
            return
        done = _Outcome()
        try:
            await self._ask_and_delete(deletable, plan.resumes_ended, done)
        except ApiError as error:
            self.say(error.text, "red")
            self.say(f"{_count(len(done.deleted))} deleted before it", "yellow")
            # What the requests before it answered still stands: a manifest that stayed would come back on an import.
            self._say_consequences(done)
        else:
            self._report(done)
        if done.deleted:
            gone = {row.label: row.execution_number for row in deletable}
            main.history.remove_rows([gone[label] for label in done.deleted if label in gone])

    async def _read(self, selection: DeleteSelection) -> list[ExecutionRow] | None:
        """The executions selected, newest first, read in full before anything is deleted; None, having said why, when
        there are none."""
        main = self.app.main
        assert main is not None and main.reader is not None
        if selection.history_filter is not None:
            rows = await asyncio.to_thread(main.reader.every, selection.history_filter)
        else:
            rows = await asyncio.to_thread(main.reader.by_numbers, list(selection.numbers))
        if isinstance(rows, str):
            self.say(rows, "red")
            return None
        found = {row.execution_number for row in rows}
        for number in selection.numbers:
            if number not in found:
                self.say(f"No execution {execution_id_text(number)}", "yellow")
        if not rows and selection.history_filter is not None:
            self.say("No executions match the filter" if selection.history_filter.text() else NO_HISTORY, "yellow")
        return rows or None

    async def _ask_and_delete(self, rows: list[ExecutionRow], ended: dict[str, list[str]], done: _Outcome) -> None:
        """The dialog for each execution in history order, each deletion sent as it is confirmed: one request per
        Delete, and batches for Delete all."""
        for index, row in enumerate(rows):
            later = rows[index + 1 :]
            dialog = DeleteDialog(delete_summary(row), place=(index + 1, len(rows)), resumes_ended=ended.get(row.label, []), remaining_ending=sum(1 for other in later if other.label in ended))
            choice = await self.app.push_screen_wait(dialog)
            if choice == "cancel":
                return
            if choice == "skip":
                continue
            if choice == "all":
                await self._send([other.label for other in (row, *later)], done)
                return
            if choice != "delete":
                # Anything but the four choices (a dialog dismissed some other way) deletes nothing.
                return
            await self._send([row.label], done)

    async def _send(self, execution_ids: list[str], outcome: _Outcome, *, dry_run: bool = False) -> _Outcome:
        """Send ``execution_ids``, up to ``DELETE_BATCH`` per request, adding each answer to ``outcome`` as it comes, so
        a request that fails leaves the ones before it counted."""
        for start in range(0, len(execution_ids), DELETE_BATCH):
            outcome.add(await delete_executions(self.app.server_url, self.app.token_file, self.app.http_transport, execution_ids[start : start + DELETE_BATCH], dry_run=dry_run))
        return outcome

    def _say_not_deleted(self, outcome: _Outcome) -> None:
        for row in outcome.refused:
            self.say(f"Not deleted: {row['reason']}", "yellow")
        for execution_id in outcome.missing:
            self.say(f"No execution {execution_id}", "yellow")

    def _report(self, done: _Outcome) -> None:
        """How many were deleted, the entries that can no longer be resumed, the manifests that stayed, and those the
        server refused while the dialog was open."""
        self.say(f"Deleted {_count(len(done.deleted))}" if done.deleted else "Nothing was deleted")
        self._say_consequences(done)

    def _say_consequences(self, done: _Outcome) -> None:
        """The entries the deletions sent so far leave unresumable, the manifests that stayed, and those the server
        refused while the dialog was open."""
        for queue_id in sorted({queue_id for queue_ids in done.resumes_ended.values() for queue_id in queue_ids}):
            self.say(f"{queue_id} can no longer be resumed", "yellow")
        for row in done.manifests_kept:
            self.say(f"{row['execution_id']}'s manifest could not be deleted: {row['path']}; `dtc import-history` would bring it back under a new number", "yellow")
        self._say_not_deleted(done)


def _count(number: int) -> str:
    return f"{number} execution{'s' if number != 1 else ''}"
