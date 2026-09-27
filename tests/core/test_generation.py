"""Tests for generation orchestration without launching a subprocess."""

import json
import shlex
import signal
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.draw_things_config import load_config
from draw_things_control.core.errors import ToolMissingError
from draw_things_control.core.generation import GenerateRequest, GenerationService, require_executable


@dataclass(frozen=True)
class FakeResult:
    return_code: int
    timed_out: bool
    termination_signal: signal.Signals | None = None


class FakeRunner:
    def __init__(self, result: FakeResult) -> None:
        self.result = result

    def run(self) -> FakeResult:
        return self.result


class GenerationServiceTests(unittest.TestCase):
    def test_preview_does_not_find_or_launch_executable(self) -> None:
        def unexpected(*_args: object) -> Any:
            self.fail("Dry run must not inspect or launch an executable")

        service = GenerationService(runner_factory=unexpected, find_executable=unexpected, config_loader=unexpected)
        arguments = DrawThingsGenerateArguments(model="example.ckpt", cloud_compute=True, api_key="secret")
        outcome = service.execute(arguments, dry_run=True, timeout=None, shutdown_grace=10)
        self.assertEqual(outcome.exit_code, 0)
        preview = outcome.command_preview or ""
        self.assertIn("[redacted]", preview)
        self.assertNotIn("secret", preview)

    def test_timeout_maps_to_shell_exit_code_124(self) -> None:
        captured: list[tuple[float | None, float]] = []

        def create_runner(_arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            captured.append((timeout, grace))
            return FakeRunner(FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM))

        service = GenerationService(runner_factory=create_runner, find_executable=lambda executable: executable, config_loader=lambda _path: {})
        outcome = service.execute(DrawThingsGenerateArguments(model="example.ckpt"), dry_run=False, timeout=5, shutdown_grace=2)
        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(captured, [(5, 2)])

    def test_exit_code_reports_the_cause(self) -> None:
        cases = (
            (FakeResult(return_code=0, timed_out=False, termination_signal=signal.SIGINT), 130),
            (FakeResult(return_code=-15, timed_out=False, termination_signal=signal.SIGTERM), 143),
            (FakeResult(return_code=0, timed_out=False, termination_signal=signal.SIGHUP), 129),
            (FakeResult(return_code=-9, timed_out=False), 137),
            (FakeResult(return_code=3, timed_out=False), 3),
            (FakeResult(return_code=0, timed_out=False), 0),
        )
        for result, expected in cases:
            with self.subTest(result=result):
                service = GenerationService(runner_factory=lambda *_args, result=result: FakeRunner(result), find_executable=lambda executable: executable, config_loader=lambda _path: {})
                outcome = service.execute(DrawThingsGenerateArguments(model="example.ckpt"), dry_run=False, timeout=None, shutdown_grace=1)
                self.assertEqual(outcome.exit_code, expected)

    def test_on_message_and_on_start_reach_the_factory(self) -> None:
        received: list[tuple] = []

        def factory(*args: object) -> FakeRunner:
            received.append(args)
            return FakeRunner(FakeResult(return_code=0, timed_out=False))

        service = GenerationService(runner_factory=factory, find_executable=lambda executable: executable, config_loader=lambda _path: {})
        arguments = DrawThingsGenerateArguments(model="m.ckpt")
        callback = lambda _message: None  # noqa: E731
        service.execute(arguments, dry_run=False, timeout=None, shutdown_grace=1)
        service.execute(arguments, dry_run=False, timeout=None, shutdown_grace=1, on_message=callback, on_start=print)
        self.assertEqual(received, [(arguments, None, 1, None, None), (arguments, None, 1, callback, print)])


def generate_options(**changes: Any) -> GenerateRequest:
    """The request the generate command builds, all unset except ``changes``."""
    return GenerateRequest(**changes)


class ConfigurationFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.launched: list[DrawThingsGenerateArguments] = []

        def factory(arguments: DrawThingsGenerateArguments, *_args: object) -> FakeRunner:
            self.launched.append(arguments)
            return FakeRunner(FakeResult(return_code=0, timed_out=False))

        self.service = GenerationService(runner_factory=factory, find_executable=lambda executable: executable, config_loader=load_config)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_yaml_is_passed_inline_with_config_json_merged_on_top(self) -> None:
        config = self.write("wan.yaml", "# Wan\nmodel: base.ckpt\nsteps: 30\nshift: 3.99\nloras: []\n")
        arguments = self.service.prepare(generate_options(config_file=config, config_json='{"steps": 8, "sharpness": 0.5}'))
        self.assertIsNone(arguments.config_file)
        self.assertEqual(arguments.model, "base.ckpt")
        self.assertEqual(arguments.config_json, '{"model":"base.ckpt","steps":8,"shift":3.99,"loras":[],"sharpness":0.5}')
        command = list(arguments.command)
        self.assertNotIn("--config-file", command)
        self.assertEqual(command.count("--config-json"), 1)
        preview = self.service.execute(arguments, dry_run=True, timeout=None, shutdown_grace=1).command_preview
        split = shlex.split(preview or "")
        self.assertEqual(json.loads(split[split.index("--config-json") + 1]), {"model": "base.ckpt", "steps": 8, "shift": 3.99, "loras": [], "sharpness": 0.5})
        self.service.execute(arguments, dry_run=False, timeout=None, shutdown_grace=1)
        self.assertEqual(self.launched, [arguments])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["wan.yaml"])

    def test_yml_without_config_json_is_passed_inline(self) -> None:
        config = self.write("wan.YML", "model: base.ckpt\n")
        arguments = self.service.prepare(generate_options(config_file=config))
        self.assertEqual((arguments.config_file, arguments.config_json), (None, '{"model":"base.ckpt"}'))

    def test_the_format_follows_the_name_given_not_a_symlink_target(self) -> None:
        target = self.write("v3", "model: base.ckpt\n")
        link = self.root / "current.yaml"
        link.symlink_to(target)
        arguments = self.service.prepare(generate_options(config_file=link))
        self.assertEqual((arguments.config_file, arguments.config_json), (None, '{"model":"base.ckpt"}'))

    def test_invalid_yaml_is_an_input_error(self) -> None:
        config = self.write("wan.yaml", "model: a\nmodel: b\n")
        with self.assertRaisesRegex(ValueError, r"wan\.yaml \(key 'model' appears twice on line 2\)"):
            self.service.prepare(generate_options(config_file=config))


class RequireExecutableTests(unittest.TestCase):
    def test_a_missing_executable_is_a_tool_error_that_is_still_a_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, r"Could not find 'nope' on PATH\. Install Draw Things CLI or pass --executable"):
            require_executable(lambda name: None, "nope")
        try:
            require_executable(lambda name: None, "nope")
        except ToolMissingError as error:
            self.assertEqual(error.code, "tool_missing")

    def test_a_found_executable_is_returned(self) -> None:
        self.assertEqual(require_executable(lambda name: f"/bin/{name}", "draw-things-cli"), "/bin/draw-things-cli")
