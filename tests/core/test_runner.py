"""Tests for process output and shutdown behavior."""

import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from loguru import logger

from framecontrol.core.process.output import OutputProcessor, OutputStream, ProcessMessage
from framecontrol.core.process.runner import DrawThingsProcessRunner
from framecontrol.core.process.signals import interruptible_wait


@dataclass(frozen=True)
class ProcessCommand:
    command: tuple[str, ...]
    cwd: Path | None = None


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

    def test_runner_uses_a_backend_requested_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = ProcessCommand((sys.executable, "-c", "import os; print(os.getcwd())"), Path(directory))
            result = DrawThingsProcessRunner(command, handle_signals=False).run()
        self.assertTrue(result.succeeded)
        self.assertEqual(Path(result.messages[0].text).resolve(), Path(directory).resolve())

    def test_cleanup_does_not_restart_shutdown_after_giving_up(self) -> None:
        runner = DrawThingsProcessRunner(ProcessCommand(("unused",)), shutdown_grace_seconds=10, handle_signals=False)
        runner.request_shutdown(signal.SIGTERM)
        runner._kill_sent_at = 0.0
        process = mock.Mock()
        with mock.patch("framecontrol.core.process.runner.process_group_alive", return_value=True), mock.patch("framecontrol.core.process.runner.send_to_process_group") as send:
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

    def test_a_byte_on_the_callers_pipe_ends_the_wait_from_another_thread(self) -> None:
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        stop = threading.Event()

        def cancel() -> None:
            stop.set()
            os.write(write_end, b"\0")

        timer = threading.Timer(0.1, cancel)
        try:
            timer.start()
            waited = interruptible_wait(5, stop.is_set, wake_on_signal=False, wake_fd=read_end)
        finally:
            timer.cancel()
            os.close(read_end)
            os.close(write_end)
        self.assertGreaterEqual(waited, 0.1)
        self.assertLess(waited, 1)


class RunnerStartHookTests(unittest.TestCase):
    def test_on_start_is_told_the_childs_pid_and_a_failing_hook_does_not_stop_the_run(self) -> None:
        pids: list[int] = []
        command = ProcessCommand((sys.executable, "-c", "print('ok')"))
        result = DrawThingsProcessRunner(command, handle_signals=False, on_start=pids.append).run()
        self.assertTrue(result.succeeded)
        self.assertEqual(len(pids), 1)
        self.assertGreater(pids[0], 1)

        def broken(_pid: int) -> None:
            raise RuntimeError("lock file is gone")

        self.assertTrue(DrawThingsProcessRunner(command, handle_signals=False, on_start=broken).run().succeeded)


# A child that writes the given bytes (hex) with a pause after each, noting the time of each write in a file.
STAMPED_CHILD = "import json, os, sys, time\nlog = open(sys.argv[1], 'w')\nfor data, pause in json.loads(sys.argv[2]):\n    os.write(1, bytes.fromhex(data))\n    log.write(repr(time.monotonic()) + '\\n')\n    log.flush()\n    time.sleep(pause)\n"


def hexed(text: str) -> str:
    return text.encode("utf-8").hex()


@dataclass(frozen=True)
class RedrawCommand:
    """A backend's command whose output may be read before its terminator, as H3's is."""

    command: tuple[str, ...]
    unterminated_reading: Callable[[str], bool] | None = None
    cwd: Path | None = None


def counter_ends_text(text: str) -> bool:
    head, _, tail = text.rpartition(" ")
    counted = tail.split("/", 1)
    return bool(head) and len(counted) == 2 and all(part.isdigit() for part in counted)


