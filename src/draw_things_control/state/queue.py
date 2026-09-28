"""The persistent job queue: a snapshot of what will run, its state, and the ``queue`` table's repository."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from draw_things_control.core.clock import local_timestamp
from draw_things_control.state.database import Database, next_number
from draw_things_control.state.execution_rows import SUCCEEDED_COUNT, ExecutionSettings, epoch
from draw_things_control.state.ids import queue_id_text


class QueueState(StrEnum):
    """How a queue entry stands or ended. The words are what the state store keeps."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


# Entries in one of these states never run again on their own; they are what a resume, a cancel, or history retention act on.
FINISHED_STATES = (QueueState.SUCCEEDED, QueueState.FAILED, QueueState.CANCELLED, QueueState.INTERRUPTED)


@dataclass(frozen=True)
class NewQueueEntry:
    """A queue entry to insert: the job's snapshot, and, for a resume, the resolved resume point."""

    job_path: str
    job_text: str
    config_file: str
    config_text: str
    input_directory: str
    output_directory: str
    # The global configuration's cooldown default, as it read at submission; None when it sets none.
    cooldown_default: dict[str, Any] | None
    settings: ExecutionSettings
    submitted_at: str
    # The whole chain's run count (job.run_count), set once at submission or resume, so a queued entry -- or one
    # with no linked execution yet -- can still show "run 0/N" (Milestone 03); the same value JobFinished.total_runs
    # carries, unaffected by where a resume starts.
    total_runs: int
    # The queue number (Q0007) this entry resumes, the execution its resume point's last succeeded run came from
    # (resolved once, at resume()), and the resume point itself; None for a plain submission.
    resumes: int | None = None
    resumes_execution: int | None = None
    resume_first_run: int | None = None
    resume_input: str | None = None
    resume_seed: int | None = None


@dataclass(frozen=True)
class QueueRow:
    """One stored queue entry."""

    id: int
    queue_number: int
    job_path: str
    job_text: str
    config_file: str
    config_text: str
    input_directory: str
    output_directory: str
    cooldown_default: dict[str, Any] | None
    settings: ExecutionSettings
    state: str
    submitted_at: str
    submitted_epoch: float
    started_at: str | None
    started_epoch: float | None
    finished_at: str | None
    finished_epoch: float | None
    execution_number: int | None
    resumes: int | None
    resumes_execution: int | None
    resume_first_run: int | None
    resume_input: str | None
    resume_seed: int | None
    error: str | None
    # The whole chain's run count (schema 6); None on a pre-migration row with no linked execution. How many of
    # those runs have succeeded so far, 0 until the entry is linked to an execution: set only by a query that joins
    # in the linked execution (list_active, list_finished), 0 from get()/by_number()/list(), which do not.
    total_runs: int | None = None
    succeeded: int = 0

    @property
    def label(self) -> str:
        """The entry's ID as people see it (Q0007)."""
        return queue_id_text(self.queue_number)

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> QueueRow:
        cooldown_default = json.loads(row["cooldown_default"]) if row["cooldown_default"] else None
        # "succeeded" (the joined execution's SUCCEEDED_COUNT) and "exec_first_run" (its own first_run) are present
        # only from a query that joins in the linked execution (list_active, list_finished); get(), by_number(), and
        # list() select the queue table alone. The formula collapses to 0 on its own when there is no link: an
        # unmatched LEFT JOIN leaves exec_first_run NULL and the correlated succeeded-count 0.
        succeeded_count = row["succeeded"] if "succeeded" in row.keys() else 0
        exec_first_run = row["exec_first_run"] if "exec_first_run" in row.keys() else None
        return cls(
            id=row["id"],
            queue_number=row["queue_number"],
            job_path=row["job_path"],
            job_text=row["job_text"],
            config_file=row["config_file"],
            config_text=row["config_text"],
            input_directory=row["input_directory"],
            output_directory=row["output_directory"],
            cooldown_default=cooldown_default,
            settings=ExecutionSettings.from_json(row["settings"]),
            state=row["state"],
            submitted_at=row["submitted_at"],
            submitted_epoch=row["submitted_epoch"],
            started_at=row["started_at"],
            started_epoch=row["started_epoch"],
            finished_at=row["finished_at"],
            finished_epoch=row["finished_epoch"],
            execution_number=row["execution_number"],
            resumes=row["resumes"],
            resumes_execution=row["resumes_execution"],
            resume_first_run=row["resume_first_run"],
            resume_input=row["resume_input"],
            resume_seed=row["resume_seed"],
            error=row["error"],
            total_runs=row["total_runs"],
            succeeded=(exec_first_run or 1) - 1 + succeeded_count,
        )


