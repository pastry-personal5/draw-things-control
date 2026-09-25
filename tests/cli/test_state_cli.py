"""Tests for the run lock and execution history in the CLI."""

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli import app as cli
from draw_things_control.cli.app import app, create_job_runner
from draw_things_control.core import run_lock
from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.job_service import JobService
from draw_things_control.state.store import Store
from tests.fixtures import JobTestCase, job_data
from tests.jobs.test_job_service import FakeResult, FakeRunner


class StateCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.state = self.root / "state"
        self.write_global_config()
        self.job_path = self.write_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.messages: list[str] = []
        self.results: dict[int, FakeResult] = {}
        self.runs_started = 0
        numbers = itertools.count(1000)
        self.fake_service = JobService(
            runner_factory=self.create_runner,
            find_executable=lambda executable: executable,
            frame_extractor=lambda video, png: png.write_bytes(b"png"),
            require_ffmpeg=lambda: "ffmpeg",
            random_number=lambda: next(numbers),
            handle_signals=False,
            cooldown=lambda seconds: seconds,
        )
        for patcher in (
            mock.patch("draw_things_control.core.generation_config.DT_CONFIG_DIRECTORY", self.dt_config),
            mock.patch.object(run_lock, "STATE_DIRECTORY", self.state),
            mock.patch.object(cli, "job_service", self.fake_service),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        sink = logger.add(lambda message: self.messages.append(str(message).strip()), format="{message}", level="ERROR")
        self.addCleanup(logger.remove, sink)

    def write_global_config(self, extra: str = "", *, name: str = "global-config.yaml") -> Path:
        path = self.root / name
        path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n{extra}", encoding="utf-8")
        self.global_path = path
        return path

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
        self.runs_started += 1
        return FakeRunner(arguments, self.results.get(self.runs_started, FakeResult()), write_output=True)

    def invoke(self, *arguments: str):
        return self.runner.invoke(app, [*arguments, "--global-config", str(self.global_path)])

    def run_job(self, *extra: str):
        return self.invoke("run-job", str(self.job_path), "--executable", "draw-things-cli", *extra)

    def store(self) -> Store:
        self.state.mkdir(exist_ok=True)
        store = Store(self.state / "dtc.db")
        self.addCleanup(store.close)
        return store

    def test_run_job_records_the_execution_even_without_job_records(self) -> None:
        result = self.run_job()
        self.assertEqual(result.exit_code, 0, result.output)
        [row] = self.store().list_executions()
        self.assertEqual((row["job_name"], row["status"], row["manifest_path"]), ("sunset-walk", "succeeded", None))
        self.assertEqual(len(self.store().get_execution(row["id"])["runs"]), 2)
        self.assertEqual((self.state / "run.lock").read_text(), "")

    def test_a_failed_job_keeps_its_exit_code_and_is_recorded(self) -> None:
        self.results[1] = FakeResult(return_code=3)
        self.assertEqual(self.run_job().exit_code, 3)
        self.assertEqual(self.store().list_executions()[0]["status"], "failed")

    def test_a_busy_lock_exits_75_with_the_message_and_starts_nothing(self) -> None:
        with RunLock("run-job"):
            result = self.run_job()
            generate = self.runner.invoke(app, ["generate", "--model", "m.ckpt", "--prompt", "x", "--output", str(self.root / "x.png")])
        for outcome in (result, generate):
            self.assertEqual(outcome.exit_code, 75)
        self.assertEqual(self.runs_started, 0)
        self.assertEqual(self.messages[0], f"Another run is in progress (run-job, PID {os.getpid()}). Try again when it finishes.")
        self.assertEqual(self.store().list_executions(), [])

    def test_commands_that_start_nothing_work_while_the_lock_is_held(self) -> None:
        executable = self.root / "draw-things-cli"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        with RunLock("run-job"):
            self.assertEqual(self.run_job("--dry-run").exit_code, 0)
            self.assertEqual(self.invoke("validate-job", str(self.job_path)).exit_code, 0)
            self.assertEqual(self.runner.invoke(app, ["validate-config", str(self.dt_config / "base.json")]).exit_code, 0)
            self.assertEqual(self.runner.invoke(app, ["generate", "--model", "m.ckpt", "--prompt", "x", "--dry-run"]).exit_code, 0)
            # None of those touched the database.
            self.assertFalse((self.state / "dtc.db").exists())
            self.output_directory.mkdir()
            self.assertEqual(self.invoke("import-history").exit_code, 0)

    def test_the_lock_can_be_taken_again_after_a_holder_is_killed(self) -> None:
        script = "import sys, time; from pathlib import Path; from draw_things_control.core.run_lock import RunLock; lock = RunLock('run-job', directory=Path(sys.argv[1])); lock.acquire(); print('held', flush=True); time.sleep(60)"
        self.state.mkdir()
        holder = subprocess.Popen([sys.executable, "-c", script, str(self.state)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.assertEqual(self.run_job().exit_code, 75)
        holder.kill()
        holder.wait()
        self.assertEqual(self.run_job().exit_code, 0)

    def test_an_unwritable_state_directory_stops_before_anything_starts(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("")
        with mock.patch.object(run_lock, "STATE_DIRECTORY", blocker / "state"):
            result = self.run_job()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.runs_started, 0)
        self.assertIn("Cannot create the state directory", self.messages[0])

    def test_a_database_with_a_newer_schema_stops_before_anything_starts(self) -> None:
        self.store().close()
        import sqlite3

        connection = sqlite3.connect(self.state / "dtc.db")
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        result = self.run_job()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.runs_started, 0)
        self.assertIn("newer version", self.messages[0])
        # The lock was released.
        self.assertTrue(run_lock.run_lock_is_free(directory=self.state))

    def test_a_row_left_running_by_a_crash_is_closed_by_the_next_run(self) -> None:
        crashed = self.store().start_execution(job_name="old", job_file="old.yaml", mode="i2v", started_at="2026-09-24T10:00:00+00:00")
        self.assertEqual(self.run_job().exit_code, 0)
        execution = self.store().get_execution(crashed)
        self.assertEqual(execution["status"], "interrupted")
        self.assertIsNotNone(execution["recovered_at"])

    def test_history_retention_days_prunes_when_a_run_opens_the_store(self) -> None:
        store = self.store()
        old = store.start_execution(job_name="old", job_file="old.yaml", mode="i2v", started_at="2020-01-01T10:00:00+00:00")
        store.finish_execution(old, status="succeeded", exit_code=0, signal=None, finished_at="2020-01-01T11:00:00+00:00")
        self.write_global_config("history_retention_days: 0\n")
        self.run_job()
        self.assertIsNotNone(store.get_execution(old))
        self.write_global_config("history_retention_days: 14\n")
        self.run_job()
        self.assertIsNone(store.get_execution(old))

    def test_the_runner_reports_its_child_with_the_executable_name(self) -> None:
        on_start = mock.Mock()
        runner = create_job_runner(DrawThingsGenerateArguments(model="m.ckpt", executable="/opt/bin/my-cli"), None, 1, None, on_start)
        runner._on_start(4242)
        on_start.assert_called_once_with(4242, "my-cli")
        self.assertIsNone(create_job_runner(DrawThingsGenerateArguments(model="m.ckpt"), None, 1)._on_start)

    def test_run_job_passes_the_held_lock_to_the_job(self) -> None:
        seen: list[object] = []
        run = self.fake_service.run

        def recording_run(*args: object, **kwargs: object):
            seen.append(kwargs["on_child_start"])
            return run(*args, **kwargs)

        with mock.patch.object(self.fake_service, "run", recording_run):
            self.assertEqual(self.run_job().exit_code, 0)
        [on_child_start] = seen
        self.assertIsInstance(on_child_start.__self__, RunLock)
        self.assertEqual(on_child_start.__func__, RunLock.record_child)
        self.assertFalse(hasattr(cli, "_active_lock"))

    def test_import_history_imports_once_and_reports_counts(self) -> None:
        self.write_global_config("write_job_records: true\n")
        self.assertEqual(self.run_job().exit_code, 0)
        [manifest] = self.output_directory.rglob("*-job.json")
        (self.output_directory / "sunset-walk" / "notes.json").write_text("{}", encoding="utf-8")
        # The live-recorded manifest is already in the store.
        self.assertIn("Imported 0, skipped 1 already imported, 0 older than the retention period, 1 unreadable", self.invoke("import-history").stdout)
        # A fresh database imports it once.
        (self.state / "dtc.db").unlink()
        first = self.invoke("import-history")
        second = self.invoke("import-history")
        self.assertIn("Imported 1, skipped 0", first.stdout)
        self.assertIn("Imported 0, skipped 1", second.stdout)
        self.assertEqual(len(self.store().list_executions()), 1)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["name"], "sunset-walk")

    def test_import_history_reads_another_directory_and_rejects_a_missing_one(self) -> None:
        self.assertEqual(self.invoke("import-history", "--directory", str(self.root / "absent")).exit_code, 2)
        self.assertEqual(self.invoke("import-history", "--directory", str(self.root)).exit_code, 0)
