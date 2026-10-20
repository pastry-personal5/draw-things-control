"""The UTM launcher supervises both services without starting the real queue."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class UtmLauncherTests(unittest.TestCase):
    def test_a_failed_mcp_listener_stops_the_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run-utm.sh"
            shutil.copy2(PROJECT_ROOT / "run-utm.sh", launcher)
            api_pid_file = root / "api.pid"
            self._script(root / "run-host-network-device-server.sh", f'printf "%s\\n" "$$" > "{api_pid_file}"\ntrap "exit 0" TERM\nwhile true; do sleep 0.1; done')
            self._script(root / "run-mcp-server.sh", "sleep 0.2\nexit 7")
            result = subprocess.run([str(launcher)], capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 7, result.stderr)
            pid = int(api_pid_file.read_text(encoding="ascii"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_a_failed_api_stops_the_mcp_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run-utm.sh"
            shutil.copy2(PROJECT_ROOT / "run-utm.sh", launcher)
            mcp_pid_file = root / "mcp.pid"
            self._script(root / "run-host-network-device-server.sh", "sleep 0.2\nexit 9")
            self._script(root / "run-mcp-server.sh", f'printf "%s\\n" "$$" > "{mcp_pid_file}"\ntrap "exit 0" TERM\nwhile true; do sleep 0.1; done')
            result = subprocess.run([str(launcher)], capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 9, result.stderr)
            pid = int(mcp_pid_file.read_text(encoding="ascii"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @staticmethod
    def _script(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
