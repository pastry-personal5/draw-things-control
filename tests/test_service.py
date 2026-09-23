"""Tests for generation orchestration without launching a subprocess."""

import signal
import unittest
from dataclasses import dataclass

from draw_things_arguments import DrawThingsGenerateArguments
from generation_service import GenerationService


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
        def unexpected(*_args: object) -> None:
            self.fail("Dry run must not inspect or launch an executable")

        service = GenerationService(runner_factory=unexpected, find_executable=unexpected, config_loader=unexpected)
        arguments = DrawThingsGenerateArguments(model="example.ckpt", cloud_compute=True, api_key="secret")
        outcome = service.execute(arguments, dry_run=True, timeout=None, shutdown_grace=10)
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("[redacted]", outcome.command_preview)
        self.assertNotIn("secret", outcome.command_preview)

    def test_timeout_maps_to_shell_exit_code_124(self) -> None:
        captured: list[tuple[float | None, float]] = []

        def create_runner(_arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float) -> FakeRunner:
            captured.append((timeout, grace))
            return FakeRunner(FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM))

        service = GenerationService(runner_factory=create_runner, find_executable=lambda executable: executable, config_loader=lambda _path: {})
        outcome = service.execute(DrawThingsGenerateArguments(model="example.ckpt"), dry_run=False, timeout=5, shutdown_grace=2)
        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(captured, [(5, 2)])
