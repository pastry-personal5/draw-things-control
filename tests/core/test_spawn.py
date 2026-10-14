"""Fork-free child launching for threaded front ends."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from framecontrol.core.process.spawn import popen, run


@unittest.skipUnless(os.name == "posix" and getattr(subprocess, "_USE_POSIX_SPAWN", False), "requires POSIX posix_spawn")
class ForkFreeSpawnTests(unittest.TestCase):
    def test_cwd_and_new_session_do_not_use_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(subprocess, "_fork_exec", side_effect=AssertionError("forked")):
            result = run(
                [sys.executable, "-c", "import os; print(os.getcwd()); print(os.getsid(0) == os.getpid())"],
                cwd=directory,
                start_new_session=True,
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertEqual(result.stdout.splitlines(), [str(Path(directory).resolve()), "True"])

    def test_helper_closes_an_inheritable_parent_descriptor(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.set_inheritable(write_fd, True)
            code = "import os, sys\ntry:\n os.fstat(int(sys.argv[1]))\nexcept OSError:\n print('closed')\nelse:\n print('inherited')"
            result = run([sys.executable, "-c", code, str(write_fd)], capture_output=True, text=True, check=True)
        finally:
            os.close(read_fd)
            os.close(write_fd)

        self.assertEqual(result.stdout.strip(), "closed")

    def test_exec_error_is_reported_before_popen_returns(self) -> None:
        missing = Path(tempfile.gettempdir()) / "framecontrol-missing-executable"
        with self.assertRaises(FileNotFoundError) as raised:
            popen([str(missing)])

        self.assertEqual(raised.exception.filename, str(missing))

    def test_run_preserves_input_output_and_check(self) -> None:
        result = run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())"], input=b"hello", capture_output=True, check=True)
        self.assertEqual(result.stdout, b"HELLO")
        with self.assertRaises(subprocess.CalledProcessError):
            run([sys.executable, "-c", "raise SystemExit(7)"], check=True)


if __name__ == "__main__":
    unittest.main()
