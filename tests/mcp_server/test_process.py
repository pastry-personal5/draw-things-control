"""``dtc mcp`` as a process (Milestone 10): started with no API running, driven over stdio by the SDK's client and by
hand, its stdout holding protocol messages only; and the checked-in ``.mcp.json`` that registers it with Claude Code."""

from __future__ import annotations

import json
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from draw_things_control.cli.app import app
from tests.server.test_queue_routes import TOKEN

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def unused_port() -> int:
    """A loopback port nothing listens on: bound, then closed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProcessCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token_file = Path(directory.name) / "server-token"
        self.token_file.write_text(TOKEN, encoding="ascii")
        self.server_url = f"http://127.0.0.1:{unused_port()}"
        self.arguments = ["-m", "draw_things_control", "mcp", "--server-url", self.server_url, "--token-file", str(self.token_file)]


class StdioTests(ProcessCase):
    async def test_with_no_api_it_connects_lists_its_tools_and_answers_server_unreachable(self) -> None:
        for mode in ("auto", "legacy"):
            with self.subTest(mode=mode):
                async with Client(StdioServerParameters(command=sys.executable, args=self.arguments, cwd=PROJECT_ROOT), mode=mode) as client:
                    names = [tool.name for tool in (await client.list_tools()).tools]
                    self.assertIn("get_queue_entry", names)
                    self.assertNotIn("delete_executions", names)
                    result = await client.call_tool("get_queue", {})
                    self.assertTrue(result.is_error)
                    assert isinstance(result.structured_content, dict)
                    self.assertEqual(result.structured_content["code"], "server_unreachable")
                    self.assertIn(self.server_url, result.structured_content["message"])

    def test_its_stdout_holds_protocol_messages_only(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_queue", "arguments": {}}},
        ]
        process = subprocess.Popen([sys.executable, *self.arguments], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=PROJECT_ROOT)
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in iter(process.stdout.readline, "")] if process.stdout else None, daemon=True)
        reader.start()
        assert process.stdin is not None
        process.stdin.write("".join(json.dumps(request) + "\n" for request in requests))
        process.stdin.flush()
        messages: list[dict[str, Any]] = []
        # Stdin stays open until the call is answered: at its end the server stops, cancelling what is in flight.
        while not any(message.get("id") == 3 for message in messages):
            messages.append(json.loads(lines.get(timeout=60)))
        process.stdin.close()
        self.assertEqual(process.wait(timeout=30), 0)
        stderr = process.stderr.read() if process.stderr is not None else ""
        self.assertTrue(all(message.get("jsonrpc") == "2.0" for message in messages), messages)
        answers = {message["id"]: message for message in messages if "id" in message}
        self.assertEqual(sorted(answers), [1, 2, 3])
        result = answers[3]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "server_unreachable")
        self.assertNotIn(TOKEN, json.dumps(messages) + stderr)


class CommandTests(unittest.TestCase):
    def test_a_server_url_beyond_loopback_is_refused_before_anything_starts(self) -> None:
        result = CliRunner().invoke(app, ["mcp", "--server-url", "http://192.0.2.7:8765"])
        self.assertEqual(result.exit_code, 2, result.output)


class RegistrationTests(unittest.TestCase):
    def test_the_checked_in_mcp_json_registers_uv_run_dtc_mcp_and_nothing_else(self) -> None:
        config = json.loads((PROJECT_ROOT / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"mcpServers": {"dtc": {"type": "stdio", "command": "uv", "args": ["run", "dtc", "mcp"]}}})


if __name__ == "__main__":
    unittest.main()
