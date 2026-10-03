"""The audit log: one row for every request the HTTP API's rules cover (submit, cancel, resume, and, from Milestone
07, create_job, replace_job, delete_job), refused ones included. Never pruned by history_retention_days: its rows are
small, and it is the record of what agents did (owner decision, phase-3-changelog.md)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from draw_things_control.state.database import Database
from draw_things_control.state.execution_rows import epoch


@dataclass(frozen=True)
class AuditRow:
    """One audit log entry."""

    id: int
    at: str
    action: str
    target: str | None
    outcome: str
    caller: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AuditRow:
        return cls(id=row["id"], at=row["at"], action=row["action"], target=row["target"], outcome=row["outcome"], caller=row["caller"])


class AuditRepository:
    """Reads and writes the ``audit_log`` table. Only the server writes to it, and nothing deletes from it."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, *, action: str, target: str | None, outcome: str, caller: str, at: str) -> AuditRow:
        """Insert one entry; ``at`` is a local ISO 8601 timestamp with an offset (``core.clock.local_timestamp``), computed by the caller, as every other repository's timestamp is."""
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO audit_log (at, at_epoch, action, target, outcome, caller) VALUES (?, ?, ?, ?, ?, ?)",
                (at, epoch(at), action, target[:256] if target is not None else None, outcome, caller),
            )
            row = connection.execute("SELECT * FROM audit_log WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return AuditRow.from_row(row)

    def page(self, *, limit: int, offset: int = 0) -> list[AuditRow]:
        """Up to ``limit`` entries from ``offset``, newest first."""
        rows = self._database.connection().execute("SELECT * FROM audit_log ORDER BY at_epoch DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [AuditRow.from_row(row) for row in rows]
