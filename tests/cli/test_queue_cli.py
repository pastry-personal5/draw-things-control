"""Tests for `fc queue`: a client of the HTTP API and (``add --wait``) the gRPC monitoring service, exercised
against a fake ``httpx`` transport and a fake gRPC stub -- no real ``fc serve`` process, as no CLI test starts one
(AGENTS.md)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from typer.testing import CliRunner

from framecontrol.cli.app import app
from framecontrol.cli.context import CliServices
from framecontrol.cli.generated import monitor_pb2
from framecontrol.core.client_config import job_argument
from framecontrol.services.toolkit import Toolkit
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
    def test_an_id_with_a_path_separator_is_refused_before_a_request(self) -> None:
        self.route(lambda request: httpx.Response(200, json={}))
        result = self.invoke("show", "Q0001/watch")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.requests, [])

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
            assert request.headers["X-Framecontrol-Caller"] == "cli"
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
        """Invokes ``fc queue`` and returns its exit code with everything Loguru logged (never CliRunner's own
        captured output: ``fc``'s ``main()``, which routes Loguru to stdout/stderr, is not what CliRunner calls)."""
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
        self.assertIn("fc serve", messages)
        self.assertNotIn(TOKEN, messages)

    def test_an_unreachable_server_exits_1_naming_dtc_serve_and_never_prints_the_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        self.route(handler)
        exit_code, messages = self.logged("list")
        self.assertEqual(exit_code, 1)
        self.assertIn("fc serve", messages)
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
        for state, code in (("succeeded", 0), ("cancelled", 130), ("interrupted", 143), ("failed", 1), ("parked", 3)):
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

    # Park, unpark, hold, and release.

    def entry_detail(self, **changes: Any) -> dict[str, Any]:
        detail = {"queue_id": "Q0001", "state": "running", "job_path": "/jobs/walk.yaml", "execution_id": "E0001", "total_runs": 7, "succeeded": 2, "park_requested": True, "current_run": 3, "between_runs_after_run": None, "cooldown_until": None, "error": None, "held": True, "held_since": "2026-09-29T12:04:05+00:00", "held_by": "Q0001", "resumable": False, "resume_refused_reason": "Q0001 is running"}
        detail.update(changes)
        return detail

    def test_park_says_after_which_run_the_entry_parks_and_that_the_queue_is_held(self) -> None:
        for changes, words in (
            ({}, "Q0001 parks after run 3/7; the queue is held ('fc queue release' starts it again)"),
            ({"current_run": 7, "succeeded": 6}, "Q0001 is on its last run and will finish; the queue is held ('fc queue release' starts it again)"),
            ({"current_run": None, "between_runs_after_run": 7, "succeeded": 7}, "Q0001 is on its last run and will finish;"),
            ({"current_run": None, "between_runs_after_run": 3, "succeeded": 3}, "Q0001 parks after run 3/7;"),
            ({"state": "parked", "current_run": None, "succeeded": 3}, "Q0001 parked after run 3/7;"),
            ({"current_run": None, "succeeded": 0}, "Q0001 parks after its next run;"),
        ):
            with self.subTest(changes=changes):

                def handler(request: httpx.Request, changes: dict[str, Any] = changes) -> httpx.Response:
                    assert request.method == "POST" and request.url.path == "/v1/queue/Q0001/park"
                    return self.json_response(200, self.entry_detail(**changes))

                self.route(handler)
                result = self.invoke("park", "Q0001")
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn(words, result.stdout)

    def test_a_refused_park_exits_2_naming_the_reason(self) -> None:
        self.route(lambda request: self.json_response(409, {"code": "invalid_state", "message": "Q0001 cannot be parked: it is queued"}))
        exit_code, logged = self.logged("park", "Q0001")
        self.assertEqual(exit_code, 2)
        self.assertIn("it is queued", logged)

    def test_unpark_says_whether_the_queue_stays_held_and_never_claims_a_release(self) -> None:
        for held, words in ((False, "Q0001 runs on; the queue is not held\n"), (True, "Q0001 runs on; the queue stays held\n")):
            with self.subTest(held=held):
                self.route(lambda request, held=held: self.json_response(200, self.entry_detail(park_requested=False, held=held)))
                result = self.invoke("unpark", "Q0001")
                self.assertEqual((result.exit_code, result.stdout), (0, words))
                self.assertEqual(self.requests[-1].url.path, "/v1/queue/Q0001/unpark")

    def test_hold_and_release(self) -> None:
        self.route(lambda request: self.json_response(200, {"held": True, "held_since": "2026-09-29T12:04:05+00:00", "held_by": None, "changed": True}))
        result = self.invoke("hold")
        self.assertEqual((result.exit_code, result.stdout), (0, "Queue held since 2026-09-29 12:04:05\n"))
        self.assertEqual((self.requests[-1].method, self.requests[-1].url.path), ("POST", "/v1/queue/hold"))
        for changed, words in ((True, "Queue released\n"), (False, "The queue is not held\n")):
            self.route(lambda request, changed=changed: self.json_response(200, {"held": False, "held_since": None, "held_by": None, "changed": changed}))
            result = self.invoke("release")
            self.assertEqual((result.exit_code, result.stdout), (0, words))
        self.assertEqual(self.requests[-1].url.path, "/v1/queue/release")

    def test_list_shows_a_parking_entry_and_the_hold(self) -> None:
        body = {"queue": [{"queue_id": "Q0001", "state": "running", "job_path": "/jobs/walk.yaml", "total_runs": 3, "succeeded": 1, "park_requested": True}, {"queue_id": "Q0000", "state": "parked", "job_path": "/jobs/walk.yaml", "total_runs": 3, "succeeded": 2, "park_requested": False}], "worker_state": "running", "cooldown_until": None, "held": True, "held_since": "2026-09-29T12:04:05+00:00", "held_by": "Q0001", "cursor": None}
        self.route(lambda request: self.json_response(200, body))
        result = self.invoke("list")
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "Queue held since 2026-09-29 12:04:05 (by Q0001)")
        self.assertEqual([line.split()[1] for line in lines[2:]], ["parking", "parked"])

    def test_show_prints_the_forecast_of_a_running_entry_and_nothing_for_one_without(self) -> None:
        base = datetime(2026, 9, 29, 14, 0, 0).timestamp()
        run = {"started_at": base, "end_at": base + 1200, "low": base + 1195, "high": base + 1205, "floor": 0.3, "progress": 0.0, "seconds_left": 1200.0}
        job = {"started_at": base, "end_at": base + 2400, "low": base + 2395, "high": base + 2405, "floor": 0.1, "progress": 0.0, "seconds_left": 2400.0}
        forecast = {"as_of": base, "stage": "denoise", "stage_done": 12, "stage_total": 30, "run": run, "job": job, "basis": {"tier": "signature", "fidelity": "live", "runs": 8, "executions": 4, "build_changed": False, "unseen": [], "penalty": None, "skipped": 0}}
        detail = {"queue_id": "Q0001", "state": "running", "job_path": "/jobs/walk.yaml", "execution_id": "E0001", "total_runs": 3, "succeeded": 1, "current_run": 2, "cooldown_until": None, "error": None, "resumable": False, "resume_refused_reason": "Q0001 is running"}
        self.route(lambda request: self.json_response(200, {**detail, "forecast": forecast}))
        result = self.invoke("show", "Q0001")
        self.assertIn("  forecast: denoise 12/30; run ends ~14:20 (in 20 min), job ends ~14:40 (in 40 min)", result.stdout)
        self.assertIn("  based on: 8 runs from 4 executions (signature, live)", result.stdout)
        rough = {**forecast, "basis": {**forecast["basis"], "tier": "nearest", "runs": 2, "executions": 2}}
        self.route(lambda request: self.json_response(200, {**detail, "forecast": rough}))
        self.assertIn("  based on: the 2 nearest runs from 2 executions, scaled to this one (rough, until the steps correct it)", self.invoke("show", "Q0001").stdout)
        factory = {**forecast, "basis": {**forecast["basis"], "tier": "factory", "fidelity": "builtin", "runs": 0, "executions": 0}}
        self.route(lambda request: self.json_response(200, {**detail, "forecast": factory}))
        self.assertIn("  based on: no history of this backend yet, so a built-in run from measured ones", self.invoke("show", "Q0001").stdout)
        self.route(lambda request: self.json_response(200, {**detail, "forecast": None}))
        self.assertNotIn("forecast", self.invoke("show", "Q0001").stdout)

    def test_show_names_the_park_reservation_and_the_hold(self) -> None:
        self.route(lambda request: self.json_response(200, self.entry_detail()))
        result = self.invoke("show", "Q0001")
        self.assertIn("Q0001: parking (walk.yaml)", result.stdout)
        self.assertIn("  park reservation: yes", result.stdout)
        self.assertIn("  Queue held since 2026-09-29 12:04:05 (by Q0001)", result.stdout)

    def wait_handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return self.json_response(200, {"status": "ok", "version": "1.0.0", "worker_alive": True, "grpc_port": 8766})
        return self.json_response(200, {"queue_id": "Q0001", "state": "parked", "job_path": "/jobs/walk.yaml", "total_runs": 7, "succeeded": 5})

    def test_add_wait_on_a_resume_that_parks_prints_the_chains_run_and_exits_3(self) -> None:
        snapshot = monitor_pb2.QueueEntrySnapshot
        self.grpc_stub = FakeMonitorStub(
            [
                snapshot(queue_id="Q0001", state="queued", total_runs=7, park_requested=False, queue_held=True),
                snapshot(queue_id="Q0001", state="queued", total_runs=7, park_requested=False, queue_held=True),
                snapshot(queue_id="Q0001", state="running", total_runs=7, current_run=4, park_requested=False, queue_held=False),
                snapshot(queue_id="Q0001", state="running", total_runs=7, current_run=5, park_requested=False, queue_held=False),
                snapshot(queue_id="Q0001", state="running", total_runs=7, current_run=5, park_requested=True, queue_held=True),
                snapshot(queue_id="Q0001", state="parked", total_runs=7, park_requested=False, queue_held=True),
            ]
        )
        self.route(self.wait_handler)
        result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertEqual(
            result.stdout.splitlines()[1:],
            [
                "Q0001 waits: the queue is held ('fc queue release' starts it)",
                "run 4/7 started",
                "run 4/7 succeeded",
                "run 5/7 started",
                "Q0001 parking: it ends after run 5/7",
                "run 5/7 succeeded",
                "Q0001 parked after run 5/7; 'fc queue resume Q0001' continues at run 6",
            ],
        )

    def forecast_snapshot(self, *, run: int, done: int, now: float, minutes_left: float = 20.0, **changes: Any) -> monitor_pb2.QueueEntrySnapshot:
        """A running snapshot with a forecast whose run ends in ``minutes_left`` minutes and whose job ends in twice that."""

        def span(seconds: float) -> dict[str, float]:
            return {"started_at": now - 600.0, "end_at": now + seconds, "low": now + seconds - 5.0, "high": now + seconds + 5.0, "floor": 0.0}

        forecast = monitor_pb2.Forecast(as_of=now, stage="denoise", stage_done=done, stage_total=30, run=span(minutes_left * 60), job=span(minutes_left * 120), basis=monitor_pb2.ForecastBasis(tier="signature", fidelity="live", runs=8, executions=4))
        return monitor_pb2.QueueEntrySnapshot(queue_id="Q0001", state="running", total_runs=3, current_run=run, park_requested=False, queue_held=False, forecast=forecast, **changes)

    def test_add_wait_prints_the_stage_and_the_ends_with_each_runs_start_and_at_most_once_a_minute_after(self) -> None:
        from unittest import mock

        wall = datetime(2026, 9, 29, 14, 0, 0).timestamp()
        snapshot = monitor_pb2.QueueEntrySnapshot
        clock = [1000.0]
        # Run 1 starts, its forecast is republished 20 s and 70 s after the first line, then run 2 starts.
        script = [(0.0, 1, 3), (20.0, 1, 4), (70.0, 1, 7), (71.0, 2, 1)]
        snapshots: list[monitor_pb2.QueueEntrySnapshot | type[KeyboardInterrupt]] = [self.forecast_snapshot(run=run, done=done, now=wall + moment) for moment, run, done in script]
        snapshots.append(snapshot(queue_id="Q0001", state="succeeded", total_runs=3, current_run=2, park_requested=False, queue_held=False))
        self.grpc_stub = FakeMonitorStub(snapshots)
        self.route(self.wait_handler)
        times = iter([moment for moment, _, _ in script])

        def monotonic() -> float:
            return 1000.0 + clock[0]

        original = FakeQueueEntryCall.__anext__

        async def advancing(call: FakeQueueEntryCall) -> monitor_pb2.QueueEntrySnapshot:
            snapshot_ = await original(call)
            clock[0] = next(times, clock[0])
            return snapshot_

        with mock.patch("framecontrol.cli.queue_wait._monotonic", monotonic), mock.patch("framecontrol.cli.queue_wait._wall_clock", lambda: wall), mock.patch.object(FakeQueueEntryCall, "__anext__", advancing):
            result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.stdout.splitlines()[1:]
        forecasts = [line for line in lines if "ends ~" in line]
        # The first line of run 1, none 20 s later, one after more than a minute (70 s), and the first line of run 2.
        self.assertEqual(len(forecasts), 3, lines)
        self.assertTrue(all(line.startswith("  denoise ") for line in forecasts), forecasts)
        self.assertIn("denoise 3/30; run ends ~14:20 (in 20 min), job ends ~14:40 (in 40 min)", forecasts[0])
        self.assertIn("denoise 7/30", forecasts[1])
        self.assertEqual(lines[0:2], ["run 1/3 started", forecasts[0]])

    def test_add_wait_prints_nothing_extra_when_the_server_has_no_forecast(self) -> None:
        snapshot = monitor_pb2.QueueEntrySnapshot
        self.grpc_stub = FakeMonitorStub([snapshot(queue_id="Q0001", state="running", total_runs=2, current_run=1, park_requested=False, queue_held=False), snapshot(queue_id="Q0001", state="succeeded", total_runs=2, current_run=1, park_requested=False, queue_held=False)])
        self.route(self.wait_handler)
        result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.stdout.splitlines()[1:], ["run 1/2 started", "run 1/2 succeeded", "Q0001 succeeded"])

    def test_add_wait_says_when_a_reservation_is_withdrawn(self) -> None:
        snapshot = monitor_pb2.QueueEntrySnapshot
        self.grpc_stub = FakeMonitorStub(
            [
                snapshot(queue_id="Q0001", state="running", total_runs=2, current_run=1, park_requested=True, queue_held=True),
                snapshot(queue_id="Q0001", state="running", total_runs=2, current_run=1, park_requested=False, queue_held=False),
                snapshot(queue_id="Q0001", state="succeeded", total_runs=2, current_run=2, park_requested=False, queue_held=False),
            ]
        )
        self.route(self.wait_handler)
        result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Q0001 parking: it ends after run 1/2\nQ0001 runs on\n", result.stdout)

    def test_add_wait_words_a_reservation_on_the_last_run_as_finishing(self) -> None:
        snapshot = monitor_pb2.QueueEntrySnapshot
        self.grpc_stub = FakeMonitorStub(
            [
                snapshot(queue_id="Q0001", state="running", total_runs=2, current_run=2, park_requested=True, queue_held=True),
                snapshot(queue_id="Q0001", state="succeeded", total_runs=2, current_run=2, park_requested=False, queue_held=True),
            ]
        )
        self.route(self.wait_handler)
        result = self.invoke("add", "walk.yaml", "--wait")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Q0001 parking: it is on its last run and will finish\nrun 2/2 succeeded\nQ0001 succeeded\n", result.stdout)