class SourceStampingTests(unittest.TestCase):
    def run_child(self, writes: list[tuple[str, float]], *, accepts: Callable[[str], bool] | None = counter_ends_text, idle: float = 0.2, stop_after: str | None = None) -> tuple[list[ProcessMessage], list[float], list[float]]:
        """Run a child making ``writes``; return its messages, the times it wrote at, and when each message reached the callback.
        The child is stopped when the message ``stop_after`` arrives."""
        arrivals: list[float] = []
        runner: DrawThingsProcessRunner

        def record(message: ProcessMessage) -> None:
            arrivals.append(time.monotonic())
            if message.text == stop_after:
                runner.request_shutdown(signal.SIGTERM)

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "writes"
            command = RedrawCommand((sys.executable, "-c", STAMPED_CHILD, str(log), json.dumps(writes)), accepts)
            runner = DrawThingsProcessRunner(command, output_processor=OutputProcessor(callback=record), idle_release_seconds=idle, handle_signals=False, shutdown_grace_seconds=1)
            result = runner.run()
            times = [float(line) for line in log.read_text(encoding="utf-8").split()]
        return list(result.messages), times, arrivals

    def assert_stamped_like_the_writes(self, messages: list[ProcessMessage], times: list[float], indexes: list[int]) -> None:
        # The runner's clock and the child's differ by a constant, so compare each stamp with the first one's.
        for message, index in zip(messages, indexes, strict=True):
            self.assertAlmostEqual(message.elapsed_seconds - messages[0].elapsed_seconds, times[index] - times[indexes[0]], delta=0.05, msg=message.text)

    def test_a_redraw_is_stamped_when_it_was_written_not_when_the_next_one_ended_it(self) -> None:
        writes = [(hexed(f"\rdenoise {step}/30"), 0.4) for step in range(1, 5)]
        messages, times, _arrivals = self.run_child(writes, accepts=None)
        self.assertEqual([message.text for message in messages], [f"denoise {step}/30" for step in range(1, 5)])
        self.assert_stamped_like_the_writes(messages, times, [0, 1, 2, 3])

    def test_a_write_that_splits_a_number_gives_the_whole_reading_stamped_by_its_last_byte(self) -> None:
        writes = [(hexed("\rdenoise 2/30"), 0.4), (hexed("\rdenoise 3/3"), 0.03), (hexed("0"), 0.5), (hexed("\rdenoise 4/30"), 0.4)]
        messages, times, _arrivals = self.run_child(writes)
        self.assertEqual([message.text for message in messages], ["denoise 2/30", "denoise 3/30", "denoise 4/30"])
        self.assert_stamped_like_the_writes(messages, times, [0, 2, 3])

    def test_a_write_that_splits_a_multi_byte_character_keeps_it_whole(self) -> None:
        encoded = "\rcaf\u00e9 1/2\n".encode()
        cut = encoded.index(b"\xc3") + 1
        writes = [(encoded[:cut].hex(), 0.05), (encoded[cut:].hex(), 0.05)]
        messages, times, _arrivals = self.run_child(writes)
        self.assertEqual([message.text for message in messages], ["caf\u00e9 1/2"])
        self.assertEqual(len(times), 2)

    def test_a_reading_that_is_never_terminated_is_delivered_once_the_idle_window_passes(self) -> None:
        writes = [(hexed("\rdenoise 7/30"), 30.0)]
        messages, times, arrivals = self.run_child(writes, stop_after="denoise 7/30")
        self.assertEqual([message.text for message in messages], ["denoise 7/30"])
        # The child sleeps 30 s after writing and nothing ends the reading; the window did, long before that.
        self.assertLess(arrivals[0] - times[0], 1.0)

    def test_text_the_predicate_does_not_accept_is_not_released_early(self) -> None:
        writes = [(hexed("loading weights"), 0.6)]
        messages, times, arrivals = self.run_child(writes)
        self.assertEqual([message.text for message in messages], ["loading weights"])
        self.assertGreater(arrivals[0] - times[0], 0.4)

    def test_without_a_predicate_a_backend_that_terminates_its_lines_is_read_as_before(self) -> None:
        messages, _times, _arrivals = self.run_child([(hexed("one\ntwo\r\nthree"), 0.05)], accepts=None)
        self.assertEqual([message.text for message in messages], ["one", "two", "three"])


class StartLogTests(unittest.TestCase):
    def test_the_start_is_logged_with_a_note_that_output_is_stamped_as_read(self) -> None:
        logged: list[str] = []
        sink = logger.add(lambda message: logged.append(str(message)), format="{message}", level="INFO")
        try:
            DrawThingsProcessRunner(ProcessCommand((sys.executable, "-c", "pass")), handle_signals=False).run()
        finally:
            logger.remove(sink)
        # fc timing import reads this note to know that a log's readings need no shift.
        self.assertTrue(any(line.startswith("Started subprocess (PID ") and line.rstrip().endswith(", output stamped as read)") for line in logged), logged)
