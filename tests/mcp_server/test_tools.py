"""The MCP server's read, run, and queue control tools (Milestone 10), through the SDK's in-memory client, with the API
in process: each tool calls its endpoint and returns its body, as structured content and as text; arguments are
checked before any request; and no argument reaches an endpoint other than its tool's."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from mcp.shared.exceptions import MCPError

from draw_things_control.core.global_config import ApiLimits
from draw_things_control.mcp_server.tools import TOOLS
from tests.mcp_server.mcp_case import McpCase
from tests.server.test_queue_routes import TOKEN

READ_RUN_AND_QUEUE_CONTROL = ["get_capabilities", "list_jobs", "get_job", "preview_job", "validate_job_text", "list_inputs", "submit_job", "get_queue", "get_queue_entry", "cancel_queue_entry", "resume_queue_entry", "list_executions", "get_execution", "get_execution_run", "list_outputs", "park_queue_entry", "unpark_queue_entry", "hold_queue", "release_queue"]
# Each tool that puts an argument in a URL path: the argument, the others it needs, and the request it makes for an
# argument value, method and path.
PATH_TOOLS: list[tuple[str, str, dict[str, Any], str, str]] = [
    ("get_job", "job", {}, "GET", "/v1/jobs/{}?brief=1"),
    ("preview_job", "job", {}, "GET", "/v1/jobs/{}/preview"),
    ("get_queue_entry", "queue_id", {}, "GET", "/v1/queue/{}"),
    ("cancel_queue_entry", "queue_id", {}, "POST", "/v1/queue/{}/cancel"),
    ("resume_queue_entry", "queue_id", {}, "POST", "/v1/queue/{}/resume"),
    ("park_queue_entry", "queue_id", {}, "POST", "/v1/queue/{}/park"),
    ("unpark_queue_entry", "queue_id", {}, "POST", "/v1/queue/{}/unpark"),
    ("get_execution", "execution_id", {}, "GET", "/v1/executions/{}?brief=1"),
    ("get_execution_run", "execution_id", {"run": 1}, "GET", "/v1/executions/{}/runs/1"),
    ("list_outputs", "execution_id", {}, "GET", "/v1/executions/{}/outputs"),
]


class ListTests(McpCase):
    async def test_with_writes_off_the_read_run_and_queue_control_tools_are_listed(self) -> None:
        async with self.client() as client:
            tools = (await client.list_tools()).tools
        self.assertEqual([tool.name for tool in tools], READ_RUN_AND_QUEUE_CONTROL)
        for tool in tools:
            with self.subTest(tool.name):
                self.assertEqual((tool.input_schema["type"], tool.input_schema["additionalProperties"]), ("object", False))
                self.assertTrue(tool.description)
                assert tool.annotations is not None
                self.assertFalse(tool.annotations.open_world_hint)
        hints = {tool.name: tool.annotations.model_dump() for tool in tools if tool.annotations is not None}
        for name in ("get_job", "validate_job_text", "get_execution_run", "list_outputs"):
            self.assertTrue(hints[name]["read_only_hint"], name)
        self.assertTrue(hints["cancel_queue_entry"]["destructive_hint"])
        for name in ("submit_job", "park_queue_entry", "resume_queue_entry"):
            self.assertIs(hints[name]["destructive_hint"], False, name)
        for name in ("hold_queue", "release_queue"):
            self.assertTrue(hints[name]["idempotent_hint"], name)

    async def test_no_tool_takes_a_path_a_flag_or_a_credential(self) -> None:
        for tool in TOOLS.values():
            for name in tool.properties:
                self.assertNotIn(name, ("path", "file", "token", "authorization", "api_key", "flags", "args", "executable", "server_url"), tool.name)

    async def test_the_list_carries_a_cache_hint_of_five_seconds(self) -> None:
        async with self.client() as client:
            self.assertEqual((await client.list_tools()).ttl_ms, 5000)

    async def test_an_unknown_tool_is_a_protocol_error(self) -> None:
        async with self.client() as client:
            with self.assertRaisesRegex(MCPError, "Unknown tool: nope"):
                await client.call_tool("nope", {})


class ReadToolTests(McpCase):
    async def test_each_read_tool_returns_its_endpoints_body(self) -> None:
        self.write_catalog_job("walk.yaml", run_count=3)
        queue_id = (await self.api("POST", "/v1/queue", json={"job": "walk.yaml"}, caller="tui")).json()["queue_id"]
        self.make_running(queue_id)
        execution_id = self.interrupt_with_runs(queue_id)
        cases = [
            ("get_capabilities", {}, "/v1/capabilities"),
            ("list_jobs", {}, "/v1/jobs?limit=50"),
            ("get_job", {"job": "walk.yaml"}, "/v1/jobs/walk.yaml?brief=1"),
            ("get_job", {"job": "J0001", "brief": False}, "/v1/jobs/J0001"),
            ("preview_job", {"job": "walk.yaml"}, "/v1/jobs/walk.yaml/preview"),
            ("list_inputs", {"limit": 5}, "/v1/inputs?limit=5"),
            ("get_queue", {"state": "interrupted"}, "/v1/queue?limit=50&state=interrupted"),
            ("get_queue", {"limit": 500, "cursor": "MA"}, "/v1/queue?limit=500&cursor=MA"),
            ("get_queue_entry", {"queue_id": queue_id}, f"/v1/queue/{queue_id}"),
            ("list_executions", {"name": "walk", "status": "interrupted"}, "/v1/executions?limit=50&status=interrupted&name=walk"),
            ("get_execution", {"execution_id": execution_id}, f"/v1/executions/{execution_id}?brief=1"),
            ("get_execution_run", {"execution_id": execution_id, "run": 2}, f"/v1/executions/{execution_id}/runs/2"),
            ("list_outputs", {"execution_id": execution_id}, f"/v1/executions/{execution_id}/outputs"),
        ]
        async with self.client() as client:
            for name, arguments, path in cases:
                with self.subTest(name, arguments=arguments):
                    body = await self.ok(client, name, arguments)
                    self.assertIn(("GET", path), self.transport.requests[-2:] if name == "get_capabilities" else self.transport.sent()[-1:])
                    expected = (await self.api("GET", path)).json()
                    if name == "preview_job":
                        # Each preview names its outputs by the time it is made.
                        body, expected = ([run["number"] for run in answer["runs"]] for answer in (body, expected))
                    self.assertEqual(body, expected)
        self.assertNotIn("text", (await self.api("GET", "/v1/jobs/walk.yaml?brief=1")).json())

    async def test_validate_job_text_checks_a_draft_without_writing_it(self) -> None:
        text = self.write_catalog_job("draft.yaml")
        (self.paths.jobs / "draft.yaml").unlink()
        async with self.client() as client:
            body = await self.ok(client, "validate_job_text", {"yaml": text, "name": "sunset-walk"})
            self.assertEqual((body["name"], len(body["sha256"])), ("sunset-walk", 64))
            self.assertEqual(self.transport.sent()[-1], ("POST", "/v1/validate?name=sunset-walk"))
            refused = await self.error(client, "validate_job_text", {"yaml": text.replace("run_timeout_seconds: 60\n", "")})
            self.assertEqual(refused["code"], "timeout_required")
        self.assertFalse((self.paths.jobs / "draft.yaml").exists())


class FlowTests(McpCase):
    async def test_an_agents_flow_through_the_queue_is_audited_as_mcp(self) -> None:
        text = self.write_catalog_job("walk.yaml", run_count=7)
        async with self.client() as client:
            inputs = await self.ok(client, "list_inputs")
            self.assertEqual([image["path"] for image in inputs["inputs"]], ["first-frame.png"])
            await self.ok(client, "validate_job_text", {"yaml": text})
            entry = await self.ok(client, "submit_job", {"job": "walk.yaml"})
            queue_id = entry["queue_id"]
            self.assertEqual((entry["state"], entry["submitted_by"]), ("queued", "mcp"))
            self.assertEqual((await self.ok(client, "get_queue_entry", {"queue_id": queue_id}))["state"], "queued")
            self.make_running(queue_id)
            parked = await self.ok(client, "park_queue_entry", {"queue_id": queue_id})
            self.assertEqual((parked["park_requested"], parked["held_by"], parked["hold_caller"]), (True, queue_id, "mcp"))
            self.assertFalse((await self.ok(client, "unpark_queue_entry", {"queue_id": queue_id}))["held"])
            self.assertTrue((await self.ok(client, "hold_queue"))["changed"])
            self.assertFalse((await self.ok(client, "hold_queue"))["changed"])
            self.assertTrue((await self.ok(client, "release_queue"))["changed"])
            await self.ok(client, "cancel_queue_entry", {"queue_id": queue_id})
            self.assertEqual(len(self.worker.cancelled), 1)
            execution_id = self.interrupt_with_runs(queue_id)
            resumed = await self.ok(client, "resume_queue_entry", {"queue_id": queue_id})
            self.assertEqual((resumed["resumes"], resumed["submitted_by"]), (queue_id, "mcp"))
            brief = await self.ok(client, "get_execution", {"execution_id": execution_id})
            self.assertEqual(brief["runs"][0]["checks"], [{"stage": "video", "verdict": "pass"}])
            self.assertIn("command", await self.ok(client, "get_execution_run", {"execution_id": execution_id, "run": 1}))
            outputs = await self.ok(client, "list_outputs", {"execution_id": execution_id})
            self.assertEqual([output["complete"] for output in outputs["outputs"]], [True, True, True])
        audit = (await self.api("GET", "/v1/audit")).json()["audit"]
        self.assertEqual({row["action"] for row in audit}, {"submit", "park", "unpark", "hold", "release", "cancel", "resume"})
        self.assertEqual({(row["caller"], row["outcome"]) for row in audit}, {("mcp", "ok")})

    async def test_an_agent_is_refused_a_persons_entry_and_hold(self) -> None:
        self.write_catalog_job("walk.yaml", run_count=3)
        queue_id = (await self.api("POST", "/v1/queue", json={"job": "walk.yaml"}, caller="tui")).json()["queue_id"]
        self.make_running(queue_id)
        await self.api("POST", f"/v1/queue/{queue_id}/park", caller="tui")
        async with self.client() as client:
            for name in ("cancel_queue_entry", "park_queue_entry", "unpark_queue_entry", "resume_queue_entry"):
                refused = await self.error(client, name, {"queue_id": queue_id})
                self.assertEqual(refused["code"], "not_permitted", name)
            self.assertEqual((await self.error(client, "release_queue"))["code"], "not_permitted")
        detail = (await self.api("GET", f"/v1/queue/{queue_id}")).json()
        self.assertEqual((detail["state"], detail["park_requested"], detail["held"]), ("running", True, True))
        self.assertEqual(self.worker.cancelled, [])
        self.assertEqual((await self.api("POST", "/v1/queue/release", caller="cli")).json()["changed"], True)


class ErrorTests(McpCase):
    async def test_an_api_error_keeps_its_code_field_limit_and_value(self) -> None:
        self.serve(api_limits=ApiLimits(max_job_runs=3))
        self.write_catalog_job("long.yaml", run_count=7)
        async with self.client() as client:
            refused = await self.error(client, "submit_job", {"job": "long.yaml"})
            self.assertEqual({key: refused[key] for key in ("code", "field", "limit", "value")}, {"code": "limit_exceeded", "field": "max_job_runs", "limit": 3, "value": 7})
            self.assertEqual(refused, (await self.api("POST", "/v1/queue", json={"job": "long.yaml"})).json())
            self.assertEqual((await self.error(client, "get_queue_entry", {"queue_id": "Q9999"}))["code"], "not_found")

    async def test_with_the_api_down_tools_answer_server_unreachable_and_the_server_keeps_running(self) -> None:
        app, self.transport.app = self.transport.app, None
        async with self.client() as client:
            self.assertEqual([tool.name for tool in (await client.list_tools()).tools], READ_RUN_AND_QUEUE_CONTROL)
            refused = await self.error(client, "get_queue")
            self.assertEqual(refused["code"], "server_unreachable")
            self.assertIn("--server-url", refused["message"])
            self.assertIn("dtc serve", refused["message"])
            self.transport.app = app
            await self.ok(client, "get_queue")

    async def test_a_missing_or_empty_token_file_is_unauthorized_naming_it(self) -> None:
        token_path = self.paths.server_token
        async with self.client() as client:
            for content in (None, " \n"):
                with self.subTest(content=content):
                    if content is None:
                        token_path.unlink()
                    else:
                        token_path.write_text(content, encoding="ascii")
                    refused = await self.error(client, "get_queue")
                    self.assertEqual(refused["code"], "unauthorized")
                    self.assertIn(str(token_path), refused["message"])
                    self.assertIn("dtc serve", refused["message"])
                    self.assertEqual(self.transport.sent(), [])
            token_path.write_text(TOKEN, encoding="ascii")
            await self.ok(client, "get_queue")

    async def test_a_token_rewritten_while_it_runs_is_read_again_after_the_401(self) -> None:
        async with self.client() as client:
            await self.ok(client, "get_queue")
            self.context.token = "b" * 64
            refused = await self.error(client, "get_queue")
            self.assertEqual(refused["code"], "unauthorized")
            self.assertIn("refused the token", refused["message"])
            self.paths.server_token.write_text("b" * 64, encoding="ascii")
            await self.ok(client, "get_queue")

    async def test_a_request_sent_but_not_answered_is_server_timeout_not_unreachable(self) -> None:
        handle = self.transport.handle_async_request

        async def slow_submissions(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/queue":
                self.transport.requests.append((request.method, request.url.raw_path.decode("ascii")))
                raise httpx.ReadTimeout("timed out", request=request)
            return await handle(request)

        self.transport.handle_async_request = slow_submissions
        async with self.client() as client:
            refused = await self.error(client, "submit_job", {"job": "walk.yaml"})
        self.assertEqual(refused["code"], "server_timeout")
        self.assertIn("may have taken effect", refused["message"])

    async def test_the_token_is_in_no_result(self) -> None:
        self.context.token = "b" * 64
        async with self.client() as client:
            for name in ("get_queue", "get_capabilities", "list_jobs"):
                result = await client.call_tool(name, {})
                self.assertNotIn(TOKEN, str(result.model_dump()))


class ArgumentTests(McpCase):
    async def test_an_unknown_missing_or_wrong_argument_is_refused_naming_it_with_no_request(self) -> None:
        cases = [
            ("get_queue", {"verbose": True}, "verbose"),
            ("get_capabilities", {"x": 1}, "x"),
            ("get_job", {}, "job"),
            ("get_job", {"job": 7}, "job"),
            ("get_job", {"job": "walk.yaml", "brief": "yes"}, "brief"),
            ("list_jobs", {"limit": 0}, "limit"),
            ("list_jobs", {"limit": True}, "limit"),
            ("list_jobs", {"limit": 2.5}, "limit"),
            ("get_execution_run", {"execution_id": "E0001", "run": 0}, "run"),
            ("get_execution_run", {"execution_id": "E0001"}, "run"),
            ("submit_job", {"job": "walk.yaml", "path": "/etc/passwd"}, "path"),
        ]
        async with self.client() as client:
            for name, arguments, field in cases:
                with self.subTest(name, arguments=arguments):
                    before = len(self.transport.sent())
                    refused = await self.error(client, name, arguments)
                    self.assertEqual((refused["code"], refused["field"]), ("invalid_input", field))
                    self.assertEqual(self.transport.sent()[before:], [])

    async def test_an_optional_null_is_left_out_and_a_whole_number_with_a_point_is_an_integer(self) -> None:
        async with self.client() as client:
            await self.ok(client, "list_executions", {"limit": 20.0, "cursor": None, "status": None})
            self.assertEqual(self.transport.sent()[-1], ("GET", "/v1/executions?limit=20"))
            refused = await self.error(client, "get_job", {"job": None})
            self.assertEqual((refused["code"], refused["field"]), ("invalid_input", "job"))

    async def test_a_list_tool_asks_for_fifty_unless_told_and_passes_any_limit_on(self) -> None:
        async with self.client() as client:
            await self.ok(client, "list_executions")
            self.assertEqual(self.transport.sent()[-1], ("GET", "/v1/executions?limit=50"))
            body = await self.ok(client, "list_executions", {"limit": 1000})
            self.assertEqual((self.transport.sent()[-1], body["cursor"]), (("GET", "/v1/executions?limit=1000"), None))

    async def test_no_path_argument_reaches_an_endpoint_other_than_its_tools(self) -> None:
        async with self.client() as client:
            for name, argument, others, method, route in PATH_TOOLS:
                for value in (".", "..", "...", "", "a/b", "Q0001/watch", "E0001/outputs", "x/../hold"):
                    with self.subTest(name, value=value):
                        before = len(self.transport.sent())
                        refused = await self.error(client, name, {argument: value, **others})
                        self.assertEqual((refused["code"], refused["field"]), ("invalid_input", argument))
                        self.assertEqual(self.transport.sent()[before:], [])
                for value in ("?x", "#x", "%2F", "%2E%2E", "a b"):
                    with self.subTest(name, value=value):
                        refused = await self.error(client, name, {argument: value, **others})
                        self.assertEqual(self.transport.sent()[-1], (method, route.format(quote(value, safe=""))))
                        # The tool's own route answered, naming what it did not find, not the router's bare 404.
                        self.assertEqual(refused["code"], "not_found")
                        self.assertNotEqual(refused["message"], "Not found")

    async def test_no_name_reaches_an_endpoint_other_than_its_write_tools(self) -> None:
        self.serve(allow_write=True)
        sha = "0" * 64
        cases = [("create_job", {"yaml": "name: x\n"}, "PUT", "/v1/jobs/{}"), ("replace_job", {"yaml": "name: x\n", "expected_sha256": sha}, "PUT", "/v1/jobs/{}?overwrite=1"), ("delete_job", {"expected_sha256": sha}, "DELETE", f"/v1/jobs/{{}}?expected_sha256={sha}")]
        async with self.client() as client:
            for name, others, method, route in cases:
                for value in (".", "..", "", "a/b", "x/../hold"):
                    with self.subTest(name, value=value):
                        before = len(self.transport.sent())
                        refused = await self.error(client, name, {"name": value, **others})
                        self.assertEqual((refused["code"], refused["field"]), ("invalid_input", "name"))
                        self.assertEqual(self.transport.sent()[before:], [])
                for value in ("?x", "#x", "%2F"):
                    with self.subTest(name, value=value):
                        refused = await self.error(client, name, {"name": value, **others})
                        self.assertEqual(self.transport.sent()[-1], (method, route.format(quote(value, safe=""))))
                        # The route itself refused the name, not the router.
                        self.assertEqual((refused["code"], refused.get("field")), ("invalid_input", "name"))


if __name__ == "__main__":
    import unittest

    unittest.main()
