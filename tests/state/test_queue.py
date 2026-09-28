"""Tests for the queue repository's Milestone 03 additions: total_runs, and the joined succeeded count."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from draw_things_control.state.execution_rows import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import NewQueueEntry, QueueState
from draw_things_control.state.schema import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_VERSION
from draw_things_control.state.store import Store, StoreMode

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "dtc.db"
        self.store = Store.open(self.path, mode=StoreMode.RUN, retention_days=14, clock=lambda: NOW)
        self.addCleanup(self.store.close)

    def entry(self, *, total_runs: int = 3) -> int:
        new = NewQueueEntry(
            job_path="/jobs/walk.yaml",
            job_text="name: walk\n",
            config_file="base.yaml",
            config_text="model: m.ckpt\n",
            input_directory="/in",
            output_directory="/out",
            cooldown_default=None,
            settings=ExecutionSettings(output_directory="/out"),
            submitted_at="2026-09-29T09:00:00+00:00",
            total_runs=total_runs,
        )
        return self.store.queue.submit(new).id

    def start_execution(self, *, total_runs: int, first_run: int = 1, succeeded: int) -> int:
        execution_id = self.store.executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at="2026-09-29T09:00:00+00:00", total_runs=total_runs, first_run=first_run, settings=ExecutionSettings(output_directory="/out")))
        for offset in range(succeeded):
            number = first_run + offset
            self.store.executions.start_run(execution_id, number, NewRun(pair="p", positive="text", started_at="2026-09-29T09:00:00+00:00"))
            self.store.executions.finish_run(execution_id, number, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
        number = self.store.executions.number_of(execution_id)
        assert number is not None
        return number


class TotalRunsTests(QueueCase):
    def test_total_runs_is_stored_and_read_back_by_every_reader(self) -> None:
        entry_id = self.entry(total_runs=7)
        fetched = self.store.queue.get(entry_id)
        assert fetched is not None
        self.assertEqual(fetched.total_runs, 7)
        [active] = self.store.queue.list_active()
        self.assertEqual(active.total_runs, 7)


class SucceededTests(QueueCase):
    def test_an_unlinked_entry_reports_zero_succeeded(self) -> None:
        self.entry()
        [active] = self.store.queue.list_active()
        self.assertEqual(active.succeeded, 0)

    def test_a_linked_entry_reports_succeeded_runs_from_run_one(self) -> None:
        entry_id = self.entry(total_runs=3)
        execution_number = self.start_execution(total_runs=3, succeeded=2)
        self.store.queue.link_execution(entry_id, execution_number)
        [active] = self.store.queue.list_active()
        self.assertEqual(active.succeeded, 2)

    def test_a_resumed_entrys_succeeded_count_carries_its_first_runs_offset(self) -> None:
        entry_id = self.entry(total_runs=5)
        # A resumed chain's execution starts at run 3 (two earlier runs already succeeded in an ancestor); one more
        # succeeds here, so this entry's true progress is 2 (from the ancestor) + 1 = 3, the same first_run - 1 +
        # succeeded convention tui/text/history.py and JobLogWriter use.
        execution_number = self.start_execution(total_runs=5, first_run=3, succeeded=1)
        self.store.queue.link_execution(entry_id, execution_number)
        [active] = self.store.queue.list_active()
        self.assertEqual(active.succeeded, 3)

    def test_list_finished_computes_the_same_succeeded_count(self) -> None:
        entry_id = self.entry(total_runs=3)
        execution_number = self.start_execution(total_runs=3, succeeded=3)
        self.store.queue.link_execution(entry_id, execution_number)
        self.store.queue.finish(entry_id, state=QueueState.SUCCEEDED, finished_at="2026-09-29T09:05:00+00:00")
        [finished] = self.store.queue.list_finished(limit=50, offset=0)
        self.assertEqual((finished.state, finished.succeeded), ("succeeded", 3))


class QueryCountTests(QueueCase):
    def test_list_active_issues_one_query_regardless_of_row_count(self) -> None:
        for _ in range(5):
            entry_id = self.entry(total_runs=2)
            execution_number = self.start_execution(total_runs=2, succeeded=1)
            self.store.queue.link_execution(entry_id, execution_number)
        statements: list[str] = []
        connection = self.store._database.connection()
        connection.set_trace_callback(statements.append)
        try:
            rows = self.store.queue.list_active()
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(len(rows), 5)
        selects = [statement for statement in statements if statement.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1)


class SchemaSixMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "v5.db"

    def test_a_pre_schema_6_row_backfills_total_runs_from_its_linked_execution(self) -> None:
        connection = sqlite3.connect(self.path)
        for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5):
            for statement in schema.split(";\n"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("PRAGMA user_version = 5")
        connection.execute("INSERT INTO executions (job_name, job_file, mode, status, started_at, started_epoch, execution_number, total_runs) VALUES ('walk', 'walk.yaml', 'i2v', 'succeeded', '2026-09-29T09:00:00+00:00', 1, 1, 4)")
        connection.execute("INSERT INTO queue (queue_number, job_path, job_text, config_file, config_text, input_directory, output_directory, state, submitted_at, submitted_epoch, execution_number) VALUES (1, '/jobs/walk.yaml', 'name: walk', 'base.yaml', 'model: m', '/in', '/out', 'succeeded', '2026-09-29T09:00:00+00:00', 1, 1)")
        connection.execute("INSERT INTO queue (queue_number, job_path, job_text, config_file, config_text, input_directory, output_directory, state, submitted_at, submitted_epoch) VALUES (2, '/jobs/wave.yaml', 'name: wave', 'base.yaml', 'model: m', '/in', '/out', 'queued', '2026-09-29T09:00:00+00:00', 1)")
        connection.commit()
        connection.close()
        store = Store.open(self.path, mode=StoreMode.WRITE, retention_days=0, clock=lambda: NOW)
        self.addCleanup(store.close)
        self.assertEqual(store._database.connection().execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        linked = store.queue.by_number(1)
        never_started = store.queue.by_number(2)
        assert linked is not None and never_started is not None
        self.assertEqual(linked.total_runs, 4)
        self.assertIsNone(never_started.total_runs)
