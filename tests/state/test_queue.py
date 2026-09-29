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


OLD = "2026-09-01T09:00:00+00:00"


class ParkedTests(QueueCase):
    """Milestone 05: a parked entry is finished, and retention keeps it, and its execution, until a resume of it has a
    succeeded run."""

    def finished(self, state: QueueState, *, resumes: int | None = None, succeeded: int = 0, failed: bool = False, ran: bool = True) -> tuple[int, int | None]:
        """A queue entry finished long before retention's cutoff, with its execution (unless ``ran`` is off); returns
        the queue number and the execution number."""
        new = NewQueueEntry(job_path="/jobs/walk.yaml", job_text="name: walk\n", config_file="base.yaml", config_text="model: m.ckpt\n", input_directory="/in", output_directory="/out", cooldown_default=None, settings=ExecutionSettings(output_directory="/out"), submitted_at=OLD, total_runs=5, resumes=resumes)
        entry = self.store.queue.submit(new)
        execution_number = None
        if ran:
            executions = self.store.executions
            execution_id = executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at=OLD, total_runs=5, settings=ExecutionSettings(output_directory="/out")))
            for number in range(1, succeeded + 2 if failed else succeeded + 1):
                executions.start_run(execution_id, number, NewRun(pair="p", positive="text", started_at=OLD))
                executions.finish_run(execution_id, number, status="failed" if number > succeeded else "succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
            executions.finish(execution_id, status=str(state), exit_code=0, signal=None, finished_at=OLD)
            execution_number = executions.number_of(execution_id)
            assert execution_number is not None
            self.store.queue.link_execution(entry.id, execution_number)
        self.store.queue.finish(entry.id, state=state, finished_at=OLD)
        return entry.queue_number, execution_number

    def exists(self, queue_number: int, execution_number: int | None) -> tuple[bool, bool]:
        return self.store.queue.by_number(queue_number) is not None, execution_number is not None and self.store.executions.by_number(execution_number) is not None

    def test_a_parked_entry_is_finished_and_listed_with_the_finished_ones(self) -> None:
        queue_number, _ = self.finished(QueueState.PARKED, succeeded=3)
        [row] = self.store.queue.list_finished(limit=50, offset=0)
        self.assertEqual((row.queue_number, row.state, row.finished, row.succeeded), (queue_number, "parked", True, 3))
        self.assertEqual([row.queue_number for row in self.store.queue.list_finished(limit=50, offset=0, state="parked")], [queue_number])

    def test_retention_keeps_a_parked_entry_and_its_execution_until_a_resume_succeeds_a_run(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        other = self.finished(QueueState.INTERRUPTED, succeeded=3)
        self.store.prune()
        self.assertEqual(self.exists(*parked), (True, True))
        self.assertEqual(self.exists(*other), (False, False))
        # A resume cancelled before it ran, and a resume of that whose first run failed: the chain still walks back to
        # the parked entry, so the resumes between are kept with it.
        cancelled = self.finished(QueueState.CANCELLED, resumes=parked[0], ran=False)
        self.store.prune()
        self.assertEqual(self.exists(*parked), (True, True))
        self.assertTrue(self.exists(*cancelled)[0])
        failed = self.finished(QueueState.FAILED, resumes=cancelled[0], failed=True)
        self.store.prune()
        self.assertEqual(self.exists(*parked), (True, True))
        self.assertTrue(self.exists(*cancelled)[0])
        self.assertEqual(self.exists(*failed), (True, True))
        # A succeeded run further down the chain, not only in the parked entry's own resume, lets them all go, in the
        # same prune as the resume itself.
        resumed = self.finished(QueueState.SUCCEEDED, resumes=failed[0], succeeded=2)
        self.store.prune()
        self.assertEqual(self.exists(*parked), (False, False))
        self.assertFalse(self.exists(*cancelled)[0])
        self.assertEqual(self.exists(*failed), (False, False))
        self.assertEqual(self.exists(*resumed), (False, False))

    def test_a_parked_resume_keeps_its_own_chain_once_its_ancestor_is_let_go(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        # A resume that itself parked has a succeeded run, so it lets its ancestor go, and is kept in its place.
        parked_again = self.finished(QueueState.PARKED, resumes=parked[0], succeeded=1)
        failed = self.finished(QueueState.FAILED, resumes=parked_again[0], failed=True)
        self.store.prune()
        self.assertEqual(self.exists(*parked), (False, False))
        self.assertEqual(self.exists(*parked_again), (True, True))
        self.assertEqual(self.exists(*failed), (True, True))

    def test_a_recent_resume_with_a_succeeded_run_lets_an_old_parked_entry_go(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        new = NewQueueEntry(job_path="/jobs/walk.yaml", job_text="name: walk\n", config_file="base.yaml", config_text="model: m.ckpt\n", input_directory="/in", output_directory="/out", cooldown_default=None, settings=ExecutionSettings(output_directory="/out"), submitted_at="2026-09-29T09:00:00+00:00", total_runs=5, resumes=parked[0])
        running = self.store.queue.submit(new)
        self.store.queue.link_execution(running.id, self.start_execution(total_runs=5, first_run=4, succeeded=1))
        self.store.prune()
        self.assertEqual(self.exists(*parked), (False, False))
        self.assertIsNotNone(self.store.queue.get(running.id))

    def delete(self, execution_number: int | None) -> None:
        assert execution_number is not None
        self.assertEqual([deleted.number for deleted in self.store.executions.delete([execution_number], in_use={}).deleted], [execution_number])

    def test_a_parked_chain_goes_once_the_parked_entrys_execution_is_deleted(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        self.delete(parked[1])
        self.store.prune()
        self.assertFalse(self.exists(*parked)[0])

    def test_a_parked_chain_goes_once_a_finished_resume_below_it_links_a_deleted_execution(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        failed = self.finished(QueueState.FAILED, resumes=parked[0], failed=True)
        self.store.prune()
        self.assertEqual((self.exists(*parked), self.exists(*failed)), ((True, True), (True, True)))
        self.delete(failed[1])
        self.store.prune()
        self.assertEqual((self.exists(*parked), self.exists(*failed)[0]), ((False, False), False))

    def test_a_running_resume_linking_a_row_not_yet_created_keeps_the_chain(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        new = NewQueueEntry(job_path="/jobs/walk.yaml", job_text="name: walk\n", config_file="base.yaml", config_text="model: m.ckpt\n", input_directory="/in", output_directory="/out", cooldown_default=None, settings=ExecutionSettings(output_directory="/out"), submitted_at=OLD, total_runs=5, resumes=parked[0])
        running = self.store.queue.submit(new)
        self.store.queue.claim_oldest(NOW)
        # Linked before JobStarted creates the row.
        self.store.queue.link_execution(running.id, self.store.executions.reserve_number())
        self.store.prune()
        self.assertEqual(self.exists(*parked), (True, True))

    def test_resume_links_read_each_entrys_execution(self) -> None:
        parked = self.finished(QueueState.PARKED, succeeded=3)
        failed = self.finished(QueueState.FAILED, resumes=parked[0], failed=True)
        self.delete(failed[1])
        first, second = self.store.queue.resume_links()
        self.assertEqual((first.queue_number, first.state, first.execution_number, first.execution_exists, first.execution_total_runs, first.last_succeeded), (parked[0], "parked", parked[1], True, 5, 3))
        self.assertEqual((second.resumes, second.execution_exists, second.last_succeeded), (parked[0], False, None))


class SettingsTests(QueueCase):
    def test_delete_removes_a_key_and_a_missing_key_is_no_error(self) -> None:
        self.store.settings.set("queue_hold", "{}")
        self.store.settings.delete("queue_hold")
        self.assertIsNone(self.store.settings.get("queue_hold"))
        self.store.settings.delete("queue_hold")
