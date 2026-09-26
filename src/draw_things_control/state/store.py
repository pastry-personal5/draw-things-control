"""SQLite record of job executions and their runs, shared by every front end."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.core.run_lock import ensure_state_directory

DATABASE_FILE_NAME = "dtc.db"
BUSY_TIMEOUT_MILLISECONDS = 5000
SECONDS_PER_DAY = 86400

SCHEMA_V1 = """
CREATE TABLE executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    job_file TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    model TEXT,
    seed INTEGER,
    seed_source TEXT,
    cooldown_seconds REAL,
    cooldown_source TEXT,
    total_runs INTEGER,
    started_at TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    finished_at TEXT,
    finished_epoch REAL,
    exit_code INTEGER,
    signal TEXT,
    manifest_path TEXT UNIQUE,
    log_path TEXT,
    config_file TEXT,
    job_yaml TEXT,
    settings TEXT NOT NULL DEFAULT '{}',
    recovered_at TEXT
);
CREATE INDEX executions_started ON executions (started_epoch DESC);
CREATE INDEX executions_finished ON executions (finished_epoch);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL REFERENCES executions (id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    pair TEXT NOT NULL,
    positive TEXT NOT NULL,
    negative TEXT,
    input TEXT,
    resized_input TEXT,
    output TEXT,
    last_frame TEXT,
    command TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    started_epoch REAL NOT NULL,
    seconds REAL,
    exit_code INTEGER,
    status TEXT NOT NULL,
    cooldown_after_seconds REAL,
    UNIQUE (execution_id, number)
);
"""

# Each run's output as measured: its actual size and frame count, which may differ from what was requested.
SCHEMA_V2 = """
ALTER TABLE runs ADD COLUMN output_width INTEGER;
ALTER TABLE runs ADD COLUMN output_height INTEGER;
ALTER TABLE runs ADD COLUMN output_frames INTEGER
"""

# Forward-only: migration N runs when the database is at N - 1. The list index is the version reached. Any open migrates,
# a browsing one too (owner decision): an upgrade is the one write a read-only screen may make.
MIGRATIONS: tuple[str, ...] = (SCHEMA_V1, SCHEMA_V2)
SCHEMA_VERSION = len(MIGRATIONS)

EXECUTION_COLUMNS = ("job_name", "job_file", "mode", "status", "model", "seed", "seed_source", "cooldown_seconds", "cooldown_source", "total_runs", "started_at", "finished_at", "exit_code", "signal", "manifest_path", "log_path", "config_file", "job_yaml", "recovered_at")
RUN_COLUMNS = ("pair", "positive", "negative", "input", "resized_input", "output", "last_frame", "started_at", "seconds", "exit_code", "status", "cooldown_after_seconds", "output_width", "output_height", "output_frames")


class StateError(Exception):
    """The state database cannot be used: a newer schema, no WAL support, or a file that will not open."""


def epoch(timestamp: str) -> float:
    """The UTC epoch of a local ISO 8601 timestamp with an offset, for ordering and retention."""
    return datetime.fromisoformat(timestamp).timestamp()


class Store:
    """The state database. One connection per thread, since sqlite3 connections are not thread-safe."""

    def __init__(self, path: Path | None = None, *, retention_days: int = 14, clock: Callable[[], datetime] = datetime.now, prune_on_open: bool = True) -> None:
        self._path = path if path is not None else ensure_state_directory() / DATABASE_FILE_NAME
        self._retention_days = retention_days
        self._clock = clock
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._create_file()
        try:
            self._migrate(self._connection())
            if prune_on_open:
                self.prune()
        except BaseException:
            # The caller gets no Store to close, so close here.
            self.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        """Close every thread's connection."""
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()
        self._local = threading.local()

    def start_execution(self, **fields: Any) -> int:
        """Insert a ``running`` execution from ``EXECUTION_COLUMNS`` keywords plus ``settings``; return its id."""
        with self._transaction() as connection:
            return self._insert_execution(connection, fields)

    def start_run(self, execution_id: int, number: int, **fields: Any) -> None:
        """Insert a ``running`` run from ``RUN_COLUMNS`` keywords plus ``command``."""
        with self._transaction() as connection:
            self._insert_run(connection, execution_id, number, {"status": "running", **fields})

    def finish_run(self, execution_id: int, number: int, *, status: str, exit_code: int | None, seconds: float | None, output: str | None, last_frame: str | None, output_width: int | None = None, output_height: int | None = None, output_frames: int | None = None) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, exit_code = ?, seconds = ?, output = ?, last_frame = ?, output_width = ?, output_height = ?, output_frames = ? WHERE execution_id = ? AND number = ?",
                (status, exit_code, seconds, output, last_frame, output_width, output_height, output_frames, execution_id, number),
            )

    def set_run_cooldown(self, execution_id: int, number: int, seconds: float) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE runs SET cooldown_after_seconds = ? WHERE execution_id = ? AND number = ?", (seconds, execution_id, number))

    def finish_execution(self, execution_id: int, *, status: str, exit_code: int | None, signal: str | None, finished_at: str) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE executions SET status = ?, exit_code = ?, signal = ?, finished_at = ?, finished_epoch = ? WHERE id = ?", (status, exit_code, signal, finished_at, epoch(finished_at), execution_id))

    def list_executions(self, *, limit: int = 50, offset: int = 0, status: str | None = None, name: str | None = None, name_contains: str | None = None, running_as_interrupted: bool = False) -> list[dict[str, Any]]:
        """Executions, newest first, without their runs. ``name`` matches the job name exactly; ``name_contains`` matches
        the job name or the job file's name as a substring, in any ASCII letter case. ``running_as_interrupted`` is for
        read-only screens that know no runner is alive: it changes the display only, never the database."""
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            if running_as_interrupted and status == "interrupted":
                clauses.append("status IN ('interrupted', 'running')")
            elif running_as_interrupted and status == "running":
                clauses.append("0")
            else:
                clauses.append("status = ?")
                values.append(status)
        if name is not None:
            clauses.append("job_name = ?")
            values.append(name)
        if name_contains is not None:
            pattern = "%" + name_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            # The file name is what follows the last '/': rtrim strips the non-slash characters from the end.
            clauses.append("(job_name LIKE ? ESCAPE '\\' OR substr(job_file, length(rtrim(job_file, replace(job_file, '/', ''))) + 1) LIKE ? ESCAPE '\\')")
            values.extend((pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection().execute(f"SELECT * FROM executions {where} ORDER BY started_epoch DESC, id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return [self._execution(row, running_as_interrupted) for row in rows]

    def get_execution(self, execution_id: int, *, running_as_interrupted: bool = False) -> dict[str, Any] | None:
        """One execution with its runs (under ``runs``, in order), or None."""
        connection = self._connection()
        row = connection.execute("SELECT * FROM executions WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            return None
        execution = self._execution(row, running_as_interrupted)
        runs = connection.execute("SELECT * FROM runs WHERE execution_id = ? ORDER BY number", (execution_id,)).fetchall()
        execution["runs"] = [self._run(run, running_as_interrupted) for run in runs]
        return execution

    def executions_by_id(self, execution_ids: list[int], *, running_as_interrupted: bool = False) -> list[dict[str, Any]]:
        """The executions with these IDs, without their runs, in no set order; a missing ID is left out."""
        if not execution_ids:
            return []
        rows = self._connection().execute(f"SELECT * FROM executions WHERE id IN ({', '.join('?' for _ in execution_ids)})", execution_ids).fetchall()
        return [self._execution(row, running_as_interrupted) for row in rows]

    def succeeded_runs(self, execution_ids: list[int]) -> dict[int, int]:
        """How many runs of each execution succeeded; an execution with none is absent."""
        if not execution_ids:
            return {}
        rows = self._connection().execute(f"SELECT execution_id, COUNT(*) FROM runs WHERE status = 'succeeded' AND execution_id IN ({', '.join('?' for _ in execution_ids)}) GROUP BY execution_id", execution_ids).fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    def latest_succeeded_run(self) -> dict[str, Any] | None:
        """The most recently started run that succeeded with a known time, of any job; None when there is none."""
        row = self._connection().execute("SELECT * FROM runs WHERE status = 'succeeded' AND seconds IS NOT NULL ORDER BY started_epoch DESC, id DESC LIMIT 1").fetchone()
        return self._run(row, False) if row is not None else None

    def has_manifest(self, manifest_path: str) -> bool:
        return self._connection().execute("SELECT 1 FROM executions WHERE manifest_path = ?", (manifest_path,)).fetchone() is not None

    def import_execution(self, execution: dict[str, Any], runs: list[dict[str, Any]]) -> int:
        """Insert a finished execution and its runs in one transaction (used by the phase 1 import)."""
        with self._transaction() as connection:
            execution_id = self._insert_execution(connection, execution)
            for number, run in enumerate(runs, start=1):
                self._insert_run(connection, execution_id, number, run)
            return execution_id

    def sweep_interrupted(self) -> int:
        """Close every ``running`` execution, and its ``running`` runs, as ``interrupted``.

        Only call this while holding the run lock: that proves no runner is alive, and it keeps a run that
        is about to start from having its own new row swept.
        """
        now = self._clock().astimezone().isoformat(timespec="seconds")
        with self._transaction() as connection:
            connection.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running' AND execution_id IN (SELECT id FROM executions WHERE status = 'running')")
            cursor = connection.execute("UPDATE executions SET status = 'interrupted', recovered_at = ?, finished_at = ?, finished_epoch = ? WHERE status = 'running'", (now, now, epoch(now)))
            return cursor.rowcount

    def prune(self) -> int:
        """Delete executions (and their runs) finished before the retention cutoff; never a ``running`` one. Rows only."""
        if self._retention_days <= 0:
            return 0
        cutoff = self._clock().timestamp() - self._retention_days * SECONDS_PER_DAY
        with self._transaction() as connection:
            return connection.execute("DELETE FROM executions WHERE status != 'running' AND finished_epoch IS NOT NULL AND finished_epoch < ?", (cutoff,)).rowcount

    def retention_cutoff(self) -> float | None:
        """The UTC epoch before which history is pruned, or None when it is kept forever."""
        return self._clock().timestamp() - self._retention_days * SECONDS_PER_DAY if self._retention_days > 0 else None

    def _create_file(self) -> None:
        """Create the database file with mode 0600 first, so SQLite's WAL files inherit it: history holds prompts."""
        try:
            os.close(os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600))
        except OSError as error:
            raise StateError(f"Cannot create the state database {self._path}: {error.strerror}") from error

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    def _connect(self) -> sqlite3.Connection:
        try:
            # Autocommit; transactions are explicit, so BEGIN IMMEDIATE takes the write lock up front.
            connection = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as error:
            raise StateError(f"Cannot open the state database {self._path}: {error}") from error
        if str(mode).lower() != "wal":
            connection.close()
            raise StateError(f"The state directory {self._path.parent} does not support SQLite WAL mode (journal mode is '{mode}'); it may be on a network or unsupported filesystem")
        return connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Read again under the write lock: another process may have migrated in between.
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise StateError(f"{self._path} was written by a newer version of this program (schema {version}, this one knows {SCHEMA_VERSION}); update the program")
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in MIGRATIONS[target - 1].split(";\n"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {target}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.Error as error:
            raise StateError(f"Cannot prepare the state database {self._path}: {error}") from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            # A failed COMMIT leaves the transaction open, which would fail every later write on this thread.
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _insert_execution(connection: sqlite3.Connection, fields: dict[str, Any]) -> int:
        values = {column: fields.get(column) for column in EXECUTION_COLUMNS}
        values["status"] = values["status"] or "running"
        values["settings"] = json.dumps(fields.get("settings") or {}, ensure_ascii=False)
        values["started_epoch"] = epoch(values["started_at"])
        finished = values["finished_at"]
        values["finished_epoch"] = epoch(finished) if finished is not None else None
        columns = ", ".join(values)
        cursor = connection.execute(f"INSERT INTO executions ({columns}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
        return int(cursor.lastrowid or 0)

    @staticmethod
    def _insert_run(connection: sqlite3.Connection, execution_id: int, number: int, fields: dict[str, Any]) -> None:
        values = {column: fields.get(column) for column in RUN_COLUMNS}
        values["command"] = json.dumps(fields.get("command") or [], ensure_ascii=False)
        values["started_epoch"] = epoch(values["started_at"])
        columns = ", ".join(("execution_id", "number", *values))
        connection.execute(f"INSERT INTO runs ({columns}) VALUES ({', '.join('?' for _ in range(len(values) + 2))})", (execution_id, number, *values.values()))

    @staticmethod
    def _execution(row: sqlite3.Row, running_as_interrupted: bool) -> dict[str, Any]:
        execution = dict(row)
        execution["settings"] = json.loads(execution["settings"])
        if running_as_interrupted and execution["status"] == "running":
            execution["status"] = "interrupted"
        return execution

    @staticmethod
    def _run(row: sqlite3.Row, running_as_interrupted: bool) -> dict[str, Any]:
        run = dict(row)
        run["command"] = json.loads(run["command"])
        if running_as_interrupted and run["status"] == "running":
            run["status"] = "interrupted"
        return run
