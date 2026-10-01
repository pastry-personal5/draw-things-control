"""Reads and writes the ``executions`` and ``runs`` tables. The rows themselves are ``state/execution_rows.py``, re-exported
here so nothing that already imports them from this module needs to change."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from draw_things_control.core.clock import local_timestamp
from draw_things_control.state.database import Database, next_number
from draw_things_control.state.execution_rows import (
    EXECUTION_COLUMNS,
    RUN_COLUMNS,
    SUCCEEDED_COUNT,
    ExecutionRow,
    ExecutionSettings,
    MediaCheckRow,
    NewExecution,
    NewRun,
    RunRow,
    epoch,
)
from draw_things_control.state.ids import execution_id_text

__all__ = ["EXECUTION_COLUMNS", "RUN_COLUMNS", "SUCCEEDED_COUNT", "DeletedExecution", "ExecutionDeletion", "ExecutionRepository", "ExecutionRow", "ExecutionSettings", "MediaCheckRow", "NewExecution", "NewRun", "RunRow", "epoch"]


@dataclass(frozen=True)
class DeletedExecution:
    """An execution a deletion removed (or, in a dry run, would remove), with the files it named."""

    number: int
    log_path: str | None
    manifest_path: str | None


@dataclass(frozen=True)
class ExecutionDeletion:
    """What ``ExecutionRepository.delete`` did: the executions deleted, those refused with the reason, and the numbers
    no execution has."""

    deleted: list[DeletedExecution] = field(default_factory=list)
    refused: dict[int, str] = field(default_factory=dict)
    missing: list[int] = field(default_factory=list)


class ExecutionRepository:
    """Reads and writes the ``executions`` and ``runs`` tables."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def reserve_number(self) -> int:
        """The next execution number, taken for good: the counter only goes up, so a number is never given twice."""
        with self._database.transaction() as connection:
            return next_number(connection, "execution")

    def start(self, new: NewExecution) -> int:
        """Insert a ``running`` execution; return its row id."""
        with self._database.transaction() as connection:
            return self._insert_execution(connection, new)[0]

    def start_run(self, execution_id: int, number: int, new: NewRun) -> None:
        with self._database.transaction() as connection:
            self._insert_run(connection, execution_id, number, new)

    def finish_run(self, execution_id: int, number: int, *, status: str, exit_code: int | None, seconds: float | None, output: str | None, last_frame: str | None, output_width: int | None = None, output_height: int | None = None, output_frames: int | None = None, corrected_output: str | None = None) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, exit_code = ?, seconds = ?, output = ?, last_frame = ?, output_width = ?, output_height = ?, output_frames = ?, corrected_output = ? WHERE execution_id = ? AND number = ?",
                (status, exit_code, seconds, output, last_frame, output_width, output_height, output_frames, corrected_output, execution_id, number),
            )

    def add_check(self, execution_id: int, check: MediaCheckRow) -> None:
        """Keep one media check of run ``check.run``; the run's own row may not exist yet (the input's checks come first)."""
        with self._database.transaction() as connection:
            connection.execute("INSERT INTO media_checks (execution_id, run, stage, file, summary, verdict, notes, facts, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (execution_id, check.run, check.stage, check.file, check.summary, check.verdict, json.dumps(list(check.notes)), json.dumps(check.facts), check.at))

    def set_run_cooldown(self, execution_id: int, number: int, seconds: float) -> None:
        with self._database.transaction() as connection:
            connection.execute("UPDATE runs SET cooldown_after_seconds = ? WHERE execution_id = ? AND number = ?", (seconds, execution_id, number))

    def set_first_image(self, execution_id: int, first_image: str | None) -> None:
        """Name the execution's first image, or none, once its job has dropped the one it started with (Milestone 09)."""
        with self._database.transaction() as connection:
            connection.execute("UPDATE executions SET first_image = ? WHERE id = ?", (first_image, execution_id))

    def finish(self, execution_id: int, *, status: str, exit_code: int | None, signal: str | None, finished_at: str) -> None:
        with self._database.transaction() as connection:
            connection.execute("UPDATE executions SET status = ?, exit_code = ?, signal = ?, finished_at = ?, finished_epoch = ? WHERE id = ?", (status, exit_code, signal, finished_at, epoch(finished_at), execution_id))

    def page(self, *, limit: int = 50, offset: int = 0, status: str | None = None, name: str | None = None, name_contains: str | None = None, job_file: str | None = None, running_as_interrupted: bool = False) -> list[ExecutionRow]:
        """Executions, newest first, without their runs. ``name`` matches the job name exactly; ``name_contains`` matches
        the job name or the job file's name as a substring, in any ASCII letter case; ``job_file`` matches the exact
        stored job file path (Milestone 02's ``GET /executions``, filtering by a job reference resolved to one file,
        as ``{job}`` is everywhere else, rather than the job's own ``name:``, which is not an identifier).
        ``running_as_interrupted`` is for read-only screens that know no runner is alive: it changes the display
        only, never the database."""
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            if running_as_interrupted and status == "interrupted":
                clauses.append("status IN ('interrupted', 'running')")
            elif running_as_interrupted and status == "running":
                clauses.append("0")
            else:
                clauses.append("status = ?")
                values.append(str(status))
        if name is not None:
            clauses.append("job_name = ?")
            values.append(name)
        if job_file is not None:
            clauses.append("job_file = ?")
            values.append(job_file)
        if name_contains is not None:
            pattern = "%" + name_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            # The file name is what follows the last '/': rtrim strips the non-slash characters from the end.
            clauses.append("(job_name LIKE ? ESCAPE '\\' OR substr(job_file, length(rtrim(job_file, replace(job_file, '/', ''))) + 1) LIKE ? ESCAPE '\\')")
            values.extend((pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._database.connection().execute(f"SELECT executions.*, {SUCCEEDED_COUNT} FROM executions {where} ORDER BY started_epoch DESC, id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return [ExecutionRow.from_row(row, running_as_interrupted) for row in rows]

    def get(self, execution_id: int, *, running_as_interrupted: bool = False) -> ExecutionRow | None:
        """One execution with its runs, by its row id, or None."""
        connection = self._database.connection()
        row = connection.execute(f"SELECT executions.*, {SUCCEEDED_COUNT} FROM executions WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            return None
        checks: dict[int, list[MediaCheckRow]] = {}
        for check in connection.execute("SELECT * FROM media_checks WHERE execution_id = ? ORDER BY id", (execution_id,)):
            checks.setdefault(check["run"], []).append(MediaCheckRow.from_row(check))
        runs = tuple(replace(RunRow.from_row(run, running_as_interrupted), checks=tuple(checks.pop(run["number"], ()))) for run in connection.execute("SELECT * FROM runs WHERE execution_id = ? ORDER BY number", (execution_id,)))
        unstarted = tuple(check for number in sorted(checks) for check in checks[number])
        return replace(ExecutionRow.from_row(row, running_as_interrupted, runs), checks=unstarted)

    def by_ids(self, execution_ids: Sequence[int], *, running_as_interrupted: bool = False) -> list[ExecutionRow]:
        """The executions with these row ids, without their runs, in no set order; a missing id is left out."""
        if not execution_ids:
            return []
        marks = ", ".join("?" for _ in execution_ids)
        rows = self._database.connection().execute(f"SELECT executions.*, {SUCCEEDED_COUNT} FROM executions WHERE id IN ({marks})", list(execution_ids)).fetchall()
        return [ExecutionRow.from_row(row, running_as_interrupted) for row in rows]

    def number_of(self, execution_row: int) -> int | None:
        """The execution number of the row with this id, or None when there is no such row."""
        row = self._database.connection().execute("SELECT execution_number FROM executions WHERE id = ?", (execution_row,)).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def row_of(self, execution_number: int) -> int | None:
        """The row id of the execution with this number, or None when there is none (never given, or pruned)."""
        row = self._database.connection().execute("SELECT id FROM executions WHERE execution_number = ?", (execution_number,)).fetchone()
        return int(row[0]) if row is not None else None

    def by_number(self, execution_number: int, *, running_as_interrupted: bool = False) -> ExecutionRow | None:
        """One execution with its runs, by its public number (E0012), or None when there is none (never given, or pruned)."""
        row_id = self.row_of(execution_number)
        return self.get(row_id, running_as_interrupted=running_as_interrupted) if row_id is not None else None

    def latest_succeeded_run(self) -> RunRow | None:
        """The most recently started run that succeeded with a known time, of any job; None when there is none."""
        row = self._database.connection().execute("SELECT * FROM runs WHERE status = 'succeeded' AND seconds IS NOT NULL ORDER BY started_epoch DESC, id DESC LIMIT 1").fetchone()
        return RunRow.from_row(row) if row is not None else None

    def has_manifest(self, manifest_path: str) -> bool:
        return self._database.connection().execute("SELECT 1 FROM executions WHERE manifest_path = ?", (manifest_path,)).fetchone() is not None

    def import_execution(self, new: NewExecution, runs: Sequence[NewRun], *, first_run: int = 1) -> tuple[int, int]:
        """Insert a finished execution and its runs in one transaction (used by the phase 1 import); return its row id and
        the execution number it was given. ``first_run`` numbers the runs from a resumed manifest's own first run, never
        from 1 (its ``runs`` list holds only the runs it made, at positions 0, 1, 2, ...)."""
        with self._database.transaction() as connection:
            execution_id, execution_number = self._insert_execution(connection, new)
            for offset, run in enumerate(runs):
                self._insert_run(connection, execution_id, first_run + offset, run)
            return execution_id, execution_number

    def sweep_interrupted(self, now: datetime) -> int:
        """Close every ``running`` execution, and its ``running`` runs, as ``interrupted``.

        Only call this while holding the run lock: that proves no runner is alive, and it keeps a run that
        is about to start from having its own new row swept.
        """
        stamp = local_timestamp(now)
        with self._database.transaction() as connection:
            connection.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running' AND execution_id IN (SELECT id FROM executions WHERE status = 'running')")
            cursor = connection.execute("UPDATE executions SET status = 'interrupted', recovered_at = ?, finished_at = ?, finished_epoch = ? WHERE status = 'running'", (stamp, stamp, epoch(stamp)))
            return cursor.rowcount

    def prune(self, cutoff: float, *, keep: Sequence[int] = ()) -> tuple[int, list[str]]:
        """Delete executions (and their runs) finished before the epoch ``cutoff``, never a ``running`` one, nor one
        whose number is in ``keep`` (a parked queue entry's, Milestone 05); return how many, and the log files they named."""
        # ``keep`` goes in as one JSON array, not a placeholder each, so no number of kept executions reaches SQLite's
        # limit on bound values.
        condition = "status != 'running' AND finished_epoch IS NOT NULL AND finished_epoch < ? AND (execution_number IS NULL OR execution_number NOT IN (SELECT value FROM json_each(?)))"
        values = (cutoff, json.dumps(list(keep)))
        with self._database.transaction() as connection:
            log_paths = [row[0] for row in connection.execute(f"SELECT log_path FROM executions WHERE {condition} AND log_path IS NOT NULL", values)]
            deleted = connection.execute(f"DELETE FROM executions WHERE {condition}", values).rowcount
        return deleted, log_paths

    def delete(self, numbers: Sequence[int], *, in_use: Mapping[int, str], dry_run: bool = False) -> ExecutionDeletion:
        """Delete the executions with these numbers (and their runs) in one transaction (Milestone 06): never a missing,
        ``running``, or in-use one (``in_use`` maps an execution number to the reason it is used). ``dry_run`` reads
        and reports the same, deleting nothing."""
        deleted: list[DeletedExecution] = []
        refused: dict[int, str] = {}
        missing: list[int] = []
        with self._database.transaction() as connection:
            for number in dict.fromkeys(numbers):
                row = connection.execute("SELECT id, status, log_path, manifest_path FROM executions WHERE execution_number = ?", (number,)).fetchone()
                if row is None:
                    missing.append(number)
                elif row["status"] == "running":
                    refused[number] = f"{execution_id_text(number)} is running; it cannot be deleted"
                elif number in in_use:
                    refused[number] = in_use[number]
                else:
                    if not dry_run:
                        connection.execute("DELETE FROM executions WHERE id = ?", (row["id"],))
                    deleted.append(DeletedExecution(number, row["log_path"], row["manifest_path"]))
        return ExecutionDeletion(deleted, refused, missing)

    @staticmethod
    def _insert_execution(connection: sqlite3.Connection, new: NewExecution) -> tuple[int, int]:
        """Insert the execution; return its row id and its execution number (the next one, when none was given)."""
        values: dict[str, Any] = {column: getattr(new, column) for column in EXECUTION_COLUMNS}
        if values["execution_number"] is None:
            values["execution_number"] = next_number(connection, "execution")
        values["status"] = str(values["status"])
        values["settings"] = new.settings.to_json()
        values["started_epoch"] = epoch(new.started_at)
        values["finished_epoch"] = epoch(new.finished_at) if new.finished_at is not None else None
        columns = ", ".join(values)
        cursor = connection.execute(f"INSERT INTO executions ({columns}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
        return int(cursor.lastrowid or 0), int(values["execution_number"])

    @staticmethod
    def _insert_run(connection: sqlite3.Connection, execution_id: int, number: int, new: NewRun) -> None:
        values: dict[str, Any] = {column: getattr(new, column) for column in RUN_COLUMNS}
        values["status"] = str(values["status"])
        values["command"] = json.dumps(list(new.command), ensure_ascii=False)
        values["started_epoch"] = epoch(new.started_at)
        columns = ", ".join(("execution_id", "number", *values))
        connection.execute(f"INSERT INTO runs ({columns}) VALUES ({', '.join('?' for _ in range(len(values) + 2))})", (execution_id, number, *values.values()))
