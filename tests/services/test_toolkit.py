"""Tests for the real tools and the services built on them."""

import unittest
from pathlib import Path

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.errors import ToolMissingError
from draw_things_control.core.generation import GenerationService
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.services.toolkit import Toolkit, create_job_runner, create_runner


class ToolkitTests(unittest.TestCase):
    def test_it_builds_the_generation_service_and_a_job_executor(self) -> None:
        toolkit = Toolkit()
        self.assertIsInstance(toolkit.generation_service(), GenerationService)
        self.assertIsInstance(toolkit.job_executor(), JobExecutor)

    def test_the_executor_handles_signals_unless_told_not_to(self) -> None:
        toolkit = Toolkit()
        self.assertTrue(toolkit.job_executor()._handle_signals)
        self.assertFalse(toolkit.job_executor(handle_signals=False)._handle_signals)

    def test_a_missing_draw_things_cli_is_a_tool_error(self) -> None:
        service = Toolkit(find_executable=lambda name: None).generation_service()
        arguments = DrawThingsGenerateArguments(model="m.ckpt", executable="nowhere")
        with self.assertRaises(ToolMissingError):
            service.execute(arguments, dry_run=False, timeout=None, shutdown_grace=1)

    def test_a_runner_lets_the_child_use_the_terminal_only_without_an_output_file(self) -> None:
        self.assertFalse(create_runner(DrawThingsGenerateArguments(model="m.ckpt"), None, 1)._capture_output)
        self.assertTrue(create_runner(DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png")), None, 1)._capture_output)

    def test_a_job_runner_leaves_signals_to_the_executor(self) -> None:
        arguments = DrawThingsGenerateArguments(model="m.ckpt", output=Path("a.png"))
        self.assertTrue(create_runner(arguments, None, 1)._handle_signals)
        self.assertFalse(create_job_runner(arguments, None, 1)._handle_signals)
