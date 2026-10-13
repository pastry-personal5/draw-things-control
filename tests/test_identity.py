"""Installed and wire identities of the Framecontrol rename."""

from __future__ import annotations

import subprocess
import sys
import unittest
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from framecontrol.mcp_server.app import NAME
from framecontrol.server.app import create_app
from framecontrol.server.caller import HEADER_NAME
from framecontrol.server.context import ServerContext
from framecontrol.server.generated import monitor_pb2
from framecontrol.tui.app import FramecontrolApp


class IdentityTests(unittest.TestCase):
    def test_installed_entry_points(self) -> None:
        installed = distribution("framecontrol")
        self.assertEqual(installed.version, "3.0.0")
        self.assertEqual([(entry.name, entry.value) for entry in installed.entry_points if entry.group == "console_scripts"], [("fc", "framecontrol.cli.app:main")])
        for command in ([sys.executable, "-m", "framecontrol", "--help"], [str(Path(sys.executable).with_name("fc")), "--help"]):
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertIn("Usage: fc", result.stdout)
        self.assertIsNone(find_spec("framechain"))

    def test_interface_identities(self) -> None:
        self.assertEqual(FramecontrolApp.TITLE, "Framecontrol")
        self.assertEqual(NAME, "framecontrol")
        self.assertEqual(HEADER_NAME, "X-Framecontrol-Caller")
        self.assertEqual(monitor_pb2.DESCRIPTOR.package, "framecontrol.monitor")
        context = cast(ServerContext, SimpleNamespace(allow_write=False, bound_host="127.0.0.1", bound_port=8765))
        self.assertEqual(create_app(context).title, "Framecontrol")
