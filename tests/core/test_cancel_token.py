"""Tests for the token that stops a running job: cancel from any thread, the wake-up pipe, and the runner it stops."""

from __future__ import annotations

import os
import signal
import threading
import time
import unittest

from draw_things_control.core.process.signals import CancelToken


class FakeRunner:
    def __init__(self) -> None:
        self.stops: list[signal.Signals] = []

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        self.stops.append(received_signal)


class CancelTokenTests(unittest.TestCase):
    def test_cancel_with_no_job_running_does_nothing(self) -> None:
        token = CancelToken(handle_signals=False)
        self.assertFalse(token.cancel(signal.SIGINT))
        self.assertIsNone(token.requested)

    def test_cancel_records_the_signal_and_stops_the_attached_runner(self) -> None:
        token, runner = CancelToken(handle_signals=False), FakeRunner()
        token.begin()
        token.attach(runner)
        self.assertTrue(token.cancel(signal.SIGTERM))
        self.assertEqual((token.requested, runner.stops), (signal.SIGTERM, [signal.SIGTERM]))
        token.end()

    def test_a_runner_attached_after_a_cancel_is_stopped_at_once(self) -> None:
        token, runner = CancelToken(handle_signals=False), FakeRunner()
        token.begin()
        token.cancel(signal.SIGINT)
        token.attach(runner)
        self.assertEqual(runner.stops, [signal.SIGINT])
        token.detach()
        token.end()

    def test_a_new_job_starts_with_no_stop_requested(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        token.cancel(signal.SIGINT)
        token.end()
        token.begin()
        self.assertIsNone(token.requested)
        token.end()

    def test_a_second_begin_is_refused(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        with self.assertRaisesRegex(RuntimeError, "already running a job"):
            token.begin()
        token.end()

    def test_end_closes_the_pipe(self) -> None:
        before = set(os.listdir("/dev/fd"))
        token = CancelToken(handle_signals=False)
        token.begin()
        self.assertEqual(len(set(os.listdir("/dev/fd")) - before), 2)
        token.end()
        self.assertEqual(set(os.listdir("/dev/fd")), before)

    def test_a_signal_handler_only_sets_the_flag_and_stops_the_runner(self) -> None:
        token, runner = CancelToken(handle_signals=False), FakeRunner()
        token.begin()
        token.attach(runner)
        token.receive(signal.SIGHUP)
        self.assertEqual((token.requested, runner.stops), (signal.SIGHUP, [signal.SIGHUP]))
        token.end()

    def test_cancel_from_another_thread_ends_a_wait_at_once(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        timer = threading.Timer(0.1, token.cancel, (signal.SIGINT,))
        timer.start()
        started = time.monotonic()
        waited = token.wait(5)
        timer.join()
        self.assertLess(time.monotonic() - started, 1)
        self.assertAlmostEqual(waited, time.monotonic() - started, delta=0.5)
        token.end()

    def test_a_wait_runs_its_full_time_without_a_stop(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        self.assertGreaterEqual(token.wait(0.05), 0.05)
        token.end()

    # Park (Milestone 05).

    def test_park_with_no_job_running_does_nothing(self) -> None:
        token = CancelToken(handle_signals=False)
        self.assertFalse(token.park())
        self.assertEqual(token.park_count, 0)

    def test_park_never_stops_the_runner_or_reads_as_a_stop(self) -> None:
        token, runner = CancelToken(handle_signals=False), FakeRunner()
        token.begin()
        token.attach(runner)
        self.assertTrue(token.park())
        self.assertEqual((token.park_count, token.requested, runner.stops), (1, None, []))
        token.end()

    def test_park_from_another_thread_ends_a_wait_at_once(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        timer = threading.Timer(0.1, token.park)
        timer.start()
        started = time.monotonic()
        waited = token.wait(5)
        timer.join()
        self.assertLess(time.monotonic() - started, 1)
        self.assertLess(waited, 1)
        self.assertIsNone(token.requested)
        token.end()

    def test_park_writes_the_wake_up_byte(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        token.park()
        assert token._wake_read is not None
        self.assertEqual(os.read(token._wake_read, 16), b"\0")
        token.end()

    def test_unpark_withdraws_a_park_until_it_is_taken(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        token.park()
        self.assertTrue(token.unpark())
        self.assertFalse(token.take_park())
        token.park()
        self.assertTrue(token.take_park())
        self.assertFalse(token.unpark())
        self.assertTrue(token.take_park())
        self.assertEqual(token.park_count, 2)
        # Still refused after the job has ended: the park took effect.
        token.end()
        self.assertFalse(token.unpark())

    def test_unpark_and_take_park_race_from_two_threads(self) -> None:
        def race(token: CancelToken) -> dict[str, bool]:
            results: dict[str, bool] = {}
            threads = [threading.Thread(target=lambda: results.__setitem__("unpark", token.unpark())), threading.Thread(target=lambda: results.__setitem__("take", token.take_park()))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            return results

        for _ in range(50):
            token = CancelToken(handle_signals=False)
            token.begin()
            token.park()
            results = race(token)
            # Exactly one of them wins: the park is either withdrawn or taken, never both.
            self.assertNotEqual(results["unpark"], results["take"])
            token.end()

    def test_a_new_job_starts_with_no_park(self) -> None:
        token = CancelToken(handle_signals=False)
        token.begin()
        token.park()
        token.take_park()
        token.end()
        token.begin()
        self.assertEqual(token.park_count, 0)
        self.assertFalse(token.take_park())
        self.assertTrue(token.unpark())
        token.end()
