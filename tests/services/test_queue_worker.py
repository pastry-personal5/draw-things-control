"""Tests for the queue worker: claiming, running, cancelling, and the cooldown between queued jobs."""

from __future__ import annotations

import signal
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.cooldown import DEFAULT_COOLDOWN
from draw_things_control.core.run_lock import RunLock
from draw_things_control.services.generation_submit import GenerationSnapshot
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.services.queue_resume import resume_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.services.queue_worker import QueueWorker
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import NewQueueEntry, QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor
from tests.jobs.test_executor import BlockingRunner, FakeResult, FakeRunner

NOW = datetime(2026, 9, 27, 15, 30, 12)


def extract_frame(_video: Path, png: Path) -> None:
    png.write_bytes(b"png")


class SlowFakeRunner(FakeRunner):
    """A FakeRunner whose run takes a real, measurable amount of time, so ``record.seconds`` is not simply 0."""

    def __init__(self, arguments: DrawThingsGenerateArguments, delay: float) -> None:
        super().__init__(arguments, FakeResult(), write_output=True)
        self._delay = delay

    def run(self) -> FakeResult:
        time.sleep(self._delay)
        return super().run()


class QueueWorkerCase(JobTestCase):
    """A worker over a real store and a fake runner, and the helpers its tests share; no tests of its own."""

    def setUp(self) -> None:
        super().setUp()
        self.starts = 0
        self.runners: list[FakeRunner] = []
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(), write_output=True)
        self.executor = job_executor(runner_factory=self.create_runner, find_executable=lambda name: name, frame_extractor=extract_frame, require_ffmpeg=lambda: "ffmpeg", handle_signals=False, cooldown=lambda seconds: seconds)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        self.lock = RunLock("serve", directory=self.paths.state)
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.worker = self.build_worker()

    def build_worker(self, **overrides: Any) -> QueueWorker:
        session = JobRunSession(self.paths, self.executor, self.global_config)
        options: dict[str, Any] = {"executable": "draw-things-cli", "shutdown_grace": 1, "clock": lambda: NOW, "poll_interval": 0.01}
        options.update(overrides)
        return QueueWorker(self.store, session, self.executor, self.lock, self.paths, self.global_config, **options)

    def create_runner(self, arguments: DrawThingsGenerateArguments, *_rest: Any) -> FakeRunner:
        self.starts += 1
        runner = self.next_runner(arguments)
        self.runners.append(runner)
        return runner

    def submit(self, **changes: object) -> str:
        changes.setdefault("prompt_pairs", [{"name": "only", "positive": "text"}])
        path = self.write_job(job_data(**changes))
        return submit_job(path, self.global_config, self.params, self.store).label

    def entry(self, label: str):
        number = int(label[1:])
        row = self.store.queue.by_number(number)
        assert row is not None
        return row

    @staticmethod
    def wait_until(condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for the condition")
            time.sleep(0.005)

    def blocked_start(self):
        """Patches ``parse_snapshot`` in ``queue_worker`` so a claim blocks right after it (before the executor's
        ``begin()``); returns the events a test waits on and sets to release it."""
        from draw_things_control.services.queue_submit import parse_snapshot as real_parse_snapshot

        claimed = threading.Event()
        release = threading.Event()

        def blocking(entry: Any, global_config: Any, params_directory: Any):
            thunk = real_parse_snapshot(entry, global_config, params_directory)

            def parse() -> Any:
                claimed.set()
                assert release.wait(5), "the test never released the blocked claim"
                return thunk()

            return parse

        return mock.patch("draw_things_control.services.queue_worker.parse_snapshot", side_effect=blocking), claimed, release


class QueueWorkerTests(QueueWorkerCase):
    def test_a_cancel_pending_before_a_generation_token_begins_is_applied_when_it_does(self) -> None:
        self.worker._pending_cancel = True
        with mock.patch.object(self.executor, "cancel", return_value=True) as cancel:
            self.worker._generation_start_guard()
        self.assertTrue(self.worker._job_started)
        cancel.assert_called_once_with()

    def test_a_generation_entry_runs_once_and_records_a_generation_execution(self) -> None:
        self.output_directory.mkdir()
        snapshot = GenerationSnapshot(model="model.ckpt", output="cube.png", output_path=str(self.output_directory / "cube.png"), timeout=60, prompt="cube")
        entry = self.store.queue.submit(NewQueueEntry(job_path="", job_text="", config_file="", config_text="", input_directory=str(self.input_directory), output_directory=str(self.output_directory), cooldown_default=None, settings=ExecutionSettings(output_directory=str(self.output_directory)), submitted_at="2026-09-27T15:30:12+00:00", total_runs=1, kind="generate", snapshot=snapshot.to_json()))
        self.assertTrue(self.worker.claim_and_run_one())
        updated = self.store.queue.get(entry.id)
        assert updated is not None and updated.execution_number is not None
        self.assertEqual((updated.kind, updated.state), ("generate", str(QueueState.SUCCEEDED)))
        execution = self.store.executions.by_number(updated.execution_number)
        assert execution is not None
        self.assertEqual((execution.source_kind, execution.job_name, execution.total_runs, execution.runs[0].output), ("generate", "generate: cube.png", 1, str(self.output_directory / "cube.png")))
        self.assertEqual(self.starts, 1)

    def test_two_queued_jobs_run_in_order(self) -> None:
        first = self.submit(run_count=1)
        second = self.submit(run_count=1)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(first).state, str(QueueState.SUCCEEDED))
        self.assertEqual(self.entry(second).state, str(QueueState.QUEUED))
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(second).state, str(QueueState.SUCCEEDED))
        self.assertFalse(self.worker.claim_and_run_one())
        self.assertEqual(self.starts, 2)

    def test_a_queued_entry_is_cancelled_and_never_runs(self) -> None:
        label = self.submit(run_count=1)
        entry = self.entry(label)
        self.assertTrue(self.store.queue.cancel_queued(entry.id, now=NOW))
        self.assertFalse(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, str(QueueState.CANCELLED))
        self.assertEqual(self.starts, 0)

    def test_enqueue_inserts_and_publishes_the_queued_change_under_the_claim_lock(self) -> None:
        """The event-ordering fix (Milestone 02, phase-3 changelog 2026-09-28): enqueue's insert and its 'queued'
        publish happen under the same lock ``_claim_and_run_one`` takes to claim and register an entry, so a claim
        already blocked on it can never see the row -- and so never publish 'running' -- before this publish."""
        events: list[str] = []
        worker = self.build_worker(on_event=lambda kind, _data: events.append(kind))
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        lock_held_during_insert = []

        def insert():
            lock_held_during_insert.append(worker._state_lock.locked())
            return submit_job(path, self.global_config, self.params, self.store)

        entry = worker.enqueue(insert)
        self.assertEqual(lock_held_during_insert, [True])
        self.assertEqual(entry.state, str(QueueState.QUEUED))
        self.assertEqual(events, ["queue_entry_changed"])

    def test_cancel_queued_publishes_its_change_under_the_claim_lock(self) -> None:
        events: list[tuple[str, dict]] = []
        worker = self.build_worker(on_event=lambda kind, data: events.append((kind, data)))
        label = self.submit(run_count=1)
        self.assertTrue(worker.cancel_queued(self.entry(label).id, label))
        self.assertEqual(self.entry(label).state, str(QueueState.CANCELLED))
        self.assertEqual(events, [("queue_entry_changed", {"queue_id": label, "state": "cancelled"})])

    def test_cancel_queued_is_false_and_publishes_nothing_once_the_worker_has_claimed_it(self) -> None:
        events: list[tuple[str, dict]] = []
        worker = self.build_worker(on_event=lambda kind, data: events.append((kind, data)))
        label = self.submit(run_count=1)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertFalse(worker.cancel_queued(self.entry(label).id, label))
        self.assertEqual(events, [])

    def test_a_running_entry_is_cancelled_and_reads_cancelled_not_interrupted(self) -> None:
        label = self.submit(run_count=1)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        results = []

        def run_in_background() -> None:
            results.append(self.worker.claim_and_run_one())

        thread = threading.Thread(target=run_in_background)
        thread.start()
        self.wait_until(lambda: self.worker.current_entry_id() is not None)
        self.assertTrue(self.worker.cancel_running(self.entry(label).id))
        thread.join(timeout=5)
        self.assertEqual(results, [True])
        entry = self.entry(label)
        self.assertEqual(entry.state, str(QueueState.CANCELLED))
        # A cancel of a running entry always reads 'interrupted' in the history, as a Ctrl-C does.
        assert entry.execution_number is not None
        row_id = self.store.executions.row_of(entry.execution_number)
        assert row_id is not None
        execution = self.store.executions.get(row_id)
        assert execution is not None
        self.assertEqual(execution.status, "interrupted")

    def test_shutdown_between_the_claim_and_begin_stops_the_job_before_it_starts(self) -> None:
        label = self.submit(run_count=1)
        patcher, claimed, release = self.blocked_start()
        with patcher:
            thread = threading.Thread(target=self.worker.claim_and_run_one)
            thread.start()
            self.assertTrue(claimed.wait(5), "the claim never reached the blocked point")
            # worker.stop() only joins a thread started by worker.start(); here it just records the pending stop.
            self.worker.stop()
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.starts, 0)
        self.assertEqual(self.entry(label).state, str(QueueState.INTERRUPTED))

    def test_cancel_between_the_claim_and_the_start_stops_the_job_before_it_runs(self) -> None:
        label = self.submit(run_count=1)
        patcher, claimed, release = self.blocked_start()
        with patcher:
            thread = threading.Thread(target=self.worker.claim_and_run_one)
            thread.start()
            self.assertTrue(claimed.wait(5), "the claim never reached the blocked point")
            self.assertTrue(self.worker.cancel_running(self.entry(label).id))
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.starts, 0)
        entry = self.entry(label)
        self.assertEqual(entry.state, str(QueueState.CANCELLED))
        # A cancel of a running entry always reads 'interrupted' in the history, even one caught this early.
        assert entry.execution_number is not None
        row_id = self.store.executions.row_of(entry.execution_number)
        assert row_id is not None
        execution = self.store.executions.get(row_id)
        assert execution is not None
        self.assertEqual(execution.status, "interrupted")

    def test_a_run_killed_from_outside_reads_failed_on_the_entry_not_cancelled_or_interrupted(self) -> None:
        # Neither cancel_running() nor stop() asked for this stop, so the queue entry (unlike the execution, which
        # keeps its own "interrupted" for any stopped run, Ctrl-C included, since Phase 2) reads a plain failure.
        label = self.submit(run_count=1)
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(return_code=-9, termination_signal=signal.SIGKILL), write_output=False)
        self.assertTrue(self.worker.claim_and_run_one())
        entry = self.entry(label)
        self.assertEqual(entry.state, str(QueueState.FAILED))
        assert entry.execution_number is not None
        row_id = self.store.executions.row_of(entry.execution_number)
        assert row_id is not None
        execution = self.store.executions.get(row_id)
        assert execution is not None
        self.assertEqual(execution.status, "interrupted")

    def test_cancel_during_the_cooldown_between_runs_stops_the_job_and_reads_interrupted_in_history(self) -> None:
        label = self.submit(run_count=2, cooldown={"mode": "manual", "seconds": 900})
        entry_id = self.entry(label).id

        def cooldown_then_cancel(_seconds: float) -> float:
            # Runs synchronously inside the job's own cooldown-between-runs wait, on the same thread as the claim.
            self.worker.cancel_running(entry_id)
            return 0.0

        # JobExecutor takes its cooldown callable at construction; rebuild it (and the worker on top of it) with the fake.
        self.executor = job_executor(runner_factory=self.create_runner, find_executable=lambda name: name, frame_extractor=extract_frame, require_ffmpeg=lambda: "ffmpeg", handle_signals=False, cooldown=cooldown_then_cancel)
        self.worker = self.build_worker()
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, str(QueueState.CANCELLED))
        entry = self.entry(label)
        assert entry.execution_number is not None
        row_id = self.store.executions.row_of(entry.execution_number)
        assert row_id is not None
        execution = self.store.executions.get(row_id)
        assert execution is not None
        self.assertEqual(execution.status, "interrupted")

    def test_an_error_that_escapes_one_job_fails_only_that_entry(self) -> None:
        first = self.submit(run_count=1)
        second = self.submit(run_count=1)

        def broken(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            raise RuntimeError("boom")

        self.next_runner = broken
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(first).state, str(QueueState.FAILED))
        self.assertEqual(self.entry(first).error, "boom")
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(), write_output=True)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(second).state, str(QueueState.SUCCEEDED))

    def test_a_persistent_store_error_backs_off_instead_of_spinning(self) -> None:
        self.submit(run_count=1)
        with mock.patch.object(self.store.queue, "claim_oldest", side_effect=sqlite3.OperationalError("disk I/O error")):
            # False (not True): a real error must back off (the idle wait), never spin claim_oldest in a tight loop.
            self.assertFalse(self.worker.claim_and_run_one())
        self.assertEqual(self.starts, 0)

    def test_an_entry_claimed_right_as_shutdown_begins_is_requeued_untouched(self) -> None:
        label = self.submit(run_count=1)
        # No thread was started (self.worker.start() was never called), so stop() just records the pending stop and
        # returns at once: it does not block waiting for a thread that does not exist.
        self.worker.stop()
        self.assertTrue(self.worker.claim_and_run_one())
        entry = self.entry(label)
        self.assertEqual(entry.state, str(QueueState.QUEUED))
        self.assertIsNone(entry.execution_number)
        self.assertIsNone(entry.started_at)
        self.assertEqual(self.starts, 0)

    def test_no_wait_follows_a_success_with_an_empty_queue(self) -> None:
        calls: list[float] = []
        worker = self.build_worker(wait_between_jobs=calls.append)
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 30})
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(self.entry(first).state, str(QueueState.SUCCEEDED))
        self.assertEqual(calls, [])

    def test_no_wait_follows_a_failed_job_even_with_another_already_queued(self) -> None:
        calls: list[float] = []
        worker = self.build_worker(wait_between_jobs=calls.append)
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 30})
        second = self.submit(run_count=1)

        def broken(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            raise RuntimeError("boom")

        self.next_runner = broken
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(self.entry(first).state, str(QueueState.FAILED))
        self.assertEqual(self.entry(second).state, str(QueueState.QUEUED))
        self.assertEqual(calls, [])

    def test_a_shutdown_after_a_cancel_does_not_relabel_it_interrupted(self) -> None:
        label = self.submit(run_count=1)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        thread = threading.Thread(target=self.worker.claim_and_run_one)
        thread.start()
        self.wait_until(lambda: self.worker.current_entry_id() is not None)
        self.assertTrue(self.worker.cancel_running(self.entry(label).id))
        # A shutdown landing after the cancel must not relabel the entry 'interrupted': the cancel already asked first.
        self.worker.stop()
        thread.join(timeout=5)
        self.assertEqual(self.entry(label).state, str(QueueState.CANCELLED))

    def test_no_wait_follows_a_cancelled_job_even_with_another_already_queued(self) -> None:
        calls: list[float] = []
        worker = self.build_worker(wait_between_jobs=calls.append)
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 30})
        second = self.submit(run_count=1)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        thread = threading.Thread(target=worker.claim_and_run_one)
        thread.start()
        self.wait_until(lambda: worker.current_entry_id() is not None)
        self.assertTrue(worker.cancel_running(self.entry(first).id))
        thread.join(timeout=5)
        self.assertEqual(self.entry(first).state, str(QueueState.CANCELLED))
        self.assertEqual(self.entry(second).state, str(QueueState.QUEUED))
        self.assertEqual(calls, [])

    def test_a_wait_follows_a_success_using_the_jobs_resolved_cooldown_applied_to_its_last_runs_time(self) -> None:
        calls: list[float] = []
        worker = self.build_worker(wait_between_jobs=calls.append)
        self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 12.5})
        second = self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        # A manual cooldown ignores the run's own time, so the wait is exactly the configured seconds, called once.
        self.assertEqual(calls, [12.5])
        self.assertEqual(self.entry(second).state, str(QueueState.QUEUED))

    def test_state_and_cooldown_until_reflect_the_between_jobs_wait(self) -> None:
        observed: list[tuple[str, bool]] = []

        def spy(_seconds: float) -> None:
            observed.append((worker.state(), worker.cooldown_until() is not None))

        worker = self.build_worker(wait_between_jobs=spy)
        self.assertEqual((worker.state(), worker.cooldown_until()), ("idle", None))
        self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 12.5})
        self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(observed, [("cooling_down", True)])
        self.assertEqual((worker.state(), worker.cooldown_until()), ("idle", None))

    def test_current_run_tracks_the_run_number_and_its_elapsed_seconds(self) -> None:
        observed: list[tuple[int, bool]] = []
        real_runner = self.next_runner

        def spy(arguments: DrawThingsGenerateArguments):
            observed.append((self.worker.current_run()[0], self.worker.current_run()[1] >= 0))  # type: ignore[index]
            return real_runner(arguments)

        self.next_runner = spy
        self.assertIsNone(self.worker.current_run())
        self.submit(run_count=2)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(observed, [(1, True), (2, True)])
        self.assertIsNone(self.worker.current_run())

    def test_events_are_emitted_for_the_claim_job_events_and_finish(self) -> None:
        events: list[tuple[str, dict]] = []
        worker = self.build_worker(on_event=lambda kind, data: events.append((kind, data)))
        label = self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        kinds = [kind for kind, _data in events]
        self.assertEqual(kinds[0], "queue_entry_changed")
        self.assertEqual(events[0][1], {"queue_id": label, "state": "running"})
        self.assertIn("job_started", kinds)
        self.assertIn("run_started", kinds)
        self.assertIn("run_finished", kinds)
        self.assertIn("job_finished", kinds)
        self.assertEqual(kinds[-1], "queue_entry_changed")
        self.assertEqual(events[-1][1], {"queue_id": label, "state": "succeeded"})

    def test_wait_events_name_the_entry_and_the_cooldown_deadline(self) -> None:
        events: list[tuple[str, dict]] = []
        worker = self.build_worker(on_event=lambda kind, data: events.append((kind, data)), wait_between_jobs=lambda seconds: None)
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 12.5})
        self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        wait_events = [(kind, data) for kind, data in events if kind.startswith("queue_wait_")]
        self.assertEqual([kind for kind, _data in wait_events], ["queue_wait_started", "queue_wait_ended"])
        self.assertEqual(wait_events[0][1]["queue_id"], first)
        self.assertIn("cooldown_until", wait_events[0][1])
        self.assertEqual(wait_events[1][1], {"queue_id": first})

    def test_state_is_running_while_a_job_is_claimed(self) -> None:
        label = self.submit(run_count=1)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        thread = threading.Thread(target=self.worker.claim_and_run_one)
        thread.start()
        self.wait_until(lambda: self.worker.current_entry_id() is not None)
        self.assertEqual(self.worker.state(), "running")
        self.assertTrue(self.worker.cancel_running(self.entry(label).id))
        thread.join(timeout=5)

    def test_the_wait_is_the_autocooldowns_share_of_the_last_runs_actual_seconds(self) -> None:
        calls: list[float] = []
        worker = self.build_worker(wait_between_jobs=calls.append)
        # An auto cooldown (default ratio 0.5) applied to a run that took some measurable time: the FakeRunner sleeps
        # briefly so record.seconds is not simply 0, so this actually exercises "applied to its last run's time".
        self.next_runner = lambda arguments: SlowFakeRunner(arguments, delay=0.2)
        first = self.submit(run_count=1, cooldown={"mode": "auto"})
        self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        entry = self.entry(first)
        assert entry.execution_number is not None
        row_id = self.store.executions.row_of(entry.execution_number)
        assert row_id is not None
        execution = self.store.executions.get(row_id)
        assert execution is not None and execution.runs
        actual_seconds = execution.runs[-1].seconds
        assert actual_seconds is not None
        self.assertEqual(calls, [DEFAULT_COOLDOWN.wait_after(actual_seconds).seconds])

    def test_a_wait_actually_runs_before_the_next_entry_starts(self) -> None:
        self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 0.1})
        second = self.submit(run_count=1)
        self.assertTrue(self.worker.claim_and_run_one())
        # The default (real) wait already ran inside claim_and_run_one; the second entry starts only after it.
        self.assertEqual(self.entry(second).state, str(QueueState.QUEUED))
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(second).state, str(QueueState.SUCCEEDED))

    def test_a_resumed_entry_runs_from_its_resolved_resume_point(self) -> None:
        label = self.submit(run_count=3)
        entry = self.entry(label)
        claimed = self.store.queue.claim_oldest(NOW)
        assert claimed is not None and claimed.id == entry.id
        last_frame = self.output_directory / "last-frame-1.png"
        last_frame.parent.mkdir(parents=True, exist_ok=True)
        last_frame.write_bytes(b"png")
        execution_row = self.store.executions.start(NewExecution(job_name="sunset-walk", job_file="job.yaml", mode="i2v", started_at="2026-09-27T10:00:00+00:00", seed=99, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        self.store.executions.start_run(execution_row, 1, NewRun(pair="only", positive="text", started_at="2026-09-27T10:00:00+00:00", status="succeeded", output="run-1.mov", last_frame=last_frame.name))
        self.store.executions.finish(execution_row, status="interrupted", exit_code=None, signal=None, finished_at="2026-09-27T10:10:00+00:00")
        execution_number = self.store.executions.number_of(execution_row)
        assert execution_number is not None
        self.store.queue.link_execution(entry.id, execution_number)
        self.store.queue.finish(entry.id, state=QueueState.INTERRUPTED, finished_at="2026-09-27T10:10:00+00:00")
        resumed = resume_entry(self.store, entry.id, self.global_config, self.params, clock=lambda: NOW)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(resumed.label).state, str(QueueState.SUCCEEDED))
        self.assertEqual(self.starts, 2)
        first_run_arguments = self.runners[0].arguments
        self.assertEqual((first_run_arguments.image, first_run_arguments.seed), (last_frame, 99))

    def test_resuming_a_resume_starts_from_its_own_last_succeeded_run_not_the_original_chain(self) -> None:
        """A 7-run job fails at run 4; resuming it fails again at run 5; resuming that resume starts at run 5, from
        run 4's own last frame (recorded by the resumed execution itself), never from run 5's leftover file, and
        finishes the chain."""
        label = self.submit(run_count=7)
        first_pass = [0]

        def fail_on_run_4(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            first_pass[0] += 1
            if first_pass[0] == 4:
                return FakeRunner(arguments, FakeResult(return_code=3), write_output=False)
            return FakeRunner(arguments, FakeResult(), write_output=True)

        self.next_runner = fail_on_run_4
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, str(QueueState.FAILED))

        first_resume = resume_entry(self.store, self.entry(label).id, self.global_config, self.params, clock=lambda: NOW)
        self.assertEqual(first_resume.resume_first_run, 4)

        runners_before_second_pass = len(self.runners)
        second_pass = [0]

        def fail_on_run_5(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            second_pass[0] += 1
            # The resumed entry's own first call is job run 4; its second is job run 5. A failed run may still leave
            # a leftover output file (draw-things-cli writes before it necessarily exits 0), so this leaves one too,
            # to prove the next resume never starts from it.
            if second_pass[0] == 2:
                return FakeRunner(arguments, FakeResult(return_code=7), write_output=True)
            return FakeRunner(arguments, FakeResult(), write_output=True)

        self.next_runner = fail_on_run_5
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(first_resume.label).state, str(QueueState.FAILED))

        second_resume = resume_entry(self.store, first_resume.id, self.global_config, self.params, clock=lambda: NOW)
        # Never run 5's own leftover file: the resumed execution's only succeeded run is run 4.
        self.assertEqual(second_resume.resume_first_run, 5)
        assert second_resume.resume_input is not None
        self.assertTrue(second_resume.resume_input.endswith("-last-frame.png"))
        # The resumed execution's own run 4 (its first call, at runners_before_second_pass), never the original
        # chain's run 4, nor run 5's leftover file.
        run_4_output_call = self.runners[runners_before_second_pass]
        assert run_4_output_call.arguments.output is not None
        expected_last_frame = run_4_output_call.arguments.output.with_name(f"{run_4_output_call.arguments.output.stem}-last-frame.png")
        self.assertEqual(Path(second_resume.resume_input), expected_last_frame)

        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(), write_output=True)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(second_resume.label).state, str(QueueState.SUCCEEDED))

    def test_an_accepted_resume_survives_its_ancestors_history_being_pruned(self) -> None:
        label = self.submit(run_count=3)
        entry_id = self.entry(label).id
        call_count = [0]

        def fail_on_run_2(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            call_count[0] += 1
            if call_count[0] == 2:
                return FakeRunner(arguments, FakeResult(return_code=1), write_output=False)
            return FakeRunner(arguments, FakeResult(), write_output=True)

        self.next_runner = fail_on_run_2
        self.assertTrue(self.worker.claim_and_run_one())
        resumed = resume_entry(self.store, entry_id, self.global_config, self.params, clock=lambda: NOW)
        # Pruning everything finished (the ancestor's execution and queue entry alike) does not touch the accepted
        # resume: its resolved resume point was already stored on its own row.
        far_future = float("inf")
        self.store.executions.prune(far_future)
        self.store.queue.prune(far_future)
        self.assertIsNone(self.store.queue.get(entry_id))
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(), write_output=True)
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(resumed.label).state, str(QueueState.SUCCEEDED))

    def test_stop_cancels_a_running_job_ends_the_wait_and_joins_the_thread(self) -> None:
        label = self.submit(run_count=1)
        queued_after = self.submit(run_count=1)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        self.worker.start()
        # Wait for the run itself to actually start (the runner constructed), not merely the claim: a stop() that
        # lands between the claim and the run's own start is a different, separately tested race (see
        # test_shutdown_between_the_claim_and_begin_stops_the_job_before_it_starts).
        self.wait_until(lambda: self.starts >= 1)
        self.worker.stop()
        # Shutdown reads 'interrupted', not 'cancelled': the same stop, but the entry says which kind it was.
        self.assertEqual(self.entry(label).state, str(QueueState.INTERRUPTED))
        # Shutdown leaves queued entries queued: nothing else was started.
        self.assertEqual(self.entry(queued_after).state, str(QueueState.QUEUED))
        self.assertEqual(self.starts, 1)

    def test_cancelling_the_queued_entry_a_between_jobs_wait_is_for_ends_the_wait(self) -> None:
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 30})
        second = self.submit(run_count=1)
        thread = threading.Thread(target=self.worker.claim_and_run_one)
        thread.start()
        try:
            # The first job succeeds fast (a FakeRunner), so the worker is waiting the 30 s cooldown by now.
            self.wait_until(lambda: self.entry(first).state == str(QueueState.SUCCEEDED))
            self.assertTrue(self.store.queue.cancel_queued(self.entry(second).id, now=NOW))
            self.worker.wake()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        finally:
            thread.join(timeout=5)
        self.assertEqual(self.entry(second).state, str(QueueState.CANCELLED))

    def test_stopping_during_the_between_jobs_wait_ends_it_at_once(self) -> None:
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 30})
        second = self.submit(run_count=1)
        self.worker.start()
        self.wait_until(lambda: self.entry(first).state == str(QueueState.SUCCEEDED))
        started = time.monotonic()
        self.worker.stop()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.entry(second).state, str(QueueState.QUEUED))
        self.assertEqual(self.starts, 1)
