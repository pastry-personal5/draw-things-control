"""Tests for running a job with its execution recorded: the run lock, the state store, and the observers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.errors import BusyError, StateUnavailableError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.run_lock import RunLock, run_lock_is_free
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import JobEvent, JobStarted
from draw_things_control.jobs.executor import JobOutcome, ResumePoint
from draw_things_control.jobs.parsing import load_job
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.state.database import StateError
from draw_things_control.state.executions import ExecutionRepository, NewExecution
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, TestExecutor, job_data, job_executor
from tests.jobs.test_executor import FakeResult, FakeRunner


class BlockedState(ProjectPaths):
    """The project's paths, except that the state directory would have to be made below a file."""

    @property
    def state(self) -> Path:
        return self.root / "blocker" / "state"


class JobRunSessionTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.started = 0
        self.executor = self.build_executor()
        self.job: JobDefinition = load_job(self.write_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params)

    @staticmethod
    def extract(video: Path, png: Path) -> None:
        png.write_bytes(b"png")

    def build_executor(self) -> TestExecutor:
        def create_runner(arguments: DrawThingsGenerateArguments, *_rest: Any) -> FakeRunner:
            self.started += 1
            return FakeRunner(arguments, FakeResult(), write_output=True)

        return job_executor(runner_factory=create_runner, find_executable=lambda name: name, frame_extractor=self.extract, require_ffmpeg=lambda: "ffmpeg", handle_signals=False, cooldown=lambda seconds: seconds)

    def session(self, paths: ProjectPaths | None = None, settings: GlobalConfig | None = None) -> JobRunSession:
        return JobRunSession(paths or self.paths, self.executor, settings or self.global_config)

    def run_job(self, session: JobRunSession | None = None, **options: Any) -> JobOutcome:
        return (session or self.session()).run(self.job, holder="test", executable="draw-things-cli", shutdown_grace=1, **options)

    def stored(self):
        store = Store.open(self.paths.database, mode=StoreMode.BROWSE)
        self.addCleanup(store.close)
        return store.executions.page()

    def test_a_job_is_run_and_recorded_and_the_lock_is_released(self) -> None:
        outcome = self.run_job()
        self.assertEqual((outcome.exit_code, outcome.completed_runs, self.started), (0, 2, 2))
        [row] = self.stored()
        self.assertEqual((row.job_name, row.status, row.succeeded, row.label), ("sunset-walk", "succeeded", 2, "E0001"))
        self.assertTrue(run_lock_is_free(directory=self.paths.state))
        self.assertEqual((self.paths.state / "run.lock").read_text(), "")

    def test_a_busy_lock_starts_nothing_and_records_nothing(self) -> None:
        with RunLock("other", directory=self.paths.state):
            with self.assertRaises(BusyError) as caught:
                self.run_job()
        self.assertEqual((caught.exception.code, self.started), ("busy", 0))
        self.assertIn("Another run is in progress (other, PID", str(caught.exception))
        self.assertFalse(self.paths.database.exists())

    def test_a_lock_the_caller_holds_is_used_and_left_held(self) -> None:
        lock = RunLock("server", directory=self.paths.state)
        lock.acquire()
        self.addCleanup(lock.release)
        self.run_job(lock=lock)
        self.assertEqual(self.started, 2)
        self.assertFalse(run_lock_is_free(directory=self.paths.state))

    def test_an_unusable_state_directory_stops_before_the_job_and_leaves_no_lock(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("")
        with self.assertRaises(StateUnavailableError) as caught:
            self.run_job(self.session(BlockedState(self.root)))
        self.assertEqual((caught.exception.code, self.started), ("state_unavailable", 0))
        self.assertIn("Cannot create the state directory", str(caught.exception))

    def test_a_database_that_cannot_be_opened_frees_the_lock(self) -> None:
        with mock.patch.object(Store, "open", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaisesRegex(StateError, "Cannot use the state database .*disk I/O error"):
                self.run_job()
        self.assertTrue(run_lock_is_free(directory=self.paths.state))
        self.assertEqual(self.started, 0)

    def test_a_job_that_cannot_get_an_execution_id_does_not_start(self) -> None:
        with mock.patch.object(ExecutionRepository, "reserve_number", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaisesRegex(StateError, "Cannot give the execution an ID"):
                self.run_job()
        self.assertEqual(self.started, 0)
        self.assertFalse(self.job.output_directory.exists())
        self.assertTrue(run_lock_is_free(directory=self.paths.state))

    def test_a_row_a_crash_left_running_is_closed_before_the_job(self) -> None:
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(store.close)
        crashed = store.executions.start(NewExecution(job_name="old", job_file="old.yaml", mode="i2v", started_at="2026-09-24T10:00:00+00:00"))
        self.run_job()
        row = store.executions.get(crashed)
        assert row is not None
        self.assertEqual(row.status, "interrupted")

    def test_before_run_sees_the_store_and_the_recorder_and_observers_follow_the_recorder(self) -> None:
        seen: list[object] = []
        rows_at_start: list[int] = []

        def before_run(store: Store, recorder: ExecutionRecorder) -> None:
            seen.append((store.path, isinstance(recorder, ExecutionRecorder), self.started))

        def observer(event: JobEvent) -> None:
            if isinstance(event, JobStarted):
                # The recorder has already written the execution's row.
                store = Store.open(self.paths.database, mode=StoreMode.BROWSE)
                rows_at_start.append(len(store.executions.page()))
                store.close()

        self.run_job(before_run=before_run, observers=(observer,))
        self.assertEqual(seen, [(self.paths.database, True, 0)])
        self.assertEqual(rows_at_start, [1])

    def test_on_reserved_is_called_with_the_execution_id_before_the_job_starts(self) -> None:
        seen: list[tuple[str, bool]] = []

        def on_reserved(label: str) -> None:
            seen.append((label, self.started == 0))

        self.run_job(on_reserved=on_reserved)
        self.assertEqual(seen, [("E0001", True)])

    def test_a_resume_reaches_the_executor(self) -> None:
        resume = ResumePoint(first_run=2, input=self.job.input, seed=123, resumes_execution="E0001")
        self.job = load_job(self.write_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params)
        self.run_job(resume=resume)
        self.assertEqual(self.started, 1)
