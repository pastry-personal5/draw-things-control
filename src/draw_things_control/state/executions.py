"""Executions and their runs: the rows as values, and the repository that reads and writes them."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.core.clock import local_timestamp
from draw_things_control.jobs.events import JobStatus, RunStatus
from draw_things_control.state.database import Database, next_number
from draw_things_control.state.ids import execution_id_text


def epoch(timestamp: str) -> float:
    """The UTC epoch of a local ISO 8601 timestamp with an offset, for ordering and retention."""
    return datetime.fromisoformat(timestamp).timestamp()


@dataclass(frozen=True)
class ExecutionSettings:
    """What an execution ran with, besides its columns: kept as JSON, so every key is optional (an imported execution has fewer)."""

    input: str | None = None
    output_directory: str | None = None
    cooldown_seconds: float | None = None
    cooldown_source: str | None = None
    # The resolved cooldown mapping; absent in an execution written before it was kept, which had only ``cooldown_seconds``.
    cooldown: dict[str, Any] | None = None
    config_file: str | None = None
    config_override: dict[str, Any] | None = None
    input_resize: dict[str, Any] | None = None

    def to_json(self) -> str:
        """The keys that are set, as JSON."""
        return json.dumps({name: value for name, value in self.__dict__.items() if value is not None}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ExecutionSettings:
        data = json.loads(text) if text else {}
        return cls(**{name: data.get(name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class NewRun:
    """A run to insert: its prompts, command (credentials redacted), files, and, when it is already over, its result."""

    pair: str
    positive: str
    started_at: str
    negative: str | None = None
    input: str | None = None
    resized_input: str | None = None
    output: str | None = None
    last_frame: str | None = None
    command: Sequence[str] = ()
    status: str = RunStatus.RUNNING
    seconds: float | None = None
    exit_code: int | None = None
    cooldown_after_seconds: float | None = None
    output_width: int | None = None
    output_height: int | None = None
    output_frames: int | None = None


@dataclass(frozen=True)
class NewExecution:
    """An execution to insert. Without an ``execution_number`` (one ``ExecutionRepository.reserve_number`` gave), the next one is taken."""

    job_name: str
    job_file: str
    mode: str
    started_at: str
    status: str = JobStatus.RUNNING
    model: str | None = None
    seed: int | None = None
    seed_source: str | None = None
    cooldown_seconds: float | None = None
    cooldown_source: str | None = None
    total_runs: int | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    signal: str | None = None
    manifest_path: str | None = None
    log_path: str | None = None
    config_file: str | None = None
    job_yaml: str | None = None
    recovered_at: str | None = None
    settings: ExecutionSettings = field(default_factory=ExecutionSettings)
    execution_number: int | None = None


@dataclass(frozen=True)
class RunRow:
    """One stored run of an execution."""

    number: int
    pair: str
    positive: str
    negative: str | None
    input: str | None
    resized_input: str | None
    output: str | None
    last_frame: str | None
    command: tuple[str, ...]
    started_at: str
    seconds: float | None
    exit_code: int | None
    status: str
    cooldown_after_seconds: float | None
    output_width: int | None
    output_height: int | None
    output_frames: int | None
    # The store's own row number and start time as an epoch, for ordering.
    id: int = 0
    started_epoch: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row, running_as_interrupted: bool = False) -> RunRow:
        status = row["status"]
        return cls(
            number=row["number"],
            pair=row["pair"],
            positive=row["positive"],
            negative=row["negative"],
            input=row["input"],
            resized_input=row["resized_input"],
            output=row["output"],
            last_frame=row["last_frame"],
            command=tuple(json.loads(row["command"])),
            started_at=row["started_at"],
            seconds=row["seconds"],
            exit_code=row["exit_code"],
            status=RunStatus.INTERRUPTED if running_as_interrupted and status == RunStatus.RUNNING else status,
            cooldown_after_seconds=row["cooldown_after_seconds"],
            output_width=row["output_width"],
            output_height=row["output_height"],
            output_frames=row["output_frames"],
            id=row["id"],
            started_epoch=row["started_epoch"],
        )


@dataclass(frozen=True)
class ExecutionRow:
    """One stored execution. ``runs`` is filled only when it is read by itself; ``succeeded`` counts its successful runs."""

    # The store's own row number, which stays internal: a person sees ``label``.
    id: int
    execution_number: int
    job_name: str
    job_file: str
    mode: str
    status: str
    model: str | None
    seed: int | None
    seed_source: str | None
    cooldown_seconds: float | None
    cooldown_source: str | None
    total_runs: int | None
    started_at: str
    started_epoch: float
    finished_at: str | None
    finished_epoch: float | None
    exit_code: int | None
    signal: str | None
    manifest_path: str | None
    log_path: str | None
    config_file: str | None
    job_yaml: str | None
    settings: ExecutionSettings
    recovered_at: str | None
    succeeded: int = 0
    runs: tuple[RunRow, ...] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row, running_as_interrupted: bool = False, runs: tuple[RunRow, ...] = ()) -> ExecutionRow:
        status = row["status"]
        return cls(
            id=row["id"],
            execution_number=row["execution_number"],
            job_name=row["job_name"],
            job_file=row["job_file"],
            mode=row["mode"],
            status=JobStatus.INTERRUPTED if running_as_interrupted and status == JobStatus.RUNNING else status,
            model=row["model"],
            seed=row["seed"],
            seed_source=row["seed_source"],
            cooldown_seconds=row["cooldown_seconds"],
            cooldown_source=row["cooldown_source"],
            total_runs=row["total_runs"],
            started_at=row["started_at"],
            started_epoch=row["started_epoch"],
            finished_at=row["finished_at"],
            finished_epoch=row["finished_epoch"],
            exit_code=row["exit_code"],
            signal=row["signal"],
            manifest_path=row["manifest_path"],
            log_path=row["log_path"],
            config_file=row["config_file"],
            job_yaml=row["job_yaml"],
            settings=ExecutionSettings.from_json(row["settings"]),
            recovered_at=row["recovered_at"],
            succeeded=row["succeeded"] if "succeeded" in row.keys() else 0,
            runs=runs,
        )

    @property
    def label(self) -> str:
        """The execution's ID as people see it (E0012)."""
        return execution_id_text(self.execution_number)

    @property
    def imported(self) -> bool:
        """Imported phase 1 executions have no job snapshot."""
        return self.job_yaml is None

    @property
    def output_directory(self) -> Path | None:
        """Where the execution's outputs are: as recorded, or beside its manifest for an imported one."""
        if self.settings.output_directory:
            return Path(self.settings.output_directory)
        return Path(self.manifest_path).parent if self.manifest_path else None

    def run_file(self, name: str | None) -> Path | None:
        """The full path of a run's output or last frame, which the store keeps as a file name."""
        if not name:
            return None
        if Path(name).is_absolute():
            return Path(name)
        directory = self.output_directory
        return directory / name if directory is not None else None

    @property
    def succeeded_runs(self) -> tuple[RunRow, ...]:
        """The runs that finished successfully, in run order."""
        return tuple(run for run in self.runs if run.status == RunStatus.SUCCEEDED)

    def run(self, number: int) -> RunRow | None:
        return next((run for run in self.runs if run.number == number), None)


