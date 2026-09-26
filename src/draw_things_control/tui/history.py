"""Execution history read from the state store for the history pane and the execution detail; reading never writes."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from draw_things_control.core.run_lock import run_lock_is_free, state_directory
from draw_things_control.state.ids import MAX_DIGITS, execution_id_text
from draw_things_control.state.store import DATABASE_FILE_NAME, StateError, Store

PAGE_SIZE = 200
STATUSES = ("succeeded", "failed", "interrupted", "running")


@dataclass(frozen=True)
class HistoryFilter:
    """The history pane's filters, each optional: a status, and text the job name or job file name contains."""

    status: str | None = None
    name: str | None = None

    def text(self) -> str:
        """The filter as the pane's title shows it; empty with no filter."""
        parts = [f"status {self.status}" if self.status else "", f"name {self.name}" if self.name else ""]
        return ", ".join(part for part in parts if part)


@dataclass(frozen=True)
class HistoryPage:
    """Executions (with ``succeeded`` added) from ``offset``; ``complete`` when no more follow."""

    offset: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    # Why the store could not be read, or why there is nothing to read.
    message: str | None = None


def database_path() -> Path:
    return state_directory() / DATABASE_FILE_NAME


NO_HISTORY = "No execution history yet"
# The largest ID SQLite can hold; a larger one cannot name an execution.
MAX_ID = 2**63 - 1

T = TypeVar("T")


