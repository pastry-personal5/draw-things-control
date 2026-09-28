"""Tests for import-history, and the runner helper it (and generate) share.

The run-lock and execution-recording behaviors this file used to exercise through the now-removed ``run-job``
command (a busy lock, a crash-left-running row, history retention, an unreachable state directory, a newer schema, a
failed execution ID reservation) are ``JobRunSession``'s own contract, not the CLI's: they are covered directly
against it in ``tests/services/test_job_runs.py``, the layer ``dtc serve``'s worker and the TUI now share (Milestone
03) instead of a CLI command running a job itself.
"""

import itertools
import json
import os
from pathlib import Path
from unittest import mock

from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli.app import CliServices, app
from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.files import read_job
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.services.toolkit import create_job_runner
from draw_things_control.state.store import Store
from tests.fixtures import FakeToolkit, JobTestCase, job_data, job_executor
from tests.jobs.test_executor import FakeResult, FakeRunner


class StateCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.state = self.root / "state"
        self.write_global_config()
        self.job_path = self.write_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.results: dict[int, FakeResult] = {}
        self.runs_started = 0
        numbers = itertools.count(1000)
        self.fake_service = job_executor(
            runner_factory=self.create_runner,
            find_executable=lambda executable: executable,
            frame_extractor=self.extract,
            require_ffmpeg=lambda: "ffmpeg",
            random_number=lambda: next(numbers),
            handle_signals=False,
            cooldown=lambda seconds: seconds,
        )
        self.services = CliServices(self.paths, FakeToolkit(self.fake_service))

    def write_global_config(self, extra: str = "", *, name: str = "global-config.yaml") -> Path:
        path = self.root / name
        path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n{extra}", encoding="utf-8")
        self.global_path = path
        return path

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
        self.runs_started += 1
        return FakeRunner(arguments, self.results.get(self.runs_started, FakeResult()), write_output=True)

    def invoke(self, *arguments: str):
        return self.runner.invoke(app, [*arguments, "--global-config", str(self.global_path)], obj=self.services)

    def seed_execution(self) -> GlobalConfig:
        """Runs ``self.job_path`` through ``JobRunSession`` directly (the same use case ``dtc serve``'s worker and
        the TUI now share), recording a real execution -- and, with ``write_job_records: true``, a real manifest --
        for ``import-history`` to read, without a CLI command that runs a job itself any more."""
        job, settings = read_job(self.job_path, self.global_path, self.paths)
        outcome = JobRunSession(self.paths, self.fake_service, settings).run(job, holder="test", executable="draw-things-cli", shutdown_grace=1)
        assert outcome.exit_code == 0, outcome
        return settings

    @staticmethod
    def extract(video: Path, png: Path) -> None:
        png.write_bytes(b"png")

    def store(self) -> Store:
        self.state.mkdir(exist_ok=True)
        store = Store.open(self.state / "dtc.db")
        self.addCleanup(store.close)
        return store

    def test_commands_that_start_nothing_work_while_the_lock_is_held(self) -> None:
        with RunLock("serve", directory=self.state):
            self.assertEqual(self.invoke("validate-job", str(self.job_path)).exit_code, 0)
            self.assertEqual(self.runner.invoke(app, ["validate-config", str(self.params / "base.yaml")], obj=self.services).exit_code, 0)
            self.assertEqual(self.runner.invoke(app, ["generate", "--model", "m.ckpt", "--prompt", "x", "--dry-run"], obj=self.services).exit_code, 0)
            # None of those touched the database.
            self.assertFalse((self.state / "dtc.db").exists())
            self.output_directory.mkdir()
            self.assertEqual(self.invoke("import-history").exit_code, 0)

    def test_generate_exits_75_naming_the_server_while_it_holds_the_lock(self) -> None:
        messages: list[str] = []
        sink = logger.add(lambda message: messages.append(str(message).strip()), format="{message}", level="ERROR")
        try:
            with RunLock("serve", directory=self.state):
                result = self.runner.invoke(app, ["generate", "--model", "m.ckpt", "--prompt", "x", "--output", str(self.root / "x.png")], obj=self.services)
        finally:
            logger.remove(sink)
        self.assertEqual(result.exit_code, 75)
        self.assertEqual(messages[0], f"The dtc server (PID {os.getpid()}) holds the run lock while it is up; stop it to generate by hand.")

    def test_the_runner_reports_its_child_with_the_executable_name(self) -> None:
        on_start = mock.Mock()
        runner = create_job_runner(DrawThingsGenerateArguments(model="m.ckpt", executable="/opt/bin/my-cli"), None, 1, None, on_start)
        assert runner._on_start is not None
        runner._on_start(4242)
        on_start.assert_called_once_with(4242, "my-cli")
        self.assertIsNone(create_job_runner(DrawThingsGenerateArguments(model="m.ckpt"), None, 1)._on_start)

    def test_import_history_imports_once_and_reports_counts(self) -> None:
        self.write_global_config("write_job_records: true\n")
        self.seed_execution()
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
        self.assertEqual(len(self.store().executions.page()), 1)
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["name"], "sunset-walk")
        # The run recorded its ID in the manifest; imported again into a fresh database, the execution takes the next
        # free number (E0001 again here, so there is nothing to add).
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["execution_id"], "E0001")
        self.assertIn(f"  E0001: {manifest}\n", first.stdout)

    def test_an_import_names_the_id_its_manifest_records_when_it_differs(self) -> None:
        self.write_global_config("write_job_records: true\n")
        self.seed_execution()
        [manifest] = self.output_directory.rglob("*-job.json")
        # A fresh database whose numbering has moved on: the import takes the next free number and names the old one.
        (self.state / "dtc.db").unlink()
        store = self.store()
        for _ in range(4):
            store.executions.reserve_number()
        store.close()
        imported = self.invoke("import-history")
        self.assertIn(f"  E0005: {manifest} (its manifest says E0001)\n", imported.stdout)
        [row] = self.store().executions.page()
        self.assertEqual(row.execution_number, 5)

    def test_import_history_reads_another_directory_and_rejects_a_missing_one(self) -> None:
        self.assertEqual(self.invoke("import-history", "--directory", str(self.root / "absent")).exit_code, 2)
        self.assertEqual(self.invoke("import-history", "--directory", str(self.root)).exit_code, 0)