EXECUTION_COLUMNS = ("execution_number", "job_name", "job_file", "mode", "status", "model", "seed", "seed_source", "cooldown_seconds", "cooldown_source", "total_runs", "started_at", "finished_at", "exit_code", "signal", "manifest_path", "log_path", "config_file", "job_yaml", "recovered_at")
RUN_COLUMNS = ("pair", "positive", "negative", "input", "resized_input", "output", "last_frame", "started_at", "seconds", "exit_code", "status", "cooldown_after_seconds", "output_width", "output_height", "output_frames")
# Each execution's count of successful runs, read with it.
SUCCEEDED_COUNT = "(SELECT COUNT(*) FROM runs WHERE runs.execution_id = executions.id AND runs.status = 'succeeded') AS succeeded"


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

    def finish_run(self, execution_id: int, number: int, *, status: str, exit_code: int | None, seconds: float | None, output: str | None, last_frame: str | None, output_width: int | None = None, output_height: int | None = None, output_frames: int | None = None) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, exit_code = ?, seconds = ?, output = ?, last_frame = ?, output_width = ?, output_height = ?, output_frames = ? WHERE execution_id = ? AND number = ?",
                (status, exit_code, seconds, output, last_frame, output_width, output_height, output_frames, execution_id, number),
            )

    def set_run_cooldown(self, execution_id: int, number: int, seconds: float) -> None:
        with self._database.transaction() as connection:
            connection.execute("UPDATE runs SET cooldown_after_seconds = ? WHERE execution_id = ? AND number = ?", (seconds, execution_id, number))

    def finish(self, execution_id: int, *, status: str, exit_code: int | None, signal: str | None, finished_at: str) -> None:
        with self._database.transaction() as connection:
            connection.execute("UPDATE executions SET status = ?, exit_code = ?, signal = ?, finished_at = ?, finished_epoch = ? WHERE id = ?", (status, exit_code, signal, finished_at, epoch(finished_at), execution_id))

    def page(self, *, limit: int = 50, offset: int = 0, status: str | None = None, name: str | None = None, name_contains: str | None = None, running_as_interrupted: bool = False) -> list[ExecutionRow]:
        """Executions, newest first, without their runs. ``name`` matches the job name exactly; ``name_contains`` matches
        the job name or the job file's name as a substring, in any ASCII letter case. ``running_as_interrupted`` is for
        read-only screens that know no runner is alive: it changes the display only, never the database."""
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            if running_as_interrupted and status == JobStatus.INTERRUPTED:
                clauses.append("status IN ('interrupted', 'running')")
            elif running_as_interrupted and status == JobStatus.RUNNING:
                clauses.append("0")
            else:
                clauses.append("status = ?")
                values.append(str(status))
        if name is not None:
            clauses.append("job_name = ?")
            values.append(name)
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
        runs = tuple(RunRow.from_row(run, running_as_interrupted) for run in connection.execute("SELECT * FROM runs WHERE execution_id = ? ORDER BY number", (execution_id,)))
        return ExecutionRow.from_row(row, running_as_interrupted, runs)

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

    def latest_succeeded_run(self) -> RunRow | None:
        """The most recently started run that succeeded with a known time, of any job; None when there is none."""
        row = self._database.connection().execute("SELECT * FROM runs WHERE status = 'succeeded' AND seconds IS NOT NULL ORDER BY started_epoch DESC, id DESC LIMIT 1").fetchone()
        return RunRow.from_row(row) if row is not None else None

    def has_manifest(self, manifest_path: str) -> bool:
        return self._database.connection().execute("SELECT 1 FROM executions WHERE manifest_path = ?", (manifest_path,)).fetchone() is not None

    def import_execution(self, new: NewExecution, runs: Sequence[NewRun]) -> tuple[int, int]:
        """Insert a finished execution and its runs in one transaction (used by the phase 1 import); return its row id and
        the execution number it was given."""
        with self._database.transaction() as connection:
            execution_id, execution_number = self._insert_execution(connection, new)
            for number, run in enumerate(runs, start=1):
                self._insert_run(connection, execution_id, number, run)
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

    def prune(self, cutoff: float) -> tuple[int, list[str]]:
        """Delete executions (and their runs) finished before the epoch ``cutoff``, never a ``running`` one; return how many,
        and the log files they named."""
        condition = "status != 'running' AND finished_epoch IS NOT NULL AND finished_epoch < ?"
        with self._database.transaction() as connection:
            log_paths = [row[0] for row in connection.execute(f"SELECT log_path FROM executions WHERE {condition} AND log_path IS NOT NULL", (cutoff,))]
            deleted = connection.execute(f"DELETE FROM executions WHERE {condition}", (cutoff,)).rowcount
        return deleted, log_paths

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
