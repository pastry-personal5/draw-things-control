"""Tests for the queue host: the run lock and store for its whole lifetime, recovery, and the worker's lifecycle."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from draw_things_control.core.errors import BusyError
from draw_things_control.core.run_lock import is_draw_things_process, run_lock_is_free
from draw_things_control.services.queue_host import QueueHost
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.executions import NewExecution
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor
from tests.jobs.test_executor import FakeResult, FakeRunner

NOW = datetime(2026, 9, 27, 15, 30, 12)

# Holds the "serve" lock and records another process as its child, as a real dtc serve would; killed with SIGKILL so
# the flock is released (the OS does that) but the file still names the child, which survives independently.
HOLDER = """
import sys
from pathlib import Path
from draw_things_control.core.run_lock import RunLock
lock = RunLock("serve", directory=Path(sys.argv[1]))
lock.acquire()
lock.record_child(int(sys.argv[2]), "draw-things-cli")
print("held", flush=True)
import time
time.sleep(60)
"""


def extract_frame(_video: Path, png: Path) -> None:
    png.write_bytes(b"png")


class QueueHostTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.starts = 0

        def create_runner(arguments: Any, *_rest: Any) -> FakeRunner:
            self.starts += 1
            return FakeRunner(arguments, FakeResult(), write_output=True)

        self.executor = job_executor(runner_factory=create_runner, find_executable=lambda name: name, frame_extractor=extract_frame, require_ffmpeg=lambda: "ffmpeg", handle_signals=False, cooldown=lambda seconds: seconds)

    def host(self, *, child_check: Callable[[int, str], bool] = is_draw_things_process) -> QueueHost:
        return QueueHost(self.paths, self.executor, self.global_config, executable="draw-things-cli", shutdown_grace=1, clock=lambda: NOW, child_check=child_check)

    def submit_ahead_of_time(self) -> None:
        """Submit a job through a store opened before the host starts, as an owner queueing work while the server is down would."""
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        submit_job(path, self.global_config, self.params, store)
        store.close()

    def test_start_runs_a_queued_job_and_stop_releases_the_lock(self) -> None:
        self.submit_ahead_of_time()
        host = self.host()
        host.start()
        try:
            self.wait_for(lambda: self.starts == 1)
        finally:
            host.stop()
        self.assertTrue(run_lock_is_free(directory=self.paths.state))

    def test_a_second_host_is_refused_while_the_first_is_up(self) -> None:
        host = self.host()
        host.start()
        try:
            with self.assertRaises(BusyError):
                self.host().start()
        finally:
            host.stop()

    def test_starting_while_an_earlier_servers_child_is_alive_refuses_naming_its_pid(self) -> None:
        child = subprocess.Popen(["sleep", "5"], start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.paths.state), str(child.pid)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "held")
            holder.send_signal(signal.SIGKILL)
            holder.wait()
            with self.assertRaisesRegex(BusyError, str(child.pid)):
                self.host(child_check=lambda _pid, _name: True).start()
            self.assertIsNone(self.host().worker)
            self.assertEqual(self.starts, 0)
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait()
            assert holder.stdout is not None
            holder.stdout.close()

    def test_a_running_entry_left_by_a_crash_is_interrupted_and_queued_successors_run(self) -> None:
        # RunLock.acquire()'s own orphaned-child guard (tests/core/test_run_lock.py) is what keeps a second server
        # from ever reaching recovery while an earlier one's draw-things-cli survives it; recovery itself only ever
        # runs once that guard has already let a starter through, which is what this test exercises.
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        crashed = submit_job(path, self.global_config, self.params, store)
        second = submit_job(path, self.global_config, self.params, store)
        claimed = store.queue.claim_oldest(NOW)
        assert claimed is not None
        # A crash after JobStarted: the execution row exists and was itself left 'running'.
        execution_id = store.executions.start(NewExecution(job_name="sunset-walk", job_file="job.yaml", mode="i2v", started_at="2026-09-27T10:00:00+00:00"))
        execution_number = store.executions.number_of(execution_id)
        assert execution_number is not None
        store.queue.link_execution(claimed.id, execution_number)
        store.close()
        host = self.host()
        host.start()
        try:
            self.wait_for(lambda: self.starts == 1)
        finally:
            host.stop()
        browse = Store.open(self.paths.database, mode=StoreMode.BROWSE)
        crashed_row = browse.queue.get(crashed.id)
        second_row = browse.queue.get(second.id)
        assert crashed_row is not None and second_row is not None
        self.assertEqual(crashed_row.state, str(QueueState.INTERRUPTED))
        self.assertEqual(second_row.state, str(QueueState.SUCCEEDED))
        browse.close()

    @staticmethod
    def wait_for(condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for the condition")
            time.sleep(0.005)
