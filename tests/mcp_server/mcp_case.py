"""The MCP server's tests' shared case (Milestone 10): ``dtc serve``'s API in process, over a fake worker, reached
through ``httpx.ASGITransport`` with a base URL its ``Host`` check accepts, and the MCP server built over it, which the
SDK's in-memory ``Client`` drives. Never starts ``draw-things-cli``."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from typing import Any

import httpx
import yaml
from fastapi import FastAPI
from mcp import Client
from mcp_types import TextContent

from draw_things_control.core.global_config import ApiLimits
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.mcp_server.app import build_server
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.server.event_backlog import EventBacklog
from draw_things_control.state.execution_rows import MediaCheckRow
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor
from tests.server.test_queue_routes import TOKEN, FakeWorker

SERVER_URL = "http://127.0.0.1:8765"


class ApiTransport(httpx.AsyncBaseTransport):
    """``dtc serve`` in process, or, with ``app`` None, a server that is down. Records each request's method and path,
    query included, as sent."""

    def __init__(self) -> None:
        self.app: FastAPI | None = None
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.raw_path.decode("ascii")))
        if self.app is None:
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        return await httpx.ASGITransport(app=self.app).handle_async_request(request)

    def sent(self) -> list[tuple[str, str]]:
        """The requests but reads of the capabilities, which the MCP server makes on its own, and the SDK's client
        prompts by listing the tools after a call."""
        return [request for request in self.requests if request[1] != "/v1/capabilities"]


class McpCase(JobTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        self.executor: JobExecutor = job_executor(runner_factory=lambda *a: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False)
        self.event_backlog = EventBacklog()
        self.worker = FakeWorker(self.store, self.event_backlog)
        self.paths.server_token.parent.mkdir(parents=True, exist_ok=True)
        self.paths.server_token.write_text(f"{TOKEN}\n", encoding="ascii")
        self.transport = ApiTransport()
        self.serve()
        # The MCP server's clock: a test moves it on to make its last read of the capabilities stale.
        self.now = 0.0
        self.server = build_server(SERVER_URL, self.paths.server_token, http_transport=self.transport, clock=lambda: self.now)

    def serve(self, *, allow_write: bool = False, api_limits: ApiLimits | None = None) -> None:
        """Start, or restart, ``dtc serve`` in process, with or without ``--allow-write``."""
        global_config = replace(self.global_config, api_limits=api_limits) if api_limits is not None else self.global_config
        self.context = ServerContext(paths=self.paths, global_config=global_config, store=self.store, worker=self.worker, executor=self.executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8765, grpc_port=8766, allow_write=allow_write, event_backlog=self.event_backlog)  # pyright: ignore[reportArgumentType]  (the fake worker only needs the methods the routes call)
        self.transport.app = create_app(self.context)

    def client(self, **options: Any) -> Client:
        return Client(self.server, **options)

    async def call(self, client: Client, name: str, arguments: dict[str, Any] | None = None) -> tuple[Any, bool]:
        """A tool's structured content and whether it is an error, checking its one text block is the same, compact."""
        result = await client.call_tool(name, arguments or {})
        [block] = result.content
        assert isinstance(block, TextContent)
        self.assertEqual(block.text, json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False))
        return result.structured_content, result.is_error

    async def ok(self, client: Client, name: str, arguments: dict[str, Any] | None = None) -> Any:
        body, is_error = await self.call(client, name, arguments)
        self.assertFalse(is_error, (name, body))
        return body

    async def error(self, client: Client, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        body, is_error = await self.call(client, name, arguments)
        self.assertTrue(is_error, (name, body))
        return body

    async def api(self, method: str, path: str, *, caller: str | None = None, **options: Any) -> httpx.Response:
        """The API itself, as a person's front end calls it."""
        headers = {"Authorization": f"Bearer {TOKEN}", **({"X-Dtc-Caller": caller} if caller is not None else {})}
        assert self.transport.app is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.transport.app), base_url=SERVER_URL) as client:
            return await client.request(method, path, headers=headers, **options)

    def write_job(self, data: dict[str, Any], name: str = "job.yaml") -> Any:
        path = self.paths.jobs / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    def write_catalog_job(self, name: str = "walk.yaml", **changes: Any) -> str:
        changes.setdefault("run_timeout_seconds", 60)
        changes.setdefault("prompt_pairs", [{"name": "only", "positive": "text"}])
        return self.write_job(job_data(**changes), name).read_text(encoding="utf-8")

    def make_running(self, queue_id: str) -> None:
        """Claim the oldest queued entry, which must be ``queue_id``, as the worker would."""
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None and claimed.label == queue_id
        self.worker._current_id = claimed.id

    def interrupt_with_runs(self, queue_id: str, *, runs: int = 3, total: int = 7) -> str:
        """Give running entry ``queue_id`` an execution with ``runs`` succeeded runs, each with a check, and leave it
        interrupted, so it can be resumed; returns the execution's ID."""
        entry = self.store.queue.by_number(int(queue_id[1:]))
        assert entry is not None
        self.output_directory.mkdir(parents=True, exist_ok=True)
        execution_row = self.store.executions.start(NewExecution(job_name="sunset-walk", job_file="walk.yaml", mode="i2v", started_at="2026-10-02T10:00:00+00:00", seed=42, total_runs=total, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        for number in range(1, runs + 1):
            (self.output_directory / f"run-{number}.mov").write_bytes(b"mov")
            (self.output_directory / f"last-frame-{number}.png").write_bytes(b"png")
            self.store.executions.start_run(execution_row, number, NewRun(pair="only", positive="text", started_at="2026-10-02T10:00:00+00:00", command=["draw-things-cli", "--prompt", "text"]))
            self.store.executions.finish_run(execution_row, number, status="succeeded", exit_code=0, seconds=4.0, output=f"run-{number}.mov", last_frame=f"last-frame-{number}.png")
            self.store.executions.add_check(execution_row, MediaCheckRow(run=number, stage="video", file=f"run-{number}.mov", summary="ok", verdict="pass", notes=(), facts={"drift": 0.1}, at="2026-10-02T10:01:00+00:00"))
        self.store.executions.finish(execution_row, status="interrupted", exit_code=None, signal=None, finished_at="2026-10-02T10:10:00+00:00")
        number = self.store.executions.number_of(execution_row)
        assert number is not None
        self.store.queue.link_execution(entry.id, number)
        self.store.queue.finish(entry.id, state=QueueState.INTERRUPTED, finished_at="2026-10-02T10:10:00+00:00")
        if self.worker._current_id == entry.id:
            self.worker._current_id = None
        return f"E{number:04d}"
