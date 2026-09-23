"""Tests for process output and shutdown behavior."""

import io
import signal
import sys
import unittest
from dataclasses import dataclass

from loguru import logger

from draw_things_runner import DrawThingsProcessRunner
from process_output import OutputProcessor, OutputStream, ProcessMessage


@dataclass(frozen=True)
class ProcessCommand:
    command: tuple[str, ...]


class ProcessRunnerTests(unittest.TestCase):
    def test_runner_captures_and_logs_both_output_streams(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdout_sink = logger.add(stdout, format="{message}", filter=lambda record: record["extra"].get("child_stream") == "stdout")
        stderr_sink = logger.add(stderr, format="{message}", filter=lambda record: record["extra"].get("child_stream") == "stderr")
        try:
            runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", "import sys; print('sampling 2/4'); print('warning', file=sys.stderr)")), output_processor=OutputProcessor(), handle_signals=False)
            result = runner.run()
        finally:
            logger.remove(stdout_sink)
            logger.remove(stderr_sink)

        self.assertTrue(result.succeeded)
        messages_by_stream = {message.stream: message for message in result.messages}
        self.assertEqual(messages_by_stream[OutputStream.STDOUT].progress, (2, 4))
        self.assertEqual(messages_by_stream[OutputStream.STDERR].text, "warning")
        self.assertIn("sampling 2/4", stdout.getvalue())
        self.assertIn("warning", stderr.getvalue())

    def test_runner_requests_graceful_shutdown_after_output(self) -> None:
        runner: DrawThingsProcessRunner

        def request_shutdown(_message: ProcessMessage) -> None:
            runner.request_shutdown(signal.SIGTERM)

        runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)")), output_processor=OutputProcessor(callback=request_shutdown), shutdown_grace_seconds=1, handle_signals=False)
        result = runner.run()
        self.assertEqual(result.termination_signal, signal.SIGTERM)
        self.assertLess(result.elapsed_seconds, 2)

    def test_runner_timeout_returns_promptly(self) -> None:
        runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", "import time; time.sleep(60)")), shutdown_grace_seconds=1, timeout_seconds=0.1, handle_signals=False)
        result = runner.run()
        self.assertTrue(result.timed_out)
        self.assertLess(result.elapsed_seconds, 2)
