"""Execution history, read from the state store: a page of executions, or one execution with its runs. Reading never writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from draw_things_control.core.errors import DtcError, NotFoundError, StateUnavailableError
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.run_lock import SERVER_HOLDER_NAME, lock_holder, lock_holder_message, run_lock_is_free
from draw_things_control.jobs.events import JobStatus
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.executions import ExecutionRow
from draw_things_control.state.ids import execution_id_text
from draw_things_control.state.store import Store

PAGE_SIZE = 200
STATUSES = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.INTERRUPTED, JobStatus.RUNNING)
NO_HISTORY = "No execution history yet"

T = TypeVar("T")


@dataclass(frozen=True)
class HistoryFilter:
    """The filters of a history read, each optional: a status, and text the job name or job file name contains."""

    status: str | None = None
    name: str | None = None

    def text(self) -> str:
        """The filter in words; empty with no filter."""
        parts = [f"status {self.status}" if self.status else "", f"name {self.name}" if self.name else ""]
        return ", ".join(part for part in parts if part)


@dataclass(frozen=True)
class HistoryPage:
    """Executions (each with its count of successful runs) from ``offset``; ``complete`` when no more follow."""

    offset: int
    rows: list[ExecutionRow] = field(default_factory=list)
    complete: bool = True
    # Why there is nothing to show: no history yet, or no execution matches the filter.
    message: str | None = None


class HistoryReader:
    """Reads the state store for a history view, from worker threads.

    A store is opened when the database first exists and kept. It is opened without pruning, since pruning writes; the job
    worker prunes when it opens its own store. A run that no process is running any more reads as interrupted: it changes
    the display only, never the database. Failures raise a DtcError (StateUnavailableError, or NotFoundError).
    """

    def __init__(self, paths: ProjectPaths, store: StoreProvider) -> None:
        self._paths = paths
        self._store = store

    def page(self, history_filter: HistoryFilter, offset: int, limit: int = PAGE_SIZE) -> HistoryPage:
        """Up to ``limit`` executions from ``offset``, newest first, each with its count of successful runs."""
        try:
            rows = self._read(lambda store: store.executions.page(limit=limit, offset=offset, status=history_filter.status, name_contains=history_filter.name, running_as_interrupted=self.lock_is_free()))
        except NotFoundError as error:
            return HistoryPage(offset, message=str(error))
        message = None if rows or offset else ("No executions match the filter" if history_filter.text() else NO_HISTORY)
        return HistoryPage(offset, rows, len(rows) < limit, message)

    def rows(self, execution_ids: list[int]) -> list[ExecutionRow]:
        """The executions with these row ids, each with its count of successful runs, to update rows already shown."""
        return self._read(lambda store: store.executions.by_ids(execution_ids, running_as_interrupted=self.lock_is_free()))

    def newest_id(self, history_filter: HistoryFilter) -> int | None:
        """The row id of the newest execution the filter shows, or None when it shows none."""
        rows = self._read(lambda store: store.executions.page(limit=1, status=history_filter.status, name_contains=history_filter.name, running_as_interrupted=self.lock_is_free()))
        return rows[0].id if rows else None

    def execution(self, execution_id: int) -> ExecutionRow:
        """One execution with its runs, by its row id; raises NotFoundError when it is no longer in the history."""
        execution = self._read(lambda store: store.executions.get(execution_id, running_as_interrupted=self.lock_is_free()))
        if execution is None:
            raise NotFoundError("That execution is no longer in the history")
        return execution

    def numbered(self, number: int) -> ExecutionRow:
        """One execution with its runs, by the number of its ID (E0012); raises NotFoundError when there is none."""

        def read(store: Store) -> ExecutionRow | None:
            row = store.executions.row_of(number)
            return store.executions.get(row, running_as_interrupted=self.lock_is_free()) if row is not None else None

        execution = self._read(read)
        if execution is None:
            raise NotFoundError(f"No execution {execution_id_text(number)}")
        return execution

    def lock_is_free(self) -> bool:
        """Whether no process holds the run lock, so nothing is running."""
        return run_lock_is_free(directory=self._paths.state)

    def lock_message(self) -> str | None:
        """The server's own busy message when it is the run lock's holder, read without taking the lock; None
        otherwise (including a free lock), so an ordinary ``run-job`` or TUI holder keeps the generic wording."""
        if lock_holder(directory=self._paths.state) != SERVER_HOLDER_NAME:
            return None
        return lock_holder_message(directory=self._paths.state)

    def _read(self, read: Callable[[Store], T]) -> T:
        try:
            store = self._store.get(create=False)
            if store is None:
                raise NotFoundError(NO_HISTORY)
            return read(store)
        except DtcError:
            raise
        except Exception as error:
            # Not only sqlite3.Error: a bad JSON column or an out-of-range number would otherwise end a worker, and the app.
            raise StateUnavailableError(f"Cannot read the state database {self._paths.database}: {error}") from error
