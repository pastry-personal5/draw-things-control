"""A job's runner reports its child to the lock it was given, with no module state, so a killed holder still blocks."""

import os
import signal
import subprocess
import sys
import time

from draw_things_control.core.run_lock import RunLock, RunLockBusy
from tests.fixtures import JobTestCase, job_data

# Holds the lock and runs a one-run job whose executable sleeps; the lock reaches the runner only through run().
HOLDER = """
import sys
from pathlib import Path
from draw_things_control.cli.app import create_job_service
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.job_definition import load_job
root, executable = Path(sys.argv[1]), sys.argv[2]
job = load_job(root / "job.yaml", GlobalConfig(input_directory=root / "input", output_directory=root / "output"), root / "dt-config")
lock = RunLock("run-job", directory=root / "state")
lock.acquire()
print("held", flush=True)
create_job_service().run(job, executable=executable, shutdown_grace=1, on_child_start=lock.record_child)
"""


class RunLockReportingTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "state").mkdir()
        self.write_job(job_data(mode="i2i", run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        # A stand-in for draw-things-cli that runs until it is killed; never the real one.
        self.executable = self.root / "fake-draw-things-cli"
        self.executable.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.lock_file = self.root / "state" / "run.lock"

    def wait_for_child(self) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            lines = self.lock_file.read_text(encoding="utf-8").splitlines() if self.lock_file.exists() else []
            if len(lines) == 2:
                pid, name = lines[1].split()
                self.assertEqual(name, "fake-draw-things-cli")
                return int(pid)
            time.sleep(0.05)
        self.fail("the job's runner never reported its child to the lock")

    def test_a_killed_holders_child_blocks_the_next_starter(self) -> None:
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.root), str(self.executable)], stdout=subprocess.PIPE, text=True)
        child: int | None = None
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "held")
            child = self.wait_for_child()
            holder.send_signal(signal.SIGKILL)
            holder.wait()
            # ps would name the script's interpreter, so the check trusts the recorded name.
            with self.assertRaisesRegex(RunLockBusy, rf"fake-draw-things-cli from an earlier run is still running \(PID {child}\)"):
                RunLock("run-job", directory=self.root / "state", retry_seconds=0, child_check=lambda _pid, _name: True).acquire()
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait()
            holder.stdout.close()
            if child is not None:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
