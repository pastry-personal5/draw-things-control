"""The MCP server's HTTP client (Milestone 10): its copy of reading the token agrees with ``core/client_config.py``'s,
the path rule, and its resources, the job files, read through the API."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import mcp_types as types
from loguru import logger
from mcp.shared.exceptions import MCPError

from draw_things_control.core.client_config import read_client_token
from draw_things_control.core.errors import StateUnavailableError
from draw_things_control.mcp_server.api import ToolError, path_segment, read_token
from draw_things_control.mcp_server.app import RESOURCE_NOT_FOUND
from tests.mcp_server.mcp_case import McpCase
from tests.server.test_queue_routes import TOKEN


def outcome(read: Any, path: Path) -> str | None:
    """The token read, or None when the read was refused."""
    try:
        return read(path)
    except (StateUnavailableError, ToolError):
        return None


class TokenCopyTests(unittest.TestCase):
    def test_the_copy_reads_a_token_file_as_the_original_does(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server-token"
            for content in (None, "", " \n", f"  {TOKEN}\n", TOKEN):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_text(content, encoding="ascii")
                    self.assertEqual(outcome(read_token, path), outcome(read_client_token, path))
            self.assertEqual(read_token(path), TOKEN)

    def test_a_refusal_is_unauthorized_naming_the_file_and_dtc_serve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server-token"
            for content in (None, b"", "tökén".encode()):
                with self.subTest(content=content):
                    if content is not None:
                        path.write_bytes(content)
                    with self.assertRaises(ToolError) as caught:
                        read_token(path)
                    self.assertEqual(caught.exception.code, "unauthorized")
                    self.assertIn(str(path), str(caught.exception))
                    self.assertIn("dtc serve", str(caught.exception))


class PathSegmentTests(unittest.TestCase):
    def test_empty_dots_and_slashes_are_refused_naming_the_argument(self) -> None:
        for value in ("", ".", "..", "....", "/", "a/b", "../x"):
            with self.subTest(value=value):
                with self.assertRaises(ToolError) as caught:
                    path_segment("job", value)
                self.assertEqual((caught.exception.body["code"], caught.exception.body["field"]), ("invalid_input", "job"))

    def test_anything_else_is_one_percent_encoded_segment(self) -> None:
        self.assertEqual([path_segment("job", value) for value in ("walk.yaml", "?x", "#x", "%2F", "a b", "a.")], ["walk.yaml", "%3Fx", "%23x", "%252F", "a%20b", "a."])


class ResourceTests(McpCase):
    async def test_job_files_are_listed_and_read_through_the_api(self) -> None:
        walk = self.write_catalog_job("walk.yaml")
        self.write_catalog_job("dusk.yaml")
        async with self.client() as client:
            listed = await client.list_resources()
            self.assertEqual(sorted(str(resource.uri) for resource in listed.resources), ["job://dusk.yaml", "job://walk.yaml"])
            self.assertEqual({resource.mime_type for resource in listed.resources}, {"application/yaml"})
            self.assertEqual(listed.ttl_ms, 5000)
            [template] = (await client.list_resource_templates()).resource_templates
            self.assertEqual(template.uri_template, "job://{job}")
            for uri in ("job://walk.yaml", "job://J0001"):
                [contents] = (await client.read_resource(uri)).contents
                assert isinstance(contents, types.TextResourceContents)
                self.assertEqual((contents.text, contents.mime_type), (walk, "application/yaml"))
        self.assertTrue(all(path.startswith("/v1/jobs") for path in (path for _method, path in self.transport.sent())))

    async def test_a_bad_or_unknown_job_uri_is_refused(self) -> None:
        async with self.client() as client:
            for uri, code in (("job://..", types.INVALID_PARAMS), ("job://a%2Fb", types.INVALID_PARAMS), ("job://", types.INVALID_PARAMS), ("file:///etc/passwd", types.INVALID_PARAMS), ("job://nope.yaml", RESOURCE_NOT_FOUND)):
                with self.subTest(uri=uri):
                    with self.assertRaises(MCPError) as caught:
                        await client.read_resource(uri)
                    self.assertEqual(caught.exception.error.code, code)
        self.assertEqual(self.transport.sent(), [("GET", "/v1/jobs/nope.yaml")])


class LogTests(McpCase):
    async def test_the_token_is_in_no_log_line(self) -> None:
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        self.addCleanup(logger.remove, sink)
        async with self.client() as client:
            await client.list_tools()
            self.context.token = "b" * 64
            await client.call_tool("get_queue", {})
            self.transport.app = None
            await client.call_tool("get_queue", {})
            self.serve(allow_write=True)
            self.now += 10
            await client.call_tool("get_queue", {})
        self.assertTrue(lines, "nothing was logged, so this checks nothing")
        self.assertFalse([line for line in lines if TOKEN in line])


if __name__ == "__main__":
    unittest.main()
