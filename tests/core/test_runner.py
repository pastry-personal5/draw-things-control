"""Tests for process output and shutdown behavior."""

import io
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from loguru import logger

from draw_things_control.core.draw_things_runner import DrawThingsProcessRunner, interruptible_wait
from draw_things_control.core.process_output import OutputProcessor, OutputStream, ProcessMessage


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

    def test_last_lines_after_sigterm_are_kept(self) -> None:
        code = "import signal, sys, time\ndef stop(*_):\n    print('saved partial output', file=sys.stderr, flush=True)\n    sys.exit(0)\nsignal.signal(signal.SIGTERM, stop)\nprint('ready', flush=True)\ntime.sleep(60)\n"
        runner: DrawThingsProcessRunner

        def request_shutdown(message: ProcessMessage) -> None:
            if message.text == "ready":
                runner.request_shutdown(signal.SIGTERM)

        runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", code)), output_processor=OutputProcessor(callback=request_shutdown), shutdown_grace_seconds=5, handle_signals=False)
        result = runner.run()
        self.assertIn("saved partial output", [message.text for message in result.messages])
        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.termination_signal, signal.SIGTERM)

    def test_invalid_utf8_output_does_not_stop_the_child(self) -> None:
        code = "import sys; sys.stdout.buffer.write(b'bad \\xff byte\\n'); sys.stdout.flush(); print('still running')"
        runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", code)), handle_signals=False)
        result = runner.run()
        self.assertTrue(result.succeeded)
        texts = [message.text for message in result.messages]
        self.assertIn("bad \ufffd byte", texts)
        self.assertIn("still running", texts)

    def test_shutdown_requested_before_start_stops_the_child(self) -> None:
        runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", "import time; time.sleep(60)")), shutdown_grace_seconds=1, handle_signals=False)
        runner.request_shutdown(signal.SIGINT)
        result = runner.run()
        self.assertEqual(result.termination_signal, signal.SIGINT)
        self.assertLess(result.elapsed_seconds, 2)

    def test_sigterm_ignoring_child_is_killed_within_bounds(self) -> None:
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        runner: DrawThingsProcessRunner

        def request_shutdown(_message: ProcessMessage) -> None:
            runner.request_shutdown(signal.SIGTERM)

        runner = DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", code)), output_processor=OutputProcessor(callback=request_shutdown), shutdown_grace_seconds=0.5, handle_signals=False)
        result = runner.run()
        self.assertEqual(result.return_code, -signal.SIGKILL)
        self.assertLess(result.elapsed_seconds, 3)

    def test_executable_that_cannot_start_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "not-executable"
            script.write_text("echo hi\n", encoding="utf-8")
            os.chmod(script, 0o644)
            runner = DrawThingsProcessRunner(ProcessCommand((str(script),)), handle_signals=False)
            with self.assertRaisesRegex(ValueError, "Could not start executable"):
                runner.run()

    def test_cleanup_does_not_restart_shutdown_after_giving_up(self) -> None:
        runner = DrawThingsProcessRunner(ProcessCommand(("unused",)), shutdown_grace_seconds=10, handle_signals=False)
        runner.request_shutdown(signal.SIGTERM)
        runner._kill_sent_at = 0.0
        process = mock.Mock()
        with mock.patch.object(DrawThingsProcessRunner, "_is_process_group_alive", return_value=True), mock.patch.object(DrawThingsProcessRunner, "_send_to_process_group") as send:
            runner._cleanup_process_group(12345, process)
        send.assert_not_called()
        process.wait.assert_not_called()


class InterruptibleWaitTests(unittest.TestCase):
    def test_waits_the_full_time_unless_stopped(self) -> None:
        self.assertGreaterEqual(interruptible_wait(0.05, lambda: False), 0.05)
        self.assertLess(interruptible_wait(5, lambda: True), 0.5)

    def test_off_the_main_thread_it_waits_without_a_wakeup_fd(self) -> None:
        waited: list[float] = []
        thread = threading.Thread(target=lambda: waited.append(interruptible_wait(0.05, lambda: False)))
        thread.start()
        thread.join(5)
        self.assertGreaterEqual(waited[0], 0.05)

    def test_polls_stopped_again_after_a_wakeup(self) -> None:
        # stopped() turning true between wake-ups ends the wait at the next one.
        deadline = time.monotonic() + 0.1
        self.assertLess(interruptible_wait(0.3, lambda: time.monotonic() >= deadline, wake_on_signal=False), 0.35)
