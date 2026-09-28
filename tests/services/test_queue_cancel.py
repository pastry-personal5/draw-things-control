"""Tests for cancelling a queue entry: queued, running, and refused-because-finished."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.core.errors import NotFoundError
from draw_things_control.core.run_lock import RunLock
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.services.queue_cancel import CancelRefused, cancel_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.services.queue_worker import QueueWorker
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor
from tests.jobs.test_executor import BlockingRunner, FakeResult, FakeRunner

NOW = datetime(2026, 9, 27, 15, 30, 12)


def extract_frame(_video: Path, png: Path) -> None:
    png.write_bytes(b"png")


class QueueCancelTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(), write_output=True)
        self.executor = job_executor(runner_factory=lambda arguments, *_r: self.next_runner(arguments), find_executable=lambda name: name, frame_extractor=extract_frame, require_ffmpeg=lambda: "ffmpeg", handle_signals=False, cooldown=lambda seconds: seconds)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        self.lock = RunLock("serve", directory=self.paths.state)
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        session = JobRunSession(self.paths, self.executor, self.global_config)
        self.worker = QueueWorker(self.store, session, self.executor, self.lock, self.paths, self.global_config, executable="draw-things-cli", shutdown_grace=1, clock=lambda: NOW, poll_interval=0.01)

    def submit(self) -> Any:
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        return submit_job(path, self.global_config, self.params, self.store)

    def entry(self, entry_id: int):
        row = self.store.queue.get(entry_id)
        assert row is not None
        return row

    @staticmethod
    def wait_until(condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for the condition")
            time.sleep(0.005)

    def test_a_queued_entry_is_cancelled_and_never_starts(self) -> None:
        entry = self.submit()
        cancel_entry(self.store, self.worker, entry.id, clock=lambda: NOW)
        self.assertEqual(self.entry(entry.id).state, str(QueueState.CANCELLED))
        self.assertFalse(self.worker.claim_and_run_one())

    def test_a_running_entry_is_cancelled(self) -> None:
        entry = self.submit()
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        thread = threading.Thread(target=self.worker.claim_and_run_one)
        thread.start()
        self.wait_until(lambda: self.worker.current_entry_id() is not None)
        cancel_entry(self.store, self.worker, entry.id, clock=lambda: NOW)
        thread.join(timeout=5)
        self.assertEqual(self.entry(entry.id).state, str(QueueState.CANCELLED))

    def test_a_finished_entry_is_refused_naming_its_state(self) -> None:
        entry = self.submit()
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(entry.id).state, str(QueueState.SUCCEEDED))
        with self.assertRaisesRegex(CancelRefused, "succeeded"):
            cancel_entry(self.store, self.worker, entry.id, clock=lambda: NOW)

    def test_a_second_cancel_on_an_entry_already_stopping_is_a_no_op(self) -> None:
        entry = self.submit()
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        thread = threading.Thread(target=self.worker.claim_and_run_one)
        thread.start()
        self.wait_until(lambda: self.worker.current_entry_id() is not None)
        cancel_entry(self.store, self.worker, entry.id, clock=lambda: NOW)
        cancel_entry(self.store, self.worker, entry.id, clock=lambda: NOW)
        thread.join(timeout=5)
        self.assertEqual(self.entry(entry.id).state, str(QueueState.CANCELLED))

    def test_an_unknown_entry_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            cancel_entry(self.store, self.worker, 999, clock=lambda: NOW)
