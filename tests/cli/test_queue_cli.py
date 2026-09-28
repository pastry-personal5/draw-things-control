"""Tests for `dtc queue`: a client of the HTTP API and (``add --wait``) the gRPC monitoring service, exercised
against a fake ``httpx`` transport and a fake gRPC stub -- no real ``dtc serve`` process, as no CLI test starts one
(AGENTS.md)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli.app import app
from draw_things_control.cli.context import CliServices
from draw_things_control.cli.generated import monitor_pb2
from draw_things_control.core.client_config import job_argument
from draw_things_control.services.toolkit import Toolkit
from tests.fixtures import JobTestCase

TOKEN = "a" * 64


class FakeQueueEntryCall:
    """Mirrors ``grpc.aio``'s own streaming call object: async-iterable, and cancellable. ``WatchQueueEntry`` never
    ends on its own in the real service, so this blocks forever once its scripted snapshots run out, rather than
    raising ``StopAsyncIteration`` -- a test that forgets to cancel would hang, not pass by accident."""

    def __init__(self, snapshots: list[monitor_pb2.QueueEntrySnapshot | type[KeyboardInterrupt]]) -> None:
        self._snapshots = iter(snapshots)
        self.cancelled = False

    def __aiter__(self) -> "FakeQueueEntryCall":
        return self

    async def __anext__(self) -> monitor_pb2.QueueEntrySnapshot:
        try:
            next_item = next(self._snapshots)
        except StopIteration:
            await asyncio.Event().wait()
            raise AssertionError("unreachable") from None  # pragma: no cover
        if not isinstance(next_item, monitor_pb2.QueueEntrySnapshot):
            raise KeyboardInterrupt
        return next_item

    def cancel(self) -> None:
        self.cancelled = True


class FakeMonitorStub:
    def __init__(self, snapshots: list[monitor_pb2.QueueEntrySnapshot | type[KeyboardInterrupt]]) -> None:
        self._snapshots = snapshots
        self.calls: list[FakeQueueEntryCall] = []

    def WatchQueueEntry(self, _request: monitor_pb2.WatchQueueEntryRequest, metadata: object = None) -> FakeQueueEntryCall:
        call = FakeQueueEntryCall(self._snapshots)
        self.calls.append(call)
        return call


class QueueCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.token_path = self.root / "token"
        self.token_path.write_text(TOKEN, encoding="ascii")
        self.requests: list[httpx.Request] = []
        self.handler: Any = None
        self.grpc_stub: FakeMonitorStub | None = None

    def route(self, handler: Any) -> None:
        self.handler = handler

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def services(self) -> CliServices:
        transport = httpx.MockTransport(self._handle)
        stub_factory = (lambda _target: self.grpc_stub) if self.grpc_stub is not None else None
        return CliServices(self.paths, Toolkit(), http_transport=transport, grpc_stub_factory=stub_factory)

    def invoke(self, *arguments: str):
        return self.runner.invoke(app, ["queue", *arguments, "--token-file", str(self.token_path)], obj=self.services())

    def json_response(self, status: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json=body)

    def test_add_submits_and_prints_the_entry_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST" and request.url.path == "/v1/queue"
            assert json.loads(request.content) == {"job": "walk.yaml"}
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert request.headers["X-Dtc-Caller"] == "cli"
            return self.json_response(200, {"queue_id": "Q0001", "state": "queued", "job_path": "/jobs/walk.yaml", "total_runs": 3, "succeeded": 0})

        self.route(handler)
        result = self.invoke("add", "walk.yaml")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout, "Q0001 queued: walk.yaml\n")

    def test_add_turns_a_path_directly_in_data_jobs_into_its_name(self) -> None:
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        job_path = self.paths.jobs / "walk.yaml"
        job_path.write_text("name: walk\n", encoding="utf-8")
        self.assertEqual(job_argument(str(job_path), self.paths), "walk.yaml")
        self.assertEqual(job_argument("J0001", self.paths), "J0001")
        self.assertEqual(job_argument("walk.yaml", self.paths), "walk.yaml")

    def test_list_prints_a_table(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/queue"
            return self.json_response(200, {"queue": [{"queue_id": "Q0001", "state": "running", "job_path": "/jobs/walk.yaml", "total_runs": 3, "succeeded": 1}], "worker_state": "running", "cooldown_until": None, "cursor": None})

        self.route(handler)
        result = self.invoke("list")
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "STATE", "RUNS", "JOB"])
        self.assertEqual(lines[1].split(), ["Q0001", "running", "1/3", "walk.yaml"])

    def test_list_with_no_entries_says_so(self) -> None:
        self.route(lambda request: self.json_response(200, {"queue": [], "worker_state": "idle", "cooldown_until": None, "cursor": None}))
        result = self.invoke("list")
        self.assertEqual((result.exit_code, result.stdout), (0, "No queue entries.\n"))

    def test_list_passes_the_state_filter(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["state"], "queued")
            return self.json_response(200, {"queue": [], "worker_state": "idle", "cooldown_until": None, "cursor": None})

        self.route(handler)
        self.invoke("list", "--state", "queued")

    def test_show_prints_the_entrys_detail(self) -> None:
        self.route(lambda request: self.json_response(200, {"queue_id": "Q0001", "state": "running", "job_path": "/jobs/walk.yaml", "execution_id": "E0001", "total_runs": 3, "succeeded": 1, "current_run": 2, "cooldown_until": None, "error": None, "resumable": False, "resume_refused_reason": "Q0001 is running"}))
        result = self.invoke("show", "Q0001")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Q0001: running (walk.yaml)", result.stdout)
        self.assertIn("execution: E0001", result.stdout)
        self.assertIn("run 2 in progress", result.stdout)
        self.assertIn("resumable: no (Q0001 is running)", result.stdout)

    def test_cancel_reports_the_new_state(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST" and request.url.path == "/v1/queue/Q0001/cancel"
            return self.json_response(200, {"queue_id": "Q0001", "state": "cancelled", "job_path": "/jobs/walk.yaml", "total_runs": 3, "succeeded": 0})

        self.route(handler)
        result = self.invoke("cancel", "Q0001")
        self.assertEqual((result.exit_code, result.stdout), (0, "Q0001 cancelled\n"))

    def test_resume_reports_the_new_entry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST" and request.url.path == "/v1/queue/Q0001/resume"
            return self.json_response(200, {"queue_id": "Q0002", "state": "queued", "job_path": "/jobs/walk.yaml", "total_runs": 3, "succeeded": 0})

        self.route(handler)
        result = self.invoke("resume", "Q0001")
        self.assertEqual((result.exit_code, result.stdout), (0, "Q0002 queued (resumed from Q0001): walk.yaml\n"))

    def logged(self, *arguments: str) -> tuple[int, str]:
        """Invokes ``dtc queue`` and returns its exit code with everything Loguru logged (never CliRunner's own
        captured output: ``dtc``'s ``main()``, which routes Loguru to stdout/stderr, is not what CliRunner calls)."""
        messages: list[str] = []
        sink = logger.add(lambda message: messages.append(str(message)), format="{message}", level="ERROR")
        try:
            result = self.invoke(*arguments)
        finally:
            logger.remove(sink)
        return result.exit_code, "\n".join(messages)

    def test_a_wrong_token_exits_1_naming_dtc_serve_and_never_prints_it(self) -> None:
        self.route(lambda request: httpx.Response(401))
        exit_code, messages = self.logged("list")
        self.assertEqual(exit_code, 1)
        self.assertIn("dtc serve", messages)
        self.assertNotIn(TOKEN, messages)

    def test_an_unreachable_server_exits_1_naming_dtc_serve_and_never_prints_the_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        self.route(handler)
        exit_code, messages = self.logged("list")
        self.assertEqual(exit_code, 1)
        self.assertIn("dtc serve", messages)
        self.assertNotIn(TOKEN, messages)

    def test_an_api_error_exits_with_its_mapped_code(self) -> None:
        self.route(lambda request: self.json_response(404, {"code": "not_found", "message": "No queue entry Q9999"}))
        result = self.invoke("show", "Q9999")
        self.assertEqual(result.exit_code, 2)

    def test_a_non_loopback_server_url_is_refused_without_allow_remote_server(self) -> None:
        result = self.invoke("list", "--server-url", "http://example.com:8765")
        self.assertEqual(result.exit_code, 2)

    def test_add_wait_prints_each_runs_start_and_the_outcome_and_exits_0(self) -> None:
        self.grpc_stub = FakeMonitorStub(
            [
                monitor_pb2.QueueEntrySnapshot(queue_id="Q0001", state="running", total_runs=2, current_run=1),
                monitor_pb2.QueueEntrySnapshot(queue_id="Q0001", state="running", total_runs=2, current_run=2),
                monitor_pb2.QueueEntrySnapshot(queue_id="Q0001", state="succeeded", total_runs=2, current_run=2),
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/health":
                return self.json_response(200, {"status": "ok", "version": "1.0.0", "worker_alive": True, "grpc_port": 8766})
            return self.json_response(200, {"queue_id": "Q0001", "state": "queued", "job_path": "/jobs/walk.yaml", "total_runs": 2, "succeeded": 0})

        self.route(handler)
        result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("run 1/2 started", result.stdout)
        self.assertIn("run 1/2 succeeded", result.stdout)
        self.assertIn("run 2/2 started", result.stdout)
        self.assertIn("run 2/2 succeeded", result.stdout)
        self.assertIn("Q0001 succeeded", result.stdout)
        [call] = self.grpc_stub.calls
        self.assertTrue(call.cancelled)

    def test_add_wait_maps_each_final_state_to_its_exit_code(self) -> None:
        for state, code in (("succeeded", 0), ("cancelled", 130), ("interrupted", 143), ("failed", 1)):
            with self.subTest(state=state):
                self.grpc_stub = FakeMonitorStub([monitor_pb2.QueueEntrySnapshot(queue_id="Q0001", state=state, total_runs=1, current_run=1)])

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.path == "/v1/health":
                        return self.json_response(200, {"status": "ok", "version": "1.0.0", "worker_alive": True, "grpc_port": 8766})
                    return self.json_response(200, {"queue_id": "Q0001", "state": "queued", "job_path": "/jobs/walk.yaml", "total_runs": 1, "succeeded": 0})

                self.route(handler)
                result = self.invoke("add", "walk.yaml", "--wait")
                self.assertEqual(result.exit_code, code, result.output)

    def test_add_wait_cancels_the_entry_on_keyboard_interrupt(self) -> None:
        self.grpc_stub = FakeMonitorStub([monitor_pb2.QueueEntrySnapshot(queue_id="Q0001", state="running", total_runs=1, current_run=1), KeyboardInterrupt])
        cancel_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/health":
                return self.json_response(200, {"status": "ok", "version": "1.0.0", "worker_alive": True, "grpc_port": 8766})
            if request.url.path == "/v1/queue/Q0001/cancel":
                cancel_requests.append(request)
                return self.json_response(200, {"queue_id": "Q0001", "state": "cancelled", "job_path": "/jobs/walk.yaml", "total_runs": 1, "succeeded": 0})
            return self.json_response(200, {"queue_id": "Q0001", "state": "queued", "job_path": "/jobs/walk.yaml", "total_runs": 1, "succeeded": 0})

        self.route(handler)
        result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertEqual(len(cancel_requests), 1)
