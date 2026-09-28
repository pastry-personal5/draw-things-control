"""Tests for the validate-job command and the runner helpers it and generate share."""

import shutil
from pathlib import Path
from unittest import mock

from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli.app import CliServices, app
from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.services.toolkit import create_job_runner, create_runner
from tests.fixtures import FakeToolkit, JobTestCase, job_data, job_executor


class JobCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.global_path = self.root / "global-config.yaml"
        self.global_path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n", encoding="utf-8")
        self.job_path = self.write_job(job_data())
        # A fake ffmpeg, so a dry run's tool check (jobs/planning.py's check_tools) needs no real ffmpeg on PATH.
        executor = job_executor(runner_factory=mock.Mock(), find_executable=shutil.which, frame_extractor=mock.Mock(), require_ffmpeg=lambda: "ffmpeg")
        self.services = CliServices(self.paths, FakeToolkit(executor))

    def invoke(self, arguments: list[str]):
        return self.runner.invoke(app, arguments, obj=self.services)

    def test_validate_job_reports_runs_and_paths(self) -> None:
        result = self.invoke(["validate-job", str(self.job_path), "--global-config", str(self.global_path)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("runs: 5 (walk, wave, walk, wave, walk)", result.stdout)
        self.assertIn(str(self.output_directory / "sunset-walk"), result.stdout)

    def test_invalid_job_and_missing_global_config_exit_with_2(self) -> None:
        bad_job = self.write_job(job_data(mode="t2i"), name="bad.yaml")
        self.assertEqual(self.invoke(["validate-job", str(bad_job), "--global-config", str(self.global_path)]).exit_code, 2)
        self.assertEqual(self.invoke(["validate-job", str(self.job_path), "--global-config", str(self.root / "absent.yaml")]).exit_code, 2)

    def test_a_job_naming_a_json_configuration_exits_with_2(self) -> None:
        (self.params / "base.json").write_text("{}", encoding="utf-8")
        job = self.write_job(job_data(config_file="base.json"), name="json.yaml")
        self.assertEqual(self.invoke(["validate-job", str(job), "--global-config", str(self.global_path)]).exit_code, 2)

    def test_generate_without_output_lets_the_child_use_the_terminal(self) -> None:
        self.assertFalse(create_runner(DrawThingsGenerateArguments(model="m.ckpt"), None, 1)._capture_output)
        self.assertFalse(create_runner(DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png"), terminal_image=True), None, 1)._capture_output)
        self.assertTrue(create_runner(DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png")), None, 1)._capture_output)

    def test_runners_pass_the_child_output_callback_to_the_output_processor(self) -> None:
        def callback(_message: object) -> None:
            pass

        arguments = DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png"))
        self.assertIs(create_job_runner(arguments, None, 1, callback)._output_processor._callback, callback)
        self.assertIs(create_runner(arguments, None, 1, callback)._output_processor._callback, callback)
        self.assertIsNone(create_job_runner(arguments, None, 1)._output_processor._callback)

    def test_validate_job_shows_the_cooldown(self) -> None:
        self.global_path.write_text(self.global_path.read_text(encoding="utf-8") + "cooldown: {mode: manual, seconds: 900}\n", encoding="utf-8")
        result = self.invoke(["validate-job", str(self.job_path), "--global-config", str(self.global_path)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("  cooldown: 900 s between runs, from global_config (4 waits, 1 h total)", result.stdout)

    def test_job_can_turn_off_the_global_cooldown(self) -> None:
        self.global_path.write_text(self.global_path.read_text(encoding="utf-8") + "cooldown: {mode: manual, seconds: 900}\n", encoding="utf-8")
        job_path = self.write_job(job_data(cooldown={"mode": "off"}), name="no-cooldown.yaml")
        result = self.invoke(["validate-job", str(job_path), "--global-config", str(self.global_path)])
        self.assertIn("  cooldown: off (job)", result.stdout)
        bad = self.write_job(job_data(cooldown={"mode": "manual", "seconds": 4000}), name="bad.yaml")
        self.assertEqual(self.invalid(bad), "'cooldown.seconds' must be a number of seconds from 0 to 3600")

    def invalid(self, job_path: Path) -> str:
        """Validate a job that must fail with exit code 2; return the logged error after the file name."""
        logged: list[str] = []
        sink = logger.add(lambda message: logged.append(str(message).rstrip("\n")), format="{message}", level="ERROR")
        try:
            result = self.invoke(["validate-job", str(job_path), "--global-config", str(self.global_path)])
        finally:
            logger.remove(sink)
        self.assertEqual(result.exit_code, 2)
        return logged[-1].split(": ", 1)[1]

    def test_the_old_cooldown_seconds_key_fails_with_exit_code_2(self) -> None:
        old = self.write_job(job_data(cooldown_seconds=300), name="old.yaml")
        self.assertTrue(self.invalid(old).startswith("'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {mode: manual, seconds: 300} for the same wait"))
        self.global_path.write_text(self.global_path.read_text(encoding="utf-8") + "cooldown_seconds: 1200\n", encoding="utf-8")
        self.assertEqual(self.invalid(self.job_path), "'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {mode: manual, seconds: 1200} for the same wait, or use mode auto or off (see config/global-config.example.yaml)")
