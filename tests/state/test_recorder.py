"""Tests for recording a job's events in the state store."""

import itertools
import json
import signal
from dataclasses import replace
from unittest import mock

from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.jobs.job_definition import JobDefinition, load_job
from draw_things_control.jobs.job_events import combine_observers
from draw_things_control.jobs.job_service import JobService, PlannedRun
from draw_things_control.jobs.media_info import MediaInfo
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import Store
from tests.fixtures import JobTestCase, job_data
from tests.jobs.test_job_service import NOW, FakeResult, FakeRunner


class RecorderTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.root / "dtc.db")
        self.addCleanup(self.store.close)
        self.results: dict[int, FakeResult] = {}
        self.calls = 0
        numbers = itertools.count(1000)
        self.service = JobService(
            runner_factory=self.create_runner,
            find_executable=lambda executable: executable,
            frame_extractor=lambda video, png: png.write_bytes(b"png"),
            require_ffmpeg=lambda: "ffmpeg",
            clock=lambda: NOW,
            random_number=lambda: next(numbers),
            random_seed=lambda: 777,
            handle_signals=False,
            cooldown=lambda seconds: seconds,
        )

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
        self.calls += 1
        return FakeRunner(arguments, self.results.get(self.calls, FakeResult()), write_output=True)

    def job(self, **changes: object) -> JobDefinition:
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.params)

    def run_recorded(self, job: JobDefinition, *, write_records: bool, recorder: ExecutionRecorder | None = None):
        return self.service.run(job, executable="draw-things-cli", shutdown_grace=2, write_records=write_records, observer=recorder or ExecutionRecorder(self.store))

    def test_an_execution_and_its_runs_match_the_manifest(self) -> None:
        job = self.job(run_count=3, prompt_pairs=[{"name": "only", "positive": "text", "negative": "blurry"}], cooldown={"mode": "manual", "seconds": 30})
        outcome = self.run_recorded(job, write_records=True)
        manifest = json.loads(outcome.manifest.read_text(encoding="utf-8"))
        [row] = self.store.list_executions()
        execution = self.store.get_execution(row["id"])
        self.assertEqual((execution["job_name"], execution["job_file"], execution["mode"], execution["status"], execution["exit_code"], execution["total_runs"]), (manifest["name"], manifest["job_file"], manifest["mode"], "succeeded", 0, 3))
        self.assertEqual((execution["seed"], execution["seed_source"], execution["cooldown_seconds"], execution["cooldown_source"], execution["model"]), (manifest["seed"], manifest["seed_source"], 30.0, manifest["cooldown_source"], "base.ckpt"))
        self.assertEqual((execution["started_at"], execution["finished_at"], execution["manifest_path"], execution["log_path"]), (manifest["started_at"], manifest["finished_at"], str(outcome.manifest), str(outcome.log)))
        self.assertEqual(execution["job_yaml"], job.path.read_text(encoding="utf-8"))
        self.assertEqual((execution["config_file"], execution["settings"]["config_override"], execution["settings"]["output_directory"]), (manifest["config_file"], manifest["config_override"], str(job.output_directory)))
        self.assertEqual(len(execution["runs"]), len(manifest["runs"]))
        for stored, recorded in zip(execution["runs"], manifest["runs"], strict=True):
            self.assertEqual((stored["pair"], stored["positive"], stored["negative"], stored["input"], stored["output"], stored["last_frame"], stored["command"], stored["started_at"], stored["seconds"], stored["exit_code"], stored["status"], stored["cooldown_after_seconds"]), (recorded["pair"], recorded["positive"], recorded["negative"], recorded["input"], recorded["output"], recorded["last_frame"], recorded["command"], recorded["started_at"], recorded["seconds"], recorded["exit_code"], recorded["status"], recorded["cooldown_after_seconds"]))
        self.assertEqual([run["cooldown_after_seconds"] for run in execution["runs"]], [30.0, 30.0, None])
        self.assertEqual((execution["settings"]["cooldown"], manifest["cooldown"]), ({"mode": "manual", "seconds": 30.0}, {"mode": "manual", "seconds": 30.0}))

    def test_the_measured_output_is_recorded_as_in_the_manifest(self) -> None:
        self.service._output_measurer = lambda path: MediaInfo(832, 448, 81)
        outcome = self.run_recorded(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}], cooldown={"mode": "off"}), write_records=True)
        manifest = json.loads(outcome.manifest.read_text(encoding="utf-8"))
        [row] = self.store.list_executions()
        runs = self.store.get_execution(row["id"])["runs"]  # type: ignore[index]
        self.assertEqual([(run["output_width"], run["output_height"], run["output_frames"]) for run in runs], [(832, 448, 81)] * 2)
        self.assertEqual([(run["output_width"], run["output_height"], run["output_frames"]) for run in manifest["runs"]], [(832, 448, 81)] * 2)

    def test_an_auto_cooldown_is_recorded_as_its_mapping(self) -> None:
        self.global_config = replace(self.global_config, cooldown=None)
        self.run_recorded(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]), write_records=False)
        [row] = self.store.list_executions()
        execution = self.store.get_execution(row["id"])
        self.assertEqual((execution["cooldown_seconds"], execution["cooldown_source"]), (None, "default"))
        self.assertEqual(execution["settings"]["cooldown"], {"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 3600.0})

    def test_recording_does_not_depend_on_write_job_records(self) -> None:
        outcome = self.run_recorded(self.job(), write_records=False)
        self.assertIsNone(outcome.manifest)
        [execution] = self.store.list_executions()
        self.assertEqual((execution["status"], execution["manifest_path"], execution["log_path"]), ("succeeded", None, None))
        self.assertEqual(len(self.store.get_execution(execution["id"])["runs"]), 5)

    def test_failed_and_interrupted_jobs_are_recorded_with_their_outcome(self) -> None:
        self.results[2] = FakeResult(return_code=3)
        self.run_recorded(self.job(), write_records=False)
        self.results = {1: FakeResult(termination_signal=signal.SIGINT)}
        self.calls = 0
        self.run_recorded(self.job(), write_records=False)
        failed, interrupted = self.store.list_executions()[::-1]
        self.assertEqual((failed["status"], failed["exit_code"]), ("failed", 3))
        self.assertEqual((interrupted["status"], interrupted["exit_code"], interrupted["signal"]), ("interrupted", 130, "SIGINT"))
        self.assertEqual([run["status"] for run in self.store.get_execution(failed["id"])["runs"]], ["succeeded", "failed"])

    def test_no_credential_reaches_the_store(self) -> None:
        secret = "hunter2-secret"
        plan_run = self.service._plan_run

        def plan_run_with_a_key(*arguments: object, **options: object) -> PlannedRun:
            run = plan_run(*arguments, **options)
            return replace(run, arguments=replace(run.arguments, cloud_compute=True, api_key=secret))

        with mock.patch.object(self.service, "_plan_run", plan_run_with_a_key):
            self.run_recorded(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]), write_records=False)
        self.assertIn("--api-key", self.store.get_execution(self.store.list_executions()[0]["id"])["runs"][0]["command"])
        self.assertNotIn(secret, "\n".join(self.store._connection().iterdump()))

    def test_a_recording_failure_is_logged_once_and_the_job_completes(self) -> None:
        with mock.patch.object(self.store, "start_execution", side_effect=RuntimeError("database is locked")), mock.patch("draw_things_control.state.recorder.logger") as log:
            outcome = self.run_recorded(self.job(), write_records=False)
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 5))
        self.assertEqual(log.exception.call_count, 1)
        self.assertEqual(self.store.list_executions(), [])

    def test_a_later_failure_stops_recording_without_touching_earlier_rows(self) -> None:
        with mock.patch.object(self.store, "start_run", side_effect=RuntimeError("disk full")), mock.patch("draw_things_control.state.recorder.logger"):
            outcome = self.run_recorded(self.job(), write_records=False)
        self.assertEqual(outcome.exit_code, 0)
        [execution] = self.store.list_executions()
        self.assertEqual(execution["status"], "running")

    def test_the_execution_id_is_known_once_job_started_is_recorded_and_forgotten_on_failure(self) -> None:
        recorder = ExecutionRecorder(self.store)
        self.assertIsNone(recorder.execution_id)
        self.run_recorded(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]), write_records=False, recorder=recorder)
        self.assertEqual(recorder.execution_id, self.store.list_executions()[0]["id"])
        failing = ExecutionRecorder(self.store)
        with mock.patch.object(self.store, "start_run", side_effect=RuntimeError("disk full")), mock.patch("draw_things_control.state.recorder.logger"):
            self.run_recorded(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]), write_records=False, recorder=failing)
        self.assertIsNone(failing.execution_id)

    def test_the_recorder_combines_with_another_observer(self) -> None:
        seen: list[object] = []
        self.run_recorded(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]), write_records=False, recorder=combine_observers(ExecutionRecorder(self.store), seen.append))
        self.assertTrue(seen)
        self.assertEqual(self.store.list_executions()[0]["status"], "succeeded")

    def test_the_manifest_path_is_stored_resolved(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.output_directory, target_is_directory=True)
        self.output_directory.mkdir(exist_ok=True)
        outcome = self.run_recorded(self.job(), write_records=True)
        [row] = self.store.list_executions()
        self.assertEqual(row["manifest_path"], str(outcome.manifest.resolve()))
        # A run through a symlinked directory would store the same key the import looks up.
        self.assertTrue(self.store.has_manifest(str((link / outcome.manifest.relative_to(self.output_directory)).resolve())))