QUEUE_COLUMNS = ("job_path", "job_text", "config_file", "config_text", "input_directory", "output_directory", "total_runs", "resumes", "resumes_execution", "resume_first_run", "resume_input", "resume_seed")
# list_active, list_finished, and by_number all join in the linked execution to compute "succeeded"
# (QueueRow.from_row's formula) rather than one executions.by_number() lookup per row (list_active/list_finished:
# the same reasoning that moved _queued_count to a bare COUNT(*), since a list read can run on every relevant
# event) or per call (by_number: one row, so the extra join costs nothing that scale concern applies to, and keeps
# a single-entry read -- GET /queue/{id}, a cancel's own response, WatchQueueEntry -- agreeing with the list reads).
_JOINED_QUEUE_SELECT = f"SELECT queue.*, {SUCCEEDED_COUNT}, executions.first_run AS exec_first_run FROM queue LEFT JOIN executions ON executions.execution_number = queue.execution_number"


class QueueRepository:
    """Reads and writes the ``queue`` table. Order is first in, first out, by ``queue_number``; nothing reorders it."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def submit(self, new: NewQueueEntry) -> QueueRow:
        """Insert a ``queued`` entry with the next queue number; return it."""
        with self._database.transaction() as connection:
            number = next_number(connection, "queue")
            values: dict[str, Any] = {column: getattr(new, column) for column in QUEUE_COLUMNS}
            values["queue_number"] = number
            values["cooldown_default"] = json_dump(new.cooldown_default)
            values["settings"] = new.settings.to_json()
            values["state"] = str(QueueState.QUEUED)
            values["submitted_at"] = new.submitted_at
            values["submitted_epoch"] = epoch(new.submitted_at)
            columns = ", ".join(values)
            cursor = connection.execute(f"INSERT INTO queue ({columns}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
            return self._get(connection, int(cursor.lastrowid or 0))

    def get(self, entry_id: int) -> QueueRow | None:
        row = self._database.connection().execute("SELECT * FROM queue WHERE id = ?", (entry_id,)).fetchone()
        return QueueRow.from_row(row) if row is not None else None

    def by_number(self, queue_number: int) -> QueueRow | None:
        """By the entry's public number (Q0007): joined, like list_active/list_finished, so a single-entry read
        (``GET /queue/{id}``, a cancel's own response, ``WatchQueueEntry``) reports the same ``succeeded`` those do;
        one row, so the join costs nothing the scale concern above applies to."""
        row = self._database.connection().execute(f"{_JOINED_QUEUE_SELECT} WHERE queue.queue_number = ?", (queue_number,)).fetchone()
        return QueueRow.from_row(row) if row is not None else None

    def list(self, *, state: str | None = None) -> list[QueueRow]:
        """Every entry in ``state`` (or all of them), oldest first."""
        if state is None:
            rows = self._database.connection().execute("SELECT * FROM queue ORDER BY queue_number").fetchall()
        else:
            rows = self._database.connection().execute("SELECT * FROM queue WHERE state = ? ORDER BY queue_number", (str(state),)).fetchall()
        return [QueueRow.from_row(row) for row in rows]

    def list_active(self, *, state: str | None = None) -> list[QueueRow]:
        """Every queued or running entry (or, given ``state``, only the ones in it), oldest first: never paged
        (``GET /queue``, Milestone 02), since the queue itself is bounded by ``max_queued_jobs`` and only one entry
        is ever running at once. Ordering by ``queue_number`` alone already reads as 'the running entry, if any,
        then the queued ones in FIFO order': the worker always claims the smallest queue number among these, so a
        running entry's own number is the smallest of the set. ``state``, joined like the unfiltered read, is what
        ``GET /queue?state=queued`` reads (Milestone 03): the plain ``list(state=...)`` below is unjoined, and
        always reported ``succeeded`` as 0 for an active entry until this existed."""
        clauses = ["queue.state IN ('queued', 'running')"]
        values: list[Any] = []
        if state is not None:
            clauses.append("queue.state = ?")
            values.append(str(state))
        where = " AND ".join(clauses)
        rows = self._database.connection().execute(f"{_JOINED_QUEUE_SELECT} WHERE {where} ORDER BY queue.queue_number", values).fetchall()
        return [QueueRow.from_row(row) for row in rows]

    def list_finished(self, *, limit: int, offset: int, state: str | None = None) -> list[QueueRow]:
        """A page of finished entries (succeeded, failed, cancelled, interrupted), newest first: ``GET /queue``'s own
        history, which -- unlike the active entries above -- is not bounded by anything but
        ``history_retention_days`` (0 keeps it forever), so it is paged like ``GET /jobs``, ``/executions``, and
        ``/audit``."""
        clauses = ["queue.state IN ('succeeded', 'failed', 'cancelled', 'interrupted')"]
        values: list[Any] = []
        if state is not None:
            clauses.append("queue.state = ?")
            values.append(str(state))
        where = " AND ".join(clauses)
        rows = self._database.connection().execute(f"{_JOINED_QUEUE_SELECT} WHERE {where} ORDER BY queue.queue_number DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return [QueueRow.from_row(row) for row in rows]

    def count(self, *, state: str) -> int:
        """How many entries are in ``state``, without reading full rows (each carrying its job and base
        configuration's exact text): ``check_api_rules``'s own ``max_queued_jobs`` check reads only this."""
        row = self._database.connection().execute("SELECT COUNT(*) AS n FROM queue WHERE state = ?", (str(state),)).fetchone()
        return int(row["n"])

    def has_queued(self) -> bool:
        """Whether any entry is ``queued``, without reading full rows (each carrying its job and base configuration's
        exact text): the between-jobs wait polls this rather than ``list(state=...)`` for a plain non-empty check."""
        return self._database.connection().execute("SELECT 1 FROM queue WHERE state = 'queued' LIMIT 1").fetchone() is not None

    def claim_oldest(self, now: datetime) -> QueueRow | None:
        """Claim the oldest ``queued`` entry, marking it ``running``; None when there is none. One transaction, so a
        concurrent cancel cannot land between the check and the claim."""
        with self._database.transaction() as connection:
            row = connection.execute("SELECT id FROM queue WHERE state = 'queued' ORDER BY queue_number LIMIT 1").fetchone()
            if row is None:
                return None
            started_at = local_timestamp(now)
            connection.execute("UPDATE queue SET state = 'running', started_at = ?, started_epoch = ? WHERE id = ?", (started_at, epoch(started_at), row["id"]))
            return self._get(connection, int(row["id"]))

    def link_execution(self, entry_id: int, execution_number: int) -> None:
        with self._database.transaction() as connection:
            connection.execute("UPDATE queue SET execution_number = ? WHERE id = ?", (execution_number, entry_id))

    def finish(self, entry_id: int, *, state: QueueState, finished_at: str, error: str | None = None, clear_link: bool = False) -> None:
        """``clear_link`` also clears a linked execution number: for a job that failed before ``JobStarted`` ever ran,
        so the number it was given (before the row that would have used it existed) is never mistaken later for one
        that ran and was pruned."""
        with self._database.transaction() as connection:
            if clear_link:
                connection.execute("UPDATE queue SET state = ?, finished_at = ?, finished_epoch = ?, error = ?, execution_number = NULL WHERE id = ?", (str(state), finished_at, epoch(finished_at), error, entry_id))
            else:
                connection.execute("UPDATE queue SET state = ?, finished_at = ?, finished_epoch = ?, error = ? WHERE id = ?", (str(state), finished_at, epoch(finished_at), error, entry_id))

    def set_error(self, entry_id: int, error: str) -> None:
        """Attach an error message to an already-finished entry, without touching its state: an exception that
        escaped the job (a collaborator's bug, not a normal run failure) reaches here after ``JobFinished`` has
        already recorded the entry's real outcome."""
        with self._database.transaction() as connection:
            connection.execute("UPDATE queue SET error = ? WHERE id = ?", (error, entry_id))

    def cancel_queued(self, entry_id: int, *, now: datetime) -> bool:
        """Cancel the entry if it is still ``queued``; returns whether it was (False when the worker claimed it first)."""
        finished_at = local_timestamp(now)
        with self._database.transaction() as connection:
            cursor = connection.execute("UPDATE queue SET state = 'cancelled', finished_at = ?, finished_epoch = ? WHERE id = ? AND state = 'queued'", (finished_at, epoch(finished_at), entry_id))
            return cursor.rowcount > 0

    def requeue(self, entry_id: int) -> None:
        """Put a ``running`` entry back to ``queued``: nothing of it ran (a crash before ``JobStarted``). Also clears
        a linked execution number: a number can be reserved (and linked) before the crash that leaves this entry to
        requeue, and its row was then never created, so the link would otherwise dangle and later be mistaken for a
        pruned execution instead of one that never ran (mirrors ``finish``'s own ``clear_link``)."""
        with self._database.transaction() as connection:
            connection.execute("UPDATE queue SET state = 'queued', started_at = NULL, started_epoch = NULL, execution_number = NULL WHERE id = ?", (entry_id,))

    def prune(self, cutoff: float) -> int:
        """Delete entries finished before the epoch ``cutoff``; never a ``queued`` or ``running`` one."""
        with self._database.transaction() as connection:
            return connection.execute("DELETE FROM queue WHERE finished_epoch IS NOT NULL AND finished_epoch < ?", (cutoff,)).rowcount

    def _get(self, connection: sqlite3.Connection, entry_id: int) -> QueueRow:
        row = connection.execute("SELECT * FROM queue WHERE id = ?", (entry_id,)).fetchone()
        assert row is not None
        return QueueRow.from_row(row)


def json_dump(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None
