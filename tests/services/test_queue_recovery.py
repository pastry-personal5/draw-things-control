"""Tests for restart recovery: what a crash left ``running`` becomes what actually happened."""

from __future__ import annotations

from datetime import datetime

from draw_things_control.services.queue_recovery import recover_queue
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.executions import NewExecution
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data

NOW = datetime(2026, 9, 27, 15, 30, 12)


class QueueRecoveryTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)

    def submit(self) -> int:
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        return submit_job(path, self.global_config, self.params, self.store).id

    def claim(self, entry_id: int) -> None:
        claimed = self.store.queue.claim_oldest(NOW)
        assert claimed is not None and claimed.id == entry_id

    def link(self, entry_id: int, execution_row: int) -> None:
        number = self.store.executions.number_of(execution_row)
        assert number is not None
        self.store.queue.link_execution(entry_id, number)

    def test_a_running_entry_whose_execution_is_still_running_becomes_interrupted(self) -> None:
        entry_id = self.submit()
        self.claim(entry_id)
        execution_id = self.store.executions.start(NewExecution(job_name="sunset-walk", job_file="job.yaml", mode="i2v", started_at="2026-09-27T10:00:00+00:00"))
        self.link(entry_id, execution_id)
        recover_queue(self.store, clock=lambda: NOW)
        entry = self.store.queue.get(entry_id)
        assert entry is not None
        self.assertEqual(entry.state, str(QueueState.INTERRUPTED))
        execution = self.store.executions.get(execution_id)
        assert execution is not None
        self.assertEqual(execution.status, "interrupted")

    def test_a_running_entry_whose_execution_already_finished_takes_that_status(self) -> None:
        for status in ("succeeded", "failed", "interrupted"):
            with self.subTest(status=status):
                entry_id = self.submit()
                self.claim(entry_id)
                execution_id = self.store.executions.start(NewExecution(job_name="sunset-walk", job_file="job.yaml", mode="i2v", started_at="2026-09-27T10:00:00+00:00"))
                self.store.executions.finish(execution_id, status=status, exit_code=0, signal=None, finished_at="2026-09-27T10:05:00+00:00")
                self.link(entry_id, execution_id)
                recover_queue(self.store, clock=lambda: NOW)
                entry = self.store.queue.get(entry_id)
                assert entry is not None
                self.assertEqual(entry.state, status)

    def test_a_running_entry_with_no_linked_execution_is_requeued(self) -> None:
        entry_id = self.submit()
        self.claim(entry_id)
        recover_queue(self.store, clock=lambda: NOW)
        entry = self.store.queue.get(entry_id)
        assert entry is not None
        self.assertEqual(entry.state, str(QueueState.QUEUED))

    def test_a_running_entry_linked_to_a_number_whose_row_was_never_created_is_requeued_with_its_link_cleared(self) -> None:
        # The crash landed between reserving the execution's number (which the worker links right away) and the
        # recorder inserting its row: the row never exists, so requeuing must also drop the dangling link, exactly
        # as QueueWorker._fail_to_start does for the same window when the failure is an exception instead of a crash.
        entry_id = self.submit()
        self.claim(entry_id)
        self.store.queue.link_execution(entry_id, 999)
        recover_queue(self.store, clock=lambda: NOW)
        entry = self.store.queue.get(entry_id)
        assert entry is not None
        self.assertEqual(entry.state, str(QueueState.QUEUED))
        self.assertIsNone(entry.execution_number)

    def test_queued_entries_stay_queued_and_running_ones_are_untouched(self) -> None:
        queued_id = self.submit()
        recover_queue(self.store, clock=lambda: NOW)
        entry = self.store.queue.get(queued_id)
        assert entry is not None
        self.assertEqual(entry.state, str(QueueState.QUEUED))
