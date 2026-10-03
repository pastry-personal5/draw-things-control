"""``dtc mcp --transport streamable-http`` (Milestone 13): the bearer token every request needs, the server as a process driven by the SDK's client over real HTTP, and the command's refusals. Never starts ``draw-things-cli`` or ``dtc serve``."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from typer.testing import CliRunner

from draw_things_control.cli.app import app
from draw_things_control.mcp_server.http import BearerAuth
from tests.mcp_server.test_process import PROJECT_ROOT, unused_port
from tests.server.test_queue_routes import TOKEN

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class Recorder:
    """An ASGI app that records the scope types it was called with and answers 204."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        self.scopes.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})


class BearerAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token_file = Path(directory.name) / "server-token"
        self.token_file.write_text(f"{TOKEN}\n", encoding="ascii")
        self.inner = Recorder()
        self.guard = BearerAuth(self.inner, self.token_file)

    async def get(self, *headers: tuple[bytes, bytes]) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.guard), base_url="http://test") as client:
            return await client.get("/mcp", headers=[(name.decode("latin-1"), value.decode("latin-1")) for name, value in headers])

    async def test_the_token_in_the_file_passes_and_anything_else_is_a_bare_401_before_the_app_sees_it(self) -> None:
        for header in (b"", b"Bearer", b"Bearer ", b"Bearer wrong", f"Basic {TOKEN}".encode(), TOKEN.encode(), f"Bearer {TOKEN}x".encode(), f"Bearer {TOKEN[:-1]}".encode()):
            with self.subTest(header=header):
                response = await self.get((b"authorization", header)) if header else await self.get()
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(response.json(), {"code": "unauthorized", "message": "A valid bearer token is required"})
        self.assertEqual(self.inner.scopes, [])
        for scheme in ("Bearer", "bearer", "BEARER"):
            with self.subTest(scheme=scheme):
                self.assertEqual((await self.get((b"authorization", f"{scheme} {TOKEN}".encode()))).status_code, 204)

    async def test_the_file_is_read_again_for_each_request_so_a_regenerated_token_counts(self) -> None:
        self.assertEqual((await self.get((b"authorization", f"Bearer {TOKEN}".encode()))).status_code, 204)
        self.token_file.write_text("a-new-token\n", encoding="ascii")
        self.assertEqual((await self.get((b"authorization", f"Bearer {TOKEN}".encode()))).status_code, 401)
        self.assertEqual((await self.get((b"authorization", b"Bearer a-new-token"))).status_code, 204)

    async def test_a_missing_or_empty_token_file_refuses_everyone_without_naming_the_path(self) -> None:
        for write in (lambda: self.token_file.unlink(), lambda: self.token_file.write_text("\n", encoding="ascii")):
            write()
            with self.subTest(file=self.token_file.exists()):
                response = await self.get((b"authorization", f"Bearer {TOKEN}".encode()))
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(str(self.token_file), response.text)

    async def test_the_lifespan_passes_through_unchecked(self) -> None:
        async def receive() -> dict[str, str]:
            return {"type": "lifespan.startup"}

        async def send(message: MutableMapping[str, Any]) -> None:
            return None

        await self.guard({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
        self.assertEqual(self.inner.scopes, ["lifespan"])


class HttpProcessCase(unittest.IsolatedAsyncioTestCase):
    """``dtc mcp --transport streamable-http`` on a loopback port, over a ``dtc serve`` that is down."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token_file = Path(directory.name) / "server-token"
        self.token_file.write_text(TOKEN, encoding="ascii")
        self.port = unused_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.server_url = f"http://127.0.0.1:{unused_port()}"
        self.process = subprocess.Popen([sys.executable, "-m", "draw_things_control", "mcp", "--transport", "streamable-http", "--port", str(self.port), "--server-url", self.server_url, "--token-file", str(self.token_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=PROJECT_ROOT)
        self.addCleanup(self.stop)
        self.wait_until_listening()

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def wait_until_listening(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.assertIsNone(self.process.poll(), "dtc mcp exited before it listened")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail("dtc mcp did not listen in 60 seconds")

    def authorized(self, token: str = TOKEN) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0))


class HttpTransportTests(HttpProcessCase):
    async def test_without_the_token_nothing_is_served(self) -> None:
        async with httpx.AsyncClient() as client:
            for headers in (MCP_HEADERS, {**MCP_HEADERS, "Authorization": "Bearer not-the-token"}):
                response = await client.post(self.url, json=INITIALIZE, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "unauthorized")
                self.assertNotIn("mcp-session-id", response.headers)
            self.assertEqual((await client.get(f"http://127.0.0.1:{self.port}/")).status_code, 401)

    async def test_a_client_with_the_token_lists_the_tools_and_gets_the_apis_error_as_a_result(self) -> None:
        async with self.authorized() as http_client, Client(streamable_http_client(self.url, http_client=http_client)) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            self.assertIn("get_queue_entry", names)
            self.assertNotIn("delete_executions", names)
            result = await client.call_tool("get_queue", {})
            self.assertTrue(result.is_error)
            assert isinstance(result.structured_content, dict)
            self.assertEqual(result.structured_content["code"], "server_unreachable")
            self.assertNotIn(TOKEN, json.dumps(result.structured_content))

    async def test_a_client_with_a_wrong_token_cannot_connect(self) -> None:
        with self.assertRaises(Exception):  # noqa: B017 (the SDK's client raises its own error type or an exception group for the refused handshake)
            async with self.authorized("not-the-token") as http_client, Client(streamable_http_client(self.url, http_client=http_client)) as client:
                await client.list_tools()

    async def test_a_regenerated_token_is_the_one_that_counts_at_once(self) -> None:
        self.token_file.write_text("regenerated", encoding="ascii")
        async with httpx.AsyncClient() as client:
            self.assertEqual((await client.post(self.url, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"})).status_code, 401)
            self.assertEqual((await client.post(self.url, json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer regenerated"})).status_code, 200)


class CommandTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> Any:
        return CliRunner().invoke(app, ["mcp", *arguments])

    def test_an_unknown_transport_is_refused(self) -> None:
        result = self.invoke("--transport", "sse")
        self.assertEqual(result.exit_code, 2, result.output)

    def test_listen_options_without_streamable_http_are_refused_not_ignored(self) -> None:
        for options in (["--host", "127.0.0.1"], ["--port", "9000"], ["--allow-remote-bind"]):
            with self.subTest(options=options):
                self.assertEqual(self.invoke(*options).exit_code, 2)

    def test_a_host_beyond_loopback_is_refused_without_allow_remote_bind_before_anything_starts(self) -> None:
        result = self.invoke("--transport", "streamable-http", "--host", "192.0.2.7")
        self.assertEqual(result.exit_code, 2, result.output)

    def test_a_port_that_is_taken_is_a_clean_exit_2_naming_it(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            result = self.invoke("--transport", "streamable-http", "--port", str(port))
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn(f"127.0.0.1:{port}", result.output)


if __name__ == "__main__":
    unittest.main()
