"""Tests for generation orchestration without launching a subprocess."""

import signal
import unittest
from dataclasses import dataclass

from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.core.generation_service import GenerationService


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
