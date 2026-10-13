"""Installed and wire identities of the Framechain rename."""

from __future__ import annotations

import subprocess
import sys
import unittest
from importlib.metadata import distribution
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from framechain.mcp_server.app import NAME
from framechain.server.app import create_app
from framechain.server.caller import HEADER_NAME
from framechain.server.context import ServerContext
from framechain.server.generated import monitor_pb2
from framechain.tui.app import FramechainApp


class IdentityTests(unittest.TestCase):
    def test_installed_entry_points(self) -> None:
        installed = distribution("framechain")
        self.assertEqual(installed.version, "3.0.0")
        self.assertEqual([(entry.name, entry.value) for entry in installed.entry_points if entry.group == "console_scripts"], [("fc", "framechain.cli.app:main")])
        for command in ([sys.executable, "-m", "framechain", "--help"], [str(Path(sys.executable).with_name("fc")), "--help"]):
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertIn("Usage: fc", result.stdout)

    def test_interface_identities(self) -> None:
        self.assertEqual(FramechainApp.TITLE, "Framechain")
        self.assertEqual(NAME, "framechain")
        self.assertEqual(HEADER_NAME, "X-Framechain-Caller")
        self.assertEqual(monitor_pb2.DESCRIPTOR.package, "framechain.monitor")
        context = cast(ServerContext, SimpleNamespace(allow_write=False, bound_host="127.0.0.1", bound_port=8765))
        self.assertEqual(create_app(context).title, "Framechain")
