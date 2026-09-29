"""A fake ``dtc serve`` for the TUI's tests: an HTTP ``MockTransport`` and a gRPC stub factory, never a network.

``WatchEvents`` behaves as the real service does where the TUI depends on it: a call opened with ``FORCE_RESET_ID`` (or
after ``explain_gap`` is turned off) begins with ``Reset``; otherwise the events after ``last_event_id`` are replayed
first; and ``run_output`` is left out for a call opened with ``include_output=false``, whose own cursor then only
moves on the events it is sent. Every request the feed opens with is recorded in ``requests``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from draw_things_control.core.cooldown import CooldownPolicy
from draw_things_control.jobs.events import CooldownStarted, JobEvent, JobFinished, JobStarted, JobStatus, RunFinished, RunOutput, RunStarted, RunStatus, event_to_dict
from draw_things_control.server.serializers import delete_report
from draw_things_control.services.history_delete import delete_executions
from draw_things_control.state.ids import EXECUTION_LETTER, parse_typed_id
from draw_things_control.state.store import Store, StoreMode
from draw_things_control.tui.feed import FORCE_RESET_ID
from draw_things_control.tui.generated import monitor_pb2

GRPC_PORT = 18766
FIRST_EVENT_ID = 1000
TOKEN = "test-token"
AT = "2026-09-29T10:00:00+00:00"


def job_started(*, total_runs: int = 2, job_name: str = "walk", first_run: int = 1, execution_id: str = "E0001") -> JobStarted:
    return JobStarted(at=AT, job_name=job_name, job_file=f"/jobs/{job_name}.yaml", source_text="", mode="i2v", total_runs=total_runs, output_directory="/out", input=None, model="base.ckpt", seed=1, seed_source="job", cooldown=CooldownPolicy(mode="off"), cooldown_source="job", manifest=None, log=None, execution_id=execution_id, first_run=first_run)


def run_started(number: int = 1, total: int = 2) -> RunStarted:
    return RunStarted(at=AT, number=number, total=total, pair="walk", positive="walk", negative=None, input=None, resized_input=None, output=f"walk-{number}.mp4", last_frame=None, command=("draw-things-cli", "generate"))


def run_output(text: str, *, number: int = 1, stream: str = "stdout", progress: tuple[int, int] | None = None, percent: int | None = None) -> RunOutput:
    return RunOutput(at=AT, number=number, stream=stream, text=text, progress=progress, percent=percent)


def run_finished(number: int = 1, seconds: float = 30.0) -> RunFinished:
    return RunFinished(at=AT, number=number, status=RunStatus.SUCCEEDED, exit_code=0, seconds=seconds, output=f"walk-{number}.mp4", last_frame=None)


def cooldown_started(after_run: int = 1, seconds: float = 120.0) -> CooldownStarted:
    return CooldownStarted(at=AT, after_run=after_run, seconds=seconds, until="10:05:00")


def job_finished(total: int = 2) -> JobFinished:
    return JobFinished(at=AT, status=JobStatus.SUCCEEDED, exit_code=0, completed_runs=total, total_runs=total, signal=None)


@dataclass
class _Call:
    """One open ``WatchEvents`` call: what it asked for, and the events waiting to be read by the client."""

    request: monitor_pb2.WatchEventsRequest
    queue: asyncio.Queue[monitor_pb2.Event | None] = field(default_factory=asyncio.Queue)
    cancelled: bool = False

    def __aiter__(self) -> AsyncIterator[monitor_pb2.Event]:
        return self._events()

    async def _events(self) -> AsyncIterator[monitor_pb2.Event]:
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    def cancel(self) -> None:
        self.cancelled = True
        self.queue.put_nowait(None)


class FakeStub:
    def __init__(self, server: FakeServer) -> None:
        self.server = server

    def WatchEvents(self, request: monitor_pb2.WatchEventsRequest, metadata: tuple[tuple[str, str], ...] | None = None) -> _Call:
        return self.server.open_call(request)


class FakeServer:
    """The HTTP API and the gRPC service, one object, for one test."""

    def __init__(self) -> None:
        self.requests: list[monitor_pb2.WatchEventsRequest] = []
        self.calls: list[_Call] = []
        self.backlog: list[monitor_pb2.Event] = []
        self.next_id = FIRST_EVENT_ID
        # False: a reopened call gets Reset even though the backlog holds its ID (the gap is too big to explain).
        self.explain_gap = True
        self.http_paths: list[str] = []
        self.running: list[dict[str, Any]] = []
        self.details: dict[str, dict[str, Any]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.entries: list[dict[str, Any]] = []
        self.health_ok = True
        self.cancelled_ids: list[str] = []
        # What POST /v1/queue was sent, as the job argument.
        self.submitted: list[str] = []
        # Milestone 05: the entries parked and unparked, the hold GET /v1/queue and the hold endpoints report, and a
        # response to replace an entry's park response with (a refusal, or an entry that already parked).
        self.parked_ids: list[str] = []
        self.unparked_ids: list[str] = []
        self.hold: dict[str, Any] = {"held": False, "held_since": None, "held_by": None}
        self.park_responses: dict[str, httpx.Response] = {}
        # Milestone 06: the state database POST /v1/executions/delete deletes from, through the real service; each
        # request's IDs and whether it was a dry run; how many real deletions succeed before the server drops (None:
        # never); and whether the server cannot be reached at all.
        self.database: Path | None = None
        self.deletions: list[tuple[list[str], bool]] = []
        self.deletes_before_drop: int | None = None
        self.unreachable = False

    # HTTP

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.unreachable:
            raise httpx.ConnectError("Connection refused")
        if request.method == "POST" and path == "/v1/executions/delete":
            return self._delete(json.loads(request.content))
        self.http_paths.append(f"{request.method} {path}" + (f"?{request.url.query.decode()}" if request.url.query else ""))
        if path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "grpc_port": GRPC_PORT}) if self.health_ok else httpx.Response(503, json={})
        if path == "/v1/queue" and request.method == "GET":
            return httpx.Response(200, json={"queue": self.running if request.url.params.get("state") == "running" else self.entries, **self.hold})
        if request.method == "POST" and path in ("/v1/queue/hold", "/v1/queue/release"):
            return self._hold_or_release(path.endswith("/hold"))
        if request.method == "POST" and (path.endswith("/park") or path.endswith("/unpark")):
            return self._park_or_unpark(path.split("/")[-2], path.endswith("/park"))
        if path == "/v1/queue" and request.method == "POST":
            job = json.loads(request.content)["job"]
            self.submitted.append(job)
            return httpx.Response(200, json={"queue_id": f"Q{len(self.submitted):04d}", "job_path": f"/jobs/{Path(job).name}"})
        if request.method == "POST" and path.endswith("/cancel"):
            queue_id = path.split("/")[-2]
            self.cancelled_ids.append(queue_id)
            return httpx.Response(200, json={"queue_id": queue_id, "state": "cancelled"})
        if path.startswith("/v1/queue/") and path.split("/")[-1] in self.details:
            return httpx.Response(200, json=self.details[path.split("/")[-1]])
        if path.startswith("/v1/executions/") and path.split("/")[-1] in self.executions:
            return httpx.Response(200, json=self.executions[path.split("/")[-1]])
        return httpx.Response(404, json={"message": f"no {path}"})

    def _delete(self, body: dict[str, Any]) -> httpx.Response:
        dry_run = body.get("dry_run", False)
        if not dry_run and self.deletes_before_drop is not None:
            if self.deletes_before_drop == 0:
                raise httpx.ReadError("Connection reset")
            self.deletes_before_drop -= 1
        self.deletions.append((body["executions"], dry_run))
        assert self.database is not None
        store = Store.open(self.database, mode=StoreMode.BROWSE)
        try:
            numbers = [parse_typed_id(text, EXECUTION_LETTER) for text in body["executions"]]
            report = delete_executions(store, [number for number in numbers if number is not None], dry_run=dry_run)
        finally:
            store.close()
        return httpx.Response(200, json=delete_report(report, dry_run=dry_run))

    def _hold_or_release(self, hold: bool) -> httpx.Response:
        changed = self.hold["held"] != hold
        self.hold = {"held": True, "held_since": self.hold["held_since"] or AT, "held_by": None} if hold else {"held": False, "held_since": None, "held_by": None}
        return httpx.Response(200, json={**self.hold, "changed": changed})

    def _park_or_unpark(self, queue_id: str, park: bool) -> httpx.Response:
        (self.parked_ids if park else self.unparked_ids).append(queue_id)
        if park and queue_id in self.park_responses:
            return self.park_responses[queue_id]
        if park and not self.hold["held"]:
            self.hold = {"held": True, "held_since": AT, "held_by": queue_id}
        if not park and self.hold["held_by"] == queue_id:
            self.hold = {"held": False, "held_since": None, "held_by": None}
        detail = self.details.get(queue_id, {"queue_id": queue_id, "state": "running", "total_runs": 2, "succeeded": 0, "current_run": 1})
        return httpx.Response(200, json={"job_path": "/jobs/walk.yaml", **detail, "park_requested": park, **self.hold})

    # gRPC

    def stub_factory(self, target: str) -> FakeStub:
        return FakeStub(self)

    def open_call(self, request: monitor_pb2.WatchEventsRequest) -> _Call:
        self.requests.append(request)
        call = _Call(request)
        self.calls.append(call)
        if request.last_event_id == FORCE_RESET_ID or (not self.explain_gap and self.backlog):
            call.queue.put_nowait(monitor_pb2.Event(id=0, kind="reset", data_json=""))
            return call
        for event in self.backlog:
            if event.id > request.last_event_id and self._wanted(request, event):
                call.queue.put_nowait(event)
        return call

    @staticmethod
    def _wanted(request: monitor_pb2.WatchEventsRequest, event: monitor_pb2.Event) -> bool:
        return request.include_output or event.kind != "run_output"

    @property
    def open_calls(self) -> list[_Call]:
        return [call for call in self.calls if not call.cancelled]

    def send(self, event: JobEvent) -> monitor_pb2.Event:
        """Deliver a job event to every open call that wants it, and keep it in the backlog."""
        return self.send_raw(event_to_dict(event)["kind"], event_to_dict(event))

    def send_raw(self, kind: str, data: dict[str, Any]) -> monitor_pb2.Event:
        event = monitor_pb2.Event(id=self.next_id, kind=kind, data_json=json.dumps(data))
        self.next_id += 1
        self.backlog.append(event)
        for call in self.open_calls:
            if self._wanted(call.request, event):
                call.queue.put_nowait(event)
        return event

    def send_queue_entry(self, queue_id: str, state: str) -> monitor_pb2.Event:
        return self.send_raw("queue_entry_changed", {"queue_id": queue_id, "state": state})

    def send_park_changed(self, queue_id: str, park_requested: bool) -> monitor_pb2.Event:
        return self.send_raw("queue_park_changed", {"queue_id": queue_id, "park_requested": park_requested})

    def send_held(self, since: str | None = AT, by: str | None = None) -> monitor_pb2.Event:
        self.hold = {"held": True, "held_since": since, "held_by": by}
        return self.send_raw("queue_held", {"since": since, "by": by})

    def send_released(self) -> monitor_pb2.Event:
        self.hold = {"held": False, "held_since": None, "held_by": None}
        return self.send_raw("queue_released", {})

    def send_reset(self) -> None:
        for call in self.open_calls:
            call.queue.put_nowait(monitor_pb2.Event(id=0, kind="reset", data_json=""))
