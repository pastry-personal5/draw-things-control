"""Tests for the run-job and validate-job commands."""

import shlex
from pathlib import Path
from unittest import mock

from job_fixtures import JobTestCase, job_data
from typer.testing import CliRunner

from draw_things_arguments import DrawThingsGenerateArguments
from main import app, create_runner


class JobCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.global_path = self.root / "global-config.yaml"
        self.global_path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n", encoding="utf-8")
        self.job_path = self.write_job(job_data())
        # The commands read the repository's dt-config/; point it at the test's copy.
        patcher = mock.patch("generation_config.DT_CONFIG_DIRECTORY", self.dt_config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_validate_job_reports_runs_and_paths(self) -> None:
        result = self.runner.invoke(app, ["validate-job", str(self.job_path), "--global-config", str(self.global_path)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("runs: 5 (walk, wave, walk, wave, walk)", result.stdout)
        self.assertIn(str(self.output_directory / "sunset-walk"), result.stdout)

    def test_invalid_job_and_missing_global_config_exit_with_2(self) -> None:
        bad_job = self.write_job(job_data(mode="t2i"), name="bad.yaml")
        self.assertEqual(self.runner.invoke(app, ["validate-job", str(bad_job), "--global-config", str(self.global_path)]).exit_code, 2)
        self.assertEqual(self.runner.invoke(app, ["validate-job", str(self.job_path), "--global-config", str(self.root / "absent.yaml")]).exit_code, 2)

    def test_dry_run_prints_every_command_and_writes_nothing(self) -> None:
        executable_stub = self.root / "draw-things-cli"
        executable_stub.write_text("#!/bin/sh\n", encoding="utf-8")
        executable_stub.chmod(0o755)
        result = self.runner.invoke(app, ["run-job", str(self.job_path), "--global-config", str(self.global_path), "--dry-run", "--executable", str(executable_stub)])
        self.assertEqual(result.exit_code, 0, result.output)
        commands = [shlex.split(line) for line in result.stdout.splitlines() if not line.startswith("#")]
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0][commands[0].index("--image") + 1], str(self.input_directory / "first-frame.png"))
        self.assertTrue(commands[1][commands[1].index("--image") + 1].endswith("-last-frame.png"))
        self.assertFalse(self.output_directory.exists())

    def test_generate_without_output_lets_the_child_use_the_terminal(self) -> None:
        self.assertFalse(create_runner(DrawThingsGenerateArguments(model="m.ckpt"), None, 1)._capture_output)
        self.assertFalse(create_runner(DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png"), terminal_image=True), None, 1)._capture_output)
        self.assertTrue(create_runner(DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png")), None, 1)._capture_output)

    def test_dry_run_with_desired_size_shows_the_placeholder(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        job_path = self.write_job(job_data(input="photo.jpg", desired_input_width=850), name="resize.yaml")
        executable_stub = self.root / "draw-things-cli"
        executable_stub.write_text("#!/bin/sh\n", encoding="utf-8")
        executable_stub.chmod(0o755)
        result = self.runner.invoke(app, ["run-job", str(job_path), "--global-config", str(self.global_path), "--dry-run", "--executable", str(executable_stub)])
        self.assertEqual(result.exit_code, 0, result.output)
        first = next(shlex.split(line) for line in result.stdout.splitlines() if not line.startswith("#"))
        self.assertEqual(first[first.index("--image") + 1], "<photo.jpg resized to 832x448>")
        self.assertEqual(first[first.index("--width") + 1 : first.index("--height") + 2], ["832", "--height", "448"])
        self.assertFalse(self.output_directory.exists())