class HistoryReader:
    """Reads the state store for the history pane and the detail, from worker threads.

    One Store is opened when the database first exists and kept, so each worker thread reuses its connection. It is
    opened without pruning, since pruning writes; the TUI's job worker prunes when it opens its own store. No method
    raises: a failed Textual worker closes the app, and with it any running job, so every error becomes a message.
    """

    def __init__(self, retention_days: int) -> None:
        self._retention_days = retention_days
        self._store: Store | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            store, self._store = self._store, None
        if store is not None:
            store.close()

    def page(self, history_filter: HistoryFilter, offset: int, limit: int = PAGE_SIZE) -> HistoryPage:
        """Up to ``limit`` executions from ``offset``, newest first, each with ``succeeded``."""
        rows = self._read(lambda store: self._with_counts(store, store.list_executions(limit=limit, offset=offset, status=history_filter.status, name_contains=history_filter.name, running_as_interrupted=run_lock_is_free())))
        if isinstance(rows, str):
            return HistoryPage(offset, message=rows)
        message = None if rows or offset else ("No executions match the filter" if history_filter.text() else NO_HISTORY)
        return HistoryPage(offset, rows, len(rows) < limit, message)

    def rows(self, execution_ids: list[int]) -> list[dict[str, Any]] | str:
        """The executions with these IDs, each with ``succeeded``, to update rows already shown."""
        return self._read(lambda store: self._with_counts(store, store.executions_by_id(execution_ids, running_as_interrupted=run_lock_is_free())))

    def newest_id(self, history_filter: HistoryFilter) -> int | None | str:
        """The ID of the newest execution the filter shows, or None when it shows none."""
        rows = self._read(lambda store: store.list_executions(limit=1, status=history_filter.status, name_contains=history_filter.name, running_as_interrupted=run_lock_is_free()))
        return rows if isinstance(rows, str) else (rows[0]["id"] if rows else None)

    def execution(self, execution_id: int) -> dict[str, Any] | str:
        """One execution with its runs, by its row in the store, or why it cannot be shown."""
        execution = self._read(lambda store: store.get_execution(execution_id, running_as_interrupted=run_lock_is_free()))
        return execution if execution is not None else "That execution is no longer in the history"

    def numbered(self, number: int) -> dict[str, Any] | str:
        """One execution with its runs, by the number of its ID (E0012), or why it cannot be shown."""

        def read(store: Store) -> dict[str, Any] | None:
            row = store.execution_row(number)
            return store.get_execution(row, running_as_interrupted=run_lock_is_free()) if row is not None else None

        execution = self._read(read)
        return execution if execution is not None else f"No execution {execution_id_text(number)}"

    @staticmethod
    def _with_counts(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        succeeded = store.succeeded_runs([row["id"] for row in rows])
        for row in rows:
            row["succeeded"] = succeeded.get(row["id"], 0)
        return rows

    def _read(self, read: Callable[[Store], T]) -> T | str:
        try:
            store = self._open()
            if store is None:
                return NO_HISTORY
            return read(store)
        except StateError as error:
            return str(error)
        except Exception as error:
            # Not only sqlite3.Error: a bad JSON column or an out-of-range number would otherwise end the worker, and the app.
            return f"Cannot read the state database {database_path()}: {error}"

    def _open(self) -> Store | None:
        """The store, opened on first use; None while the database does not exist, since browsing must not create it."""
        with self._lock:
            if self._store is None:
                path = database_path()
                if not path.exists():
                    return None
                self._store = Store(path, retention_days=self._retention_days, prune_on_open=False)
            return self._store


def execution_label(execution: dict[str, Any]) -> str:
    """The execution's ID as people see it (E0012); the store's row id stays internal."""
    return execution_id_text(int(execution["execution_number"]))


def parse_id(text: str) -> int | None:
    """An execution or run number as typed, or None when it is not one: ASCII digits only, and small enough for SQLite."""
    if not (text.isascii() and text.isdecimal()):
        return None
    # int() raises on thousands of digits; nothing that long names a run.
    digits = text.lstrip("0") or "0"
    if len(digits) > MAX_DIGITS:
        return None
    number = int(digits)
    return number if 0 < number <= MAX_ID else None


def output_directory(execution: dict[str, Any]) -> Path | None:
    """Where the execution's outputs are: as recorded, or beside its manifest for an imported one."""
    recorded = execution.get("settings", {}).get("output_directory")
    if recorded:
        return Path(recorded)
    manifest = execution.get("manifest_path")
    return Path(manifest).parent if manifest else None


def run_file(execution: dict[str, Any], name: str | None) -> Path | None:
    """The full path of a run's output or last frame, which the store keeps as a file name."""
    if not name:
        return None
    if Path(name).is_absolute():
        return Path(name)
    directory = output_directory(execution)
    return directory / name if directory is not None else None


def is_imported(execution: dict[str, Any]) -> bool:
    """Imported phase 1 executions have no job snapshot."""
    return execution.get("job_yaml") is None


def reveal_target(execution: dict[str, Any], run_number: int | None) -> Path | str:
    """The output file to reveal for the run (default: the last run with an output), or why there is none."""
    runs = execution["runs"]
    if run_number is None:
        with_output = [run for run in runs if run.get("output")]
        if not with_output:
            return f"Execution {execution_label(execution)} has no output"
        run = with_output[-1]
    else:
        matches = [run for run in runs if run["number"] == run_number]
        if not matches:
            return f"Execution {execution_label(execution)} has no run {run_number}"
        run = matches[0]
    path = run_file(execution, run.get("output"))
    if path is None:
        return f"Run {run['number']} of execution {execution_label(execution)} has no output" if not run.get("output") else f"The output directory of execution {execution_label(execution)} was not recorded"
    if not path.exists():
        return f"{path} is missing"
    return path


def reveal_in_finder(path: Path) -> str | None:
    """Select the file in a Finder window; None when it did, else why not. The path is an argument, never shell text."""
    try:
        result = subprocess.run(["open", "-R", str(path)], check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return f"Cannot reveal {path}: {error}"
    if result.returncode != 0:
        return f"Cannot reveal {path}: open exited with {result.returncode}{': ' + result.stderr.strip() if result.stderr.strip() else ''}"
    return None


def copy_to_pasteboard(text: str) -> str | None:
    """Put ``text`` on the macOS clipboard with pbcopy; None when it did, else why not (also when there is no pbcopy)."""
    pbcopy = shutil.which("pbcopy")
    if pbcopy is None:
        return "pbcopy was not found"
    # pbcopy reads its input in the locale's encoding; UTF-8 keeps any prompt's characters.
    environment = {**os.environ, "LC_CTYPE": "UTF-8"}
    try:
        result = subprocess.run([pbcopy], input=text.encode("utf-8"), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, env=environment)
    except (OSError, subprocess.SubprocessError) as error:
        return f"pbcopy failed: {error}"
    if result.returncode != 0:
        return f"pbcopy exited with {result.returncode}"
    return None


def reveal_run(execution: dict[str, Any] | str, run_number: int | None) -> tuple[str, str]:
    """Reveal the run's output in Finder (/reveal, a click, or Enter): the message to say, and its style. ``execution`` is
    as the reader returns it, a string when it could not be read; runs off the UI thread."""
    target = reveal_target(execution, run_number) if isinstance(execution, dict) else execution
    if isinstance(target, str):
        return target, "red"
    error = reveal_in_finder(target)
    return (error, "red") if error else (f"Revealed {target}", "")
