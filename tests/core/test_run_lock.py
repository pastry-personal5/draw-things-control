"""Tests for the machine-wide run lock."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from draw_things_control.core import run_lock
from draw_things_control.core.run_lock import RunLock, RunLockBusy, RunLockError, ensure_state_directory, run_lock_is_free

HOLDER = """
import sys, time
from pathlib import Path
from draw_things_control.core.run_lock import RunLock
lock = RunLock("run-job", directory=Path(sys.argv[1]))
lock.acquire()
if len(sys.argv) > 2:
    lock.record_child(int(sys.argv[2]))
print("held", flush=True)
time.sleep(60)
"""


class RunLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def lock(self, **options: object) -> RunLock:
        return RunLock("run-job", directory=self.directory, retry_seconds=0, **options)

    def start_holder(self, child_pid: int | None = None) -> subprocess.Popen:
        arguments = [sys.executable, "-c", HOLDER, str(self.directory)] + ([str(child_pid)] if child_pid else [])
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, text=True)
        self.processes.append(process)
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def start_child(self) -> subprocess.Popen:
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.processes.append(child)
        return child

    def test_a_second_holder_is_refused_and_told_who_holds_the_lock(self) -> None:
        with self.lock():
            with self.assertRaisesRegex(RunLockBusy, rf"Another run is in progress \(run-job, PID {os.getpid()}\)\. Try again when it finishes\."):
                self.lock().acquire()
        # Released with the first holder.
        with self.lock():
            pass

    def test_release_clears_the_description_and_is_repeatable(self) -> None:
        lock = self.lock()
        lock.acquire()
        lock.record_child(4242)
        self.assertEqual((self.directory / "run.lock").read_text(), f"run-job {os.getpid()}\n4242 draw-things-cli\n")
        lock.release()
        lock.release()
        self.assertEqual((self.directory / "run.lock").read_text(), "")
        self.assertEqual(oct((self.directory / "run.lock").stat().st_mode & 0o777), "0o600")

    def test_a_killed_holder_leaves_no_stale_lock(self) -> None:
        holder = self.start_holder()
        with self.assertRaises(RunLockBusy):
            self.lock().acquire()
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        with self.lock():
            pass

    def test_a_busy_lock_is_retried_briefly_before_it_is_reported(self) -> None:
        holder = self.lock()
        holder.acquire()
        started = time.monotonic()
        with self.assertRaises(RunLockBusy):
            RunLock("run-job", directory=self.directory, retry_seconds=0.2).acquire()
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        holder.release()

    def test_a_lock_released_during_the_retry_window_is_taken(self) -> None:
        holder = self.lock()
        holder.acquire()
        self.addCleanup(holder.release)
        real_sleep = time.sleep
        with mock.patch.object(run_lock.time, "sleep", side_effect=lambda seconds: (holder.release(), real_sleep(seconds))):
            with RunLock("run-job", directory=self.directory, retry_seconds=1):
                pass

    def test_a_probe_reports_the_lock_without_changing_the_file(self) -> None:
        self.assertTrue(run_lock_is_free(directory=self.directory))
        (self.directory / "run.lock").write_text("run-job 1\n99\n")
        self.assertTrue(run_lock_is_free(directory=self.directory))
        self.assertEqual((self.directory / "run.lock").read_text(), "run-job 1\n99\n")
        with self.lock():
            self.assertFalse(run_lock_is_free(directory=self.directory))

    def test_a_child_left_by_a_killed_holder_blocks_the_next_start_but_is_never_killed(self) -> None:
        child = self.start_child()
        holder = self.start_holder(child.pid)
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        with self.assertRaisesRegex(RunLockBusy, rf"draw-things-cli from an earlier run is still running \(PID {child.pid}\)"):
            self.lock(child_check=lambda _pid, _name: True).acquire()
        self.assertIsNone(child.poll())
        # The refusal released the flock, so the guard, not the lock, was what said no.
        self.assertTrue(run_lock_is_free(directory=self.directory))
        child.kill()
        child.wait()
        with self.lock(child_check=lambda _pid, _name: True):
            pass

    def test_a_pid_that_is_not_draw_things_cli_or_is_gone_does_not_block(self) -> None:
        child = self.start_child()
        (self.directory / "run.lock").write_text(f"run-job 1\n{child.pid} draw-things-cli\n")
        with self.lock(child_check=lambda _pid, _name: False):
            pass
        (self.directory / "run.lock").write_text("run-job 1\n99999999\n")
        with self.lock(child_check=lambda _pid, _name: True):
            pass
        (self.directory / "run.lock").write_text("run-job 1\nnot-a-pid\n")
        with self.lock(child_check=lambda _pid, _name: True):
            pass

    def test_the_process_check_matches_only_the_command_name(self) -> None:
        self.assertFalse(run_lock.is_draw_things_process(os.getpid()))
        with mock.patch.object(run_lock.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="/usr/local/bin/draw-things-cli\n")):
            self.assertTrue(run_lock.is_draw_things_process(1234))
            self.assertFalse(run_lock.is_draw_things_process(1234, "other-cli"))
        with mock.patch.object(run_lock.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="/opt/my-wrapper\n")):
            self.assertTrue(run_lock.is_draw_things_process(1234, "my-wrapper"))
        # A system that truncates the command name to 15 characters.
        with mock.patch.object(run_lock.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="a-very-long-exe\n")):
            self.assertTrue(run_lock.is_draw_things_process(1234, "a-very-long-executable"))

    def test_the_refusal_names_the_executable_the_earlier_run_started(self) -> None:
        child = self.start_child()
        (self.directory / "run.lock").write_text(f"run-job 1\n{child.pid} my-wrapper\n")
        seen: list[tuple[int, str]] = []
        with self.assertRaisesRegex(RunLockBusy, rf"A my-wrapper from an earlier run is still running \(PID {child.pid}\)"):
            self.lock(child_check=lambda pid, name: seen.append((pid, name)) or True).acquire()
        self.assertEqual(seen, [(child.pid, "my-wrapper")])

    def test_the_lock_file_is_never_empty_while_it_is_rewritten(self) -> None:
        lock = self.lock()
        lock.acquire()
        self.addCleanup(lock.release)
        writes: list[int] = []
        real_ftruncate = os.ftruncate
        with mock.patch.object(run_lock.os, "ftruncate", side_effect=lambda descriptor, length: (writes.append(len((self.directory / "run.lock").read_bytes())), real_ftruncate(descriptor, length))):
            lock.record_child(4242)
        # Before the cut to length, the new text is already there in full.
        self.assertGreater(writes[0], 0)
        self.assertIn(b"run-job", (self.directory / "run.lock").read_bytes())

    def test_the_state_directory_is_created_on_demand_and_errors_are_reported(self) -> None:
        with mock.patch.object(run_lock, "STATE_DIRECTORY", self.directory / "nested" / "state"):
            self.assertTrue(ensure_state_directory().is_dir())
            with RunLock("generate"):
                self.assertTrue((self.directory / "nested" / "state" / "run.lock").is_file())
        blocker = self.directory / "file"
        blocker.write_text("")
        with mock.patch.object(run_lock, "STATE_DIRECTORY", blocker / "state"):
            with self.assertRaisesRegex(RunLockError, "Cannot create the state directory"):
                RunLock("generate").acquire()
