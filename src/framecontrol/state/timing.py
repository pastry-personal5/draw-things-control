"""Reads and writes the ``run_stages`` and ``run_timing`` tables: what each run's stages took, kept with its execution."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from framecontrol.jobs.timing.model import StageSpan, thin_marks
from framecontrol.state.database import Database

LIVE = "live"
LOG = "log"


@dataclass(frozen=True)
class StageRow:
    """One span of a run, as the store keeps it."""

    run: int
    stage: str
    phase: str | None
    source: str
    start: float
    end: float
    done: int | None
    total: int | None
    unit: str | None
    marks: tuple[tuple[float, int], ...]
    # The first mark's offset is an estimate (a backfill cannot recover when a phase's first reading was written).
    uncertain: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> StageRow:
        entries = json.loads(row["marks"])
        return cls(
            run=row["run"],
            stage=row["stage"],
            phase=row["phase"],
            source=row["source"],
            start=row["start_offset"],
            end=row["end_offset"],
            done=row["units_done"],
            total=row["units_total"],
            unit=row["unit"],
            marks=tuple((float(entry[0]), int(entry[1])) for entry in entries),
            uncertain=bool(entries) and len(entries[0]) > 2,
        )


@dataclass(frozen=True)
class RunTimingRow:
    """What a run was judged by, and whether its timing is whole."""

    run: int
    backend: str
    pipeline: str | None
    workload: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    fidelity: str = LIVE
    complete: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RunTimingRow:
        return cls(run=row["run"], backend=row["backend"], pipeline=row["pipeline"], workload=json.loads(row["workload"]), environment=json.loads(row["environment"]), fidelity=row["fidelity"], complete=bool(row["complete"]))


@dataclass(frozen=True)
class CompleteTiming:
    """A complete run as the forecast reads it: its row, the frames its output measured, and its stages in time order."""

    # The execution's public number (E0047 is 47), which is how history orders and groups.
    execution: int
    run: int
    backend: str
    pipeline: str | None
    workload: dict[str, Any]
    environment: dict[str, Any]
    fidelity: str
    output_frames: int | None
    stages: tuple[StageRow, ...]


def _marks_json(span: StageSpan) -> str:
    marks = [[offset, done] for offset, done in thin_marks(span.marks)]
    if span.uncertain and marks:
        marks[0].append(1)
    return json.dumps(marks)


class TimingRepository:
    """Reads and writes the ``run_stages`` and ``run_timing`` tables."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def start(self, execution_id: int, run: int, backend: str, pipeline: str | None, workload: dict[str, Any], environment: dict[str, Any], *, fidelity: str = LIVE) -> None:
        """Record what run ``run`` is judged by, incomplete until ``finish`` says otherwise."""
        with self._database.transaction() as connection:
            self._put_timing(connection, execution_id, run, backend, pipeline, workload, environment, fidelity, False)

    def finish(self, execution_id: int, run: int, *, complete: bool, environment: dict[str, Any] | None = None) -> None:
        """Mark run ``run``'s timing whole or not, adding ``environment`` (the load it ended under) to what was recorded."""
        with self._database.transaction() as connection:
            row = connection.execute("SELECT environment FROM run_timing WHERE execution_id = ? AND run = ?", (execution_id, run)).fetchone()
            if row is None:
                return
            merged = {**json.loads(row["environment"]), **(environment or {})}
            connection.execute("UPDATE run_timing SET complete = ?, environment = ? WHERE execution_id = ? AND run = ?", (int(complete), json.dumps(merged), execution_id, run))

    def add_stage(self, execution_id: int, run: int, span: StageSpan) -> None:
        """Keep one finished span of run ``run``."""
        with self._database.transaction() as connection:
            self._put_stage(connection, execution_id, run, span)

    def replace_run(self, execution_id: int, run: int, *, backend: str, pipeline: str | None, workload: dict[str, Any], environment: dict[str, Any], fidelity: str, complete: bool, spans: Iterable[StageSpan]) -> None:
        """Keep a run's timing rebuilt as a whole: its row and its spans, replacing any it had, in one transaction."""
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM run_stages WHERE execution_id = ? AND run = ?", (execution_id, run))
            self._put_timing(connection, execution_id, run, backend, pipeline, workload, environment, fidelity, complete)
            for span in spans:
                self._put_stage(connection, execution_id, run, span)

    def stages_of(self, execution_id: int, run: int) -> list[StageRow]:
        """Run ``run``'s spans in the order they start (work done before the run started first)."""
        rows = self._database.connection().execute("SELECT * FROM run_stages WHERE execution_id = ? AND run = ? ORDER BY start_offset, end_offset, id", (execution_id, run)).fetchall()
        return [StageRow.from_row(row) for row in rows]

    def timing_of(self, execution_id: int, run: int) -> RunTimingRow | None:
        row = self._database.connection().execute("SELECT * FROM run_timing WHERE execution_id = ? AND run = ?", (execution_id, run)).fetchone()
        return RunTimingRow.from_row(row) if row is not None else None

    def complete_runs(self) -> tuple[list[CompleteTiming], int]:
        """Every complete run with its stages, oldest first, and the number of rows that could not be read (a column that is not
        what it should be) and were left out: the whole history in two queries."""
        connection = self._database.connection()
        heads = connection.execute("SELECT t.execution_id, e.execution_number, t.run, t.backend, t.pipeline, t.workload, t.environment, t.fidelity, r.output_frames FROM run_timing t JOIN executions e ON e.id = t.execution_id LEFT JOIN runs r ON r.execution_id = t.execution_id AND r.number = t.run WHERE t.complete = 1 ORDER BY e.execution_number, t.run").fetchall()
        stage_rows = connection.execute("SELECT s.* FROM run_stages s JOIN run_timing t ON t.execution_id = s.execution_id AND t.run = s.run WHERE t.complete = 1 ORDER BY s.execution_id, s.run, s.start_offset, s.end_offset, s.id").fetchall()
        by_run: dict[tuple[int, int], list[sqlite3.Row]] = defaultdict(list)
        for row in stage_rows:
            by_run[(row["execution_id"], row["run"])].append(row)
        runs: list[CompleteTiming] = []
        skipped = 0
        for head in heads:
            try:
                workload, environment = json.loads(head["workload"]), json.loads(head["environment"])
                if not isinstance(workload, dict) or not isinstance(environment, dict):
                    raise ValueError("workload and environment are mappings")
                stages = tuple(StageRow.from_row(row) for row in by_run.get((head["execution_id"], head["run"]), ()))
            except (ValueError, TypeError, KeyError, IndexError):
                skipped += 1
                continue
            runs.append(CompleteTiming(head["execution_number"], head["run"], head["backend"], head["pipeline"], workload, environment, head["fidelity"], head["output_frames"], stages))
        return runs, skipped

    def completion_mark(self) -> tuple[int, tuple[int, int] | None]:
        """How many complete runs there are and the newest, as (execution number, run): what a fit made from them is cached
        by, since a run that completes adds one and pruning removes some."""
        connection = self._database.connection()
        count = connection.execute("SELECT COUNT(*) FROM run_timing WHERE complete = 1").fetchone()[0]
        newest = connection.execute("SELECT e.execution_number, t.run FROM run_timing t JOIN executions e ON e.id = t.execution_id WHERE t.complete = 1 ORDER BY e.execution_number DESC, t.run DESC LIMIT 1").fetchone()
        return count, (newest["execution_number"], newest["run"]) if newest is not None else None

    def has_timing(self, execution_id: int) -> bool:
        """Whether any run of the execution has timing recorded."""
        return self._database.connection().execute("SELECT 1 FROM run_timing WHERE execution_id = ? LIMIT 1", (execution_id,)).fetchone() is not None

    @staticmethod
    def _put_timing(connection: sqlite3.Connection, execution_id: int, run: int, backend: str, pipeline: str | None, workload: dict[str, Any], environment: dict[str, Any], fidelity: str, complete: bool) -> None:
        connection.execute(
            "INSERT INTO run_timing (execution_id, run, backend, pipeline, workload, environment, fidelity, complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (execution_id, run) DO UPDATE SET backend = excluded.backend, pipeline = excluded.pipeline, workload = excluded.workload, environment = excluded.environment, fidelity = excluded.fidelity, complete = excluded.complete",
            (execution_id, run, backend, pipeline, json.dumps(workload, sort_keys=True), json.dumps(environment, sort_keys=True), fidelity, int(complete)),
        )

    @staticmethod
    def _put_stage(connection: sqlite3.Connection, execution_id: int, run: int, span: StageSpan) -> None:
        connection.execute(
            "INSERT INTO run_stages (execution_id, run, stage, phase, source, start_offset, end_offset, units_done, units_total, unit, marks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (execution_id, run, span.stage, span.phase, span.source, span.start, span.end, span.done, span.total, span.unit, _marks_json(span)),
        )
