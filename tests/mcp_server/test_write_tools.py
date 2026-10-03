"""The MCP server's write tools (Milestone 10): listed only while ``dtc serve`` has writes on, a list-changed
notification in each protocol era when that changes, a job file's whole life, and ``delete_executions`` behind a fresh
read of the capabilities and a required dry run."""

from __future__ import annotations

from typing import Any

import anyio
import httpx
from mcp.shared.subscriptions import ToolsListChanged

from tests.mcp_server.mcp_case import McpCase
from tests.mcp_server.test_tools import READ_RUN_AND_QUEUE_CONTROL

WRITE_TOOLS = ["create_job", "replace_job", "delete_job", "delete_executions"]


class WriteToolTests(McpCase):
    async def test_with_writes_on_the_write_tools_are_listed(self) -> None:
        self.serve(allow_write=True)
        async with self.client() as client:
            tools = (await client.list_tools()).tools
        self.assertEqual([tool.name for tool in tools], READ_RUN_AND_QUEUE_CONTROL + WRITE_TOOLS)
        hints = {tool.name: tool.annotations.model_dump() for tool in tools if tool.annotations is not None}
        for name in ("replace_job", "delete_job", "delete_executions"):
            self.assertTrue(hints[name]["destructive_hint"], name)
        self.assertIs(hints["create_job"]["destructive_hint"], False)

    async def test_a_job_files_whole_life_through_its_sha256(self) -> None:
        self.serve(allow_write=True)
        text = self.write_catalog_job("draft.yaml")
        (self.paths.jobs / "draft.yaml").unlink()
        async with self.client() as client:
            created = await self.ok(client, "create_job", {"name": "sunset-walk", "yaml": text})
            self.assertEqual(self.transport.sent()[-1], ("PUT", "/v1/jobs/sunset-walk"))
            self.assertEqual(created["text"], text)
            brief = await self.ok(client, "get_job", {"job": "sunset-walk.yaml"})
            self.assertNotIn("text", brief)
            full = await self.ok(client, "get_job", {"job": "sunset-walk.yaml", "brief": False})
            self.assertEqual((full["text"], full["sha256"]), (text, created["sha256"]))
            edited = text.replace("positive: text", "positive: other words")
            stale = await self.error(client, "replace_job", {"name": "sunset-walk", "yaml": edited, "expected_sha256": "0" * 64})
            self.assertEqual((stale["code"], stale["current_sha256"]), ("conflict", full["sha256"]))
            replaced = await self.ok(client, "replace_job", {"name": "sunset-walk", "yaml": edited, "expected_sha256": full["sha256"]})
            self.assertEqual(self.transport.sent()[-1], ("PUT", "/v1/jobs/sunset-walk?overwrite=1"))
            self.assertEqual((self.paths.jobs / "sunset-walk.yaml").read_text(encoding="utf-8"), edited)
            deleted = await self.ok(client, "delete_job", {"name": "sunset-walk", "expected_sha256": replaced["sha256"]})
            self.assertEqual(self.transport.sent()[-1], ("DELETE", f"/v1/jobs/sunset-walk?expected_sha256={replaced['sha256']}"))
            self.assertTrue((self.paths.root / deleted["trash_path"]).is_file())
        self.assertFalse((self.paths.jobs / "sunset-walk.yaml").exists())
        audit = (await self.api("GET", "/v1/audit")).json()["audit"]
        self.assertEqual([(row["action"], row["outcome"], row["caller"]) for row in reversed(audit)], [("create_job", "ok", "mcp"), ("replace_job", "conflict", "mcp"), ("replace_job", "ok", "mcp"), ("delete_job", "ok", "mcp")])

    async def test_an_execution_is_deleted_after_a_dry_run(self) -> None:
        self.serve(allow_write=True)
        self.write_catalog_job("walk.yaml", run_count=7)
        queue_id = (await self.api("POST", "/v1/queue", json={"job": "walk.yaml"}, caller="mcp")).json()["queue_id"]
        self.make_running(queue_id)
        execution_id = self.interrupt_with_runs(queue_id)
        async with self.client() as client:
            dry = await self.ok(client, "delete_executions", {"executions": [execution_id], "dry_run": True})
            self.assertEqual((dry["dry_run"], dry["deleted"]), (True, [execution_id]))
            self.assertIsNotNone(self.store.executions.by_number(int(execution_id[1:])))
            done = await self.ok(client, "delete_executions", {"executions": [execution_id], "dry_run": False})
            self.assertEqual((done["dry_run"], done["deleted"]), (False, [execution_id]))
        self.assertIsNone(self.store.executions.by_number(int(execution_id[1:])))
        audit = (await self.api("GET", "/v1/audit")).json()["audit"]
        self.assertEqual([(row["action"], row["target"], row["caller"]) for row in audit if row["action"] == "delete_execution"], [("delete_execution", execution_id, "mcp")])

    async def test_with_writes_off_a_job_file_tool_answers_the_apis_writes_off(self) -> None:
        async with self.client() as client:
            refused = await self.error(client, "create_job", {"name": "sunset-walk", "yaml": "name: sunset-walk\n"})
        self.assertEqual(refused["code"], "writes_off")
        self.assertIn("--allow-write", refused["message"])

    async def test_with_writes_off_delete_executions_is_refused_before_its_endpoint(self) -> None:
        async with self.client() as client:
            self.now += 1
            refused = await self.error(client, "delete_executions", {"executions": ["E0001"], "dry_run": True})
            self.assertEqual(refused, {"code": "writes_off", "message": "Deleting executions through MCP requires dtc serve --allow-write"})
            self.assertEqual(self.transport.sent(), [])
            self.assertEqual(self.transport.requests[-1], ("GET", "/v1/capabilities"))

    async def test_a_capabilities_read_that_times_out_says_nothing_was_deleted(self) -> None:
        self.serve(allow_write=True)
        handle = self.transport.handle_async_request

        async def slow_capabilities(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/capabilities":
                raise httpx.ReadTimeout("timed out", request=request)
            return await handle(request)

        self.transport.handle_async_request = slow_capabilities
        async with self.client() as client:
            refused = await self.error(client, "delete_executions", {"executions": ["E0001"], "dry_run": False})
        self.assertEqual(refused["code"], "server_timeout")
        self.assertIn("nothing else was sent", refused["message"])
        self.assertEqual(self.transport.sent(), [])

    async def test_delete_executions_reads_the_capabilities_afresh_each_time(self) -> None:
        self.serve(allow_write=True)
        async with self.client() as client:
            await client.list_tools()
            self.serve(allow_write=False)
            refused = await self.error(client, "delete_executions", {"executions": ["E0001"], "dry_run": True})
            self.assertEqual(refused["code"], "writes_off")
            self.assertEqual(self.transport.sent(), [])

    async def test_delete_executions_needs_its_dry_run_and_one_to_200_ids(self) -> None:
        self.serve(allow_write=True)
        async with self.client() as client:
            for arguments, field in (({"executions": ["E0001"]}, "dry_run"), ({"executions": [], "dry_run": True}, "executions"), ({"executions": ["E0001"] * 201, "dry_run": True}, "executions"), ({"executions": [1], "dry_run": True}, "executions"), ({"executions": ["E0001"], "dry_run": "yes"}, "dry_run")):
                with self.subTest(arguments=arguments):
                    refused = await self.error(client, "delete_executions", arguments)
                    self.assertEqual((refused["code"], refused["field"]), ("invalid_input", field))
            self.assertEqual(self.transport.sent(), [])


class ToolListChangeTests(McpCase):
    """A server restarted with the other ``--allow-write`` setting, without restarting ``dtc mcp``."""

    async def names(self, client: Any) -> list[str]:
        return [tool.name for tool in (await client.list_tools()).tools]

    async def restart(self, client: Any, *, allow_write: bool) -> None:
        """Restart ``dtc serve`` with ``allow_write``, and make a call once the MCP server's last read is stale."""
        self.serve(allow_write=allow_write)
        self.now += 6
        await self.ok(client, "get_queue")

    async def test_a_2026_client_listening_is_told_and_its_next_list_changes(self) -> None:
        async with self.client() as client:
            self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL)
            async with client.listen(tools_list_changed=True) as subscription:
                await self.restart(client, allow_write=True)
                with anyio.fail_after(5):
                    self.assertIsInstance(await anext(subscription), ToolsListChanged)
                self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL + WRITE_TOOLS)
                await self.restart(client, allow_write=False)
                with anyio.fail_after(5):
                    self.assertIsInstance(await anext(subscription), ToolsListChanged)
                self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL)

    async def test_a_2025_client_is_told_on_its_session_once_a_change(self) -> None:
        methods: list[str] = []

        async def on_message(message: Any) -> None:
            method = getattr(message, "method", None) or getattr(getattr(message, "root", None), "method", None)
            if method is not None:
                methods.append(method)

        async with self.client(mode="legacy", message_handler=on_message) as client:
            capabilities = client.session.server_capabilities
            self.assertTrue(capabilities is not None and capabilities.tools is not None and capabilities.tools.list_changed)
            self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL)
            await self.restart(client, allow_write=True)
            await self.restart(client, allow_write=True)
            with anyio.fail_after(5):
                while "notifications/tools/list_changed" not in methods:
                    await anyio.sleep(0.01)
            self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL + WRITE_TOOLS)
        self.assertEqual(methods.count("notifications/tools/list_changed"), 1)

    async def test_one_change_reaches_a_client_of_each_era(self) -> None:
        methods: list[str] = []

        async def on_message(message: Any) -> None:
            method = getattr(message, "method", None) or getattr(getattr(message, "root", None), "method", None)
            if method is not None:
                methods.append(method)

        async with self.client(mode="legacy", message_handler=on_message) as legacy, self.client() as modern:
            await legacy.list_tools()
            await modern.list_tools()
            async with modern.listen(tools_list_changed=True) as subscription:
                await self.restart(modern, allow_write=True)
                with anyio.fail_after(5):
                    self.assertIsInstance(await anext(subscription), ToolsListChanged)
                    while "notifications/tools/list_changed" not in methods:
                        await anyio.sleep(0.01)

    async def test_a_change_one_client_lists_first_reaches_the_others(self) -> None:
        methods: list[str] = []

        async def on_message(message: Any) -> None:
            method = getattr(message, "method", None) or getattr(getattr(message, "root", None), "method", None)
            if method is not None:
                methods.append(method)

        async with self.client(mode="legacy", message_handler=on_message) as legacy, self.client() as modern:
            await legacy.list_tools()
            await modern.list_tools()
            self.serve(allow_write=True)
            # Past the list's cache hint, as a client that asks again after it would.
            self.assertEqual([tool.name for tool in (await modern.list_tools(cache_mode="refresh")).tools], READ_RUN_AND_QUEUE_CONTROL + WRITE_TOOLS)
            with anyio.fail_after(5):
                while "notifications/tools/list_changed" not in methods:
                    await anyio.sleep(0.01)

    async def test_get_capabilities_reads_them_once_and_is_the_read(self) -> None:
        async with self.client() as client:
            await client.list_tools()
            self.serve(allow_write=True)
            self.now += 6
            async with client.listen(tools_list_changed=True) as subscription:
                reads = self.transport.requests.count(("GET", "/v1/capabilities"))
                result = await client.session.call_tool("get_capabilities", {})
                self.assertEqual(self.transport.requests.count(("GET", "/v1/capabilities")), reads + 1)
                assert isinstance(result.structured_content, dict)
                self.assertTrue(result.structured_content["allow_write"])
                with anyio.fail_after(5):
                    self.assertIsInstance(await anext(subscription), ToolsListChanged)

    async def test_an_api_down_when_listed_and_up_with_writes_on_changes_the_list(self) -> None:
        self.transport.app = None
        async with self.client() as client:
            self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL)
            async with client.listen(tools_list_changed=True) as subscription:
                await self.restart(client, allow_write=True)
                with anyio.fail_after(5):
                    self.assertIsInstance(await anext(subscription), ToolsListChanged)
            self.assertEqual(await self.names(client), READ_RUN_AND_QUEUE_CONTROL + WRITE_TOOLS)

    async def test_a_call_within_five_seconds_of_the_last_read_reads_nothing_more(self) -> None:
        async with self.client() as client:
            await client.list_tools()
            reads = self.transport.requests.count(("GET", "/v1/capabilities"))
            self.now += 4
            await client.session.call_tool("get_queue", {})
            self.assertEqual(self.transport.requests.count(("GET", "/v1/capabilities")), reads)
            self.now += 2
            await client.session.call_tool("get_queue", {})
            self.assertEqual(self.transport.requests.count(("GET", "/v1/capabilities")), reads + 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
