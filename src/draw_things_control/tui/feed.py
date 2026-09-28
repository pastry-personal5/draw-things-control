"""The TUI's one gRPC connection to ``dtc serve`` (Milestone 03): ``WatchEvents(include_output=true)`` feeds both
the Queue widget and the draw-things-cli pane, since the TUI no longer runs a job itself to read events from
directly. Reconnects on any drop, replaying the gap when the server never went down in between, and reseeding
(``GET /queue?state=running``, ``GET /queue/{id}``, ``GET /executions/{id}``) exactly as attaching does otherwise.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from loguru import logger

from draw_things_control.core.client_config import read_client_token
from draw_things_control.core.cooldown import CooldownPolicy, parse_cooldown
from draw_things_control.core.errors import DtcError
from draw_things_control.core.network import grpc_target
from draw_things_control.jobs.events import CooldownStarted, JobEvent, JobStarted, RunFinished, RunOutput, RunStarted, RunStatus, event_from_dict
from draw_things_control.state.queue import FINISHED_STATES
from draw_things_control.tui.client import CALLER, CALLER_HEADER, GrpcStubFactory, MonitorStub
from draw_things_control.tui.generated import monitor_pb2, monitor_pb2_grpc
from draw_things_control.tui.live_run import LiveRun

if TYPE_CHECKING:
    import httpx

    from draw_things_control.tui.app import DrawThingsApp

# Any real event ID is a timestamp in milliseconds since 1970, always far larger than this: opening with it always
# reads as "the server's backlog cannot explain this", forcing the first message of a connection to be Reset, so
# the pane always seeds before applying anything live (attaching and a mid-stream Reset then run the same code).
FORCE_RESET_ID = 1
RECONNECT_SECONDS = 2.0
_FINISHED_STATE_VALUES = frozenset(str(state) for state in FINISHED_STATES)


class QueueFeed:
    """Owns the app's one ``WatchEvents`` connection; ``run`` is the coroutine a Textual ``@work`` worker drives."""

    def __init__(self, app: DrawThingsApp) -> None:
        self._app = app
        self._last_event_id = FORCE_RESET_ID
        # The next open must force Reset: true at startup, and after any health-check failure during a gap (a
        # resumed real ID could, in a vanishingly rare race, be clamped by a freshly restarted server with no Reset).
        self._reopen_fresh = True

    async def run(self) -> None:
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The queue feed disconnected")
            self._app.feed_connected = False
            self._app.on_feed_connection_changed()
            await asyncio.sleep(RECONNECT_SECONDS)

    async def _connect_once(self) -> None:
        import httpx

        try:
            token = read_client_token(self._app.token_file)
        except DtcError:
            self._reopen_fresh = True
            return
        headers = {"Authorization": f"Bearer {token}", CALLER_HEADER: CALLER}
        async with httpx.AsyncClient(base_url=self._app.server_url, transport=self._app.http_transport, headers=headers, timeout=30.0) as http:
            grpc_port = await self._grpc_port(http)
            if grpc_port is None:
                return
            target = grpc_target(urlsplit(self._app.server_url).hostname or "127.0.0.1", grpc_port)
            metadata = (("authorization", f"Bearer {token}"),)
            async with _stub(target, self._app.grpc_stub_factory) as stub:
                await self._watch(stub, metadata, http)

    async def _grpc_port(self, http: httpx.AsyncClient) -> int | None:
        import httpx

        try:
            health = await http.get("/v1/health")
            health.raise_for_status()
            return int(health.json()["grpc_port"])
        except (httpx.HTTPError, KeyError, ValueError):
            self._reopen_fresh = True
            return None

    async def _watch(self, stub: MonitorStub, metadata: tuple[tuple[str, str], ...], http: httpx.AsyncClient) -> None:
        open_id = FORCE_RESET_ID if self._reopen_fresh else self._last_event_id
        call = stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=open_id, include_output=True), metadata=metadata)
        try:
            async for event in call:
                await self._handle(event, http)
        finally:
            call.cancel()

    async def _handle(self, event: monitor_pb2.Event, http: httpx.AsyncClient) -> None:
        self._reopen_fresh = False
        if not self._app.feed_connected:
            self._app.feed_connected = True
            self._app.on_feed_connection_changed()
        if event.kind == "reset":
            await self._reseed(http)
            return
        self._last_event_id = event.id
        data = json.loads(event.data_json)
        if event.kind.startswith("queue_"):
            self._app.refresh_queue()
            if event.kind == "queue_entry_changed":
                self._apply_queue_entry_changed(data)
            return
        await self._apply_job_event(event_from_dict(data))

    def _apply_queue_entry_changed(self, data: dict[str, Any]) -> None:
        queue_id, state = data["queue_id"], data["state"]
        if state == "running":
            self._app.pending_queue_id = queue_id
            return
        if state not in _FINISHED_STATE_VALUES:
            return
        live = self._app.live
        if live is not None and live.queue_id == queue_id and not live.ended:
            self._end_followed(live)

    async def _apply_job_event(self, job_event: JobEvent) -> None:
        app = self._app
        if isinstance(job_event, JobStarted):
            live = LiveRun()
            live.apply(job_event)
            live.queue_id = app.pending_queue_id
            app.pending_queue_id = None
            app.live = live
            if app.main is not None:
                app.main.job_started()
            live.past_run = await asyncio.to_thread(app.read_past_run)
            return
        if app.live is None:
            return
        was_ended = app.live.ended
        app.live.apply(job_event)
        if app.main is not None:
            app.main.job_event(job_event)
        if not was_ended and app.live.ended and app.main is not None:
            app.main.job_ended()

    def _end_followed(self, live: LiveRun, *, error: str | None = None) -> None:
        live.end(error=error)
        self._app.refresh_queue()
        if self._app.main is not None:
            self._app.main.job_ended()

    async def _reseed(self, http: httpx.AsyncClient) -> None:
        self._app.refresh_queue()
        response = await http.get("/v1/queue", params={"state": "running"})
        response.raise_for_status()
        entries = response.json()["queue"]
        if not entries:
            live = self._app.live
            if live is not None and not live.ended:
                self._end_followed(live)
            return
        await self._seed_running_entry(http, entries[0])

    async def _seed_running_entry(self, http: httpx.AsyncClient, entry: dict[str, Any]) -> None:
        detail_response = await http.get(f"/v1/queue/{entry['queue_id']}")
        detail_response.raise_for_status()
        detail = detail_response.json()
        execution = None
        if detail.get("execution_id"):
            execution_response = await http.get(f"/v1/executions/{detail['execution_id']}")
            execution_response.raise_for_status()
            execution = execution_response.json()
        live = LiveRun()
        live.apply(_synthetic_job_started(entry, detail, execution))
        live.queue_id = entry["queue_id"]
        if execution is not None:
            _seed_runs(live, execution["runs"])
        _seed_active_run(live, detail)
        live.past_run = await asyncio.to_thread(self._app.read_past_run)
        self._app.live = live
        if self._app.main is not None:
            self._app.main.job_started(seeded=True)


@asynccontextmanager
async def _stub(target: str, grpc_stub_factory: GrpcStubFactory | None) -> AsyncIterator[MonitorStub]:
    if grpc_stub_factory is not None:
        yield cast(MonitorStub, grpc_stub_factory(target))
        return
    import grpc.aio

    async with grpc.aio.insecure_channel(target) as channel:
        yield monitor_pb2_grpc.MonitorStub(channel)


def _synthetic_job_started(entry: dict[str, Any], detail: dict[str, Any], execution: dict[str, Any] | None) -> JobStarted:
    """A ``JobStarted`` built from a seeding read, standing in for the live event this session never saw: only the
    fields anything under ``tui/`` actually reads (``jobs/events.py``'s ``source_text``, ``output_directory``,
    ``input``, ``config_file``, ``config_override``, and ``input_resize`` are not among them)."""
    cooldown = _cooldown_of(execution) if execution is not None else CooldownPolicy(mode="off")
    total_runs = (execution["total_runs"] if execution is not None else entry["total_runs"]) or 0
    return JobStarted(
        at=execution["started_at"] if execution is not None else entry["submitted_at"],
        job_name=execution["job_name"] if execution is not None else Path(entry["job_path"]).stem,
        job_file=execution["job_file"] if execution is not None else entry["job_path"],
        source_text="",
        mode=execution["mode"] if execution is not None else "",
        total_runs=total_runs,
        output_directory="",
        input=None,
        model=(execution["model"] if execution is not None else None) or "",
        seed=execution["seed"] if execution is not None else 0,
        seed_source=execution["seed_source"] if execution is not None else "",
        cooldown=cooldown,
        cooldown_source=execution["cooldown_source"] if execution is not None else "",
        manifest=execution["manifest"] if execution is not None else None,
        log=execution["log"] if execution is not None else None,
        execution_id=detail.get("execution_id"),
        first_run=execution["first_run"] if execution is not None else 1,
    )


def _cooldown_of(execution: dict[str, Any]) -> CooldownPolicy:
    if execution.get("cooldown") is not None:
        return parse_cooldown(execution["cooldown"], "cooldown")
    seconds = execution.get("cooldown_seconds")
    return CooldownPolicy(mode="manual", seconds=seconds) if seconds else CooldownPolicy(mode="off")


def _seed_runs(live: LiveRun, runs: list[dict[str, Any]]) -> None:
    for run in runs:
        started = RunStarted(at="", number=run["number"], total=len(live.runs), pair=run["pair"], positive="", negative=None, input=None, resized_input=None, output=run["output"] or "", last_frame=run["last_frame"], command=tuple(run["command"]))
        live.apply(started)
        if run["status"] != "running":
            finished = RunFinished(at="", number=run["number"], status=RunStatus(run["status"]), exit_code=run["exit_code"], seconds=run["seconds"], output=run["output"], last_frame=run["last_frame"], output_width=run["output_width"], output_height=run["output_height"], output_frames=run["output_frames"])
            live.apply(finished)


def _seed_active_run(live: LiveRun, detail: dict[str, Any]) -> None:
    """Layers the still-running run's live position on: its elapsed time (the seeded RunStarted above applied
    "now" as its start, corrected here to the real start), and its step counter, when draw-things-cli has shown
    one -- everything ``GET /queue/{id}``'s own current_run, current_run_elapsed_seconds, current_step, and
    current_step_total cover about the active run that a finished run's stored row cannot."""
    if detail.get("current_run") is None:
        _seed_cooldown(live, detail)
        return
    elapsed = detail.get("current_run_elapsed_seconds") or 0.0
    if live.active_run != detail["current_run"]:
        live.apply(RunStarted(at="", number=detail["current_run"], total=len(live.runs), pair="?", positive="", negative=None, input=None, resized_input=None, output="", last_frame=None, command=()))
    if live.run_started_at is not None:
        live.run_started_at = live.now() - elapsed
    if detail.get("current_step") is not None and detail.get("current_step_total") is not None:
        live.apply(RunOutput(at="", number=detail["current_run"], stream="stdout", text="", progress=(detail["current_step"], detail["current_step_total"])))


def _seed_cooldown(live: LiveRun, detail: dict[str, Any]) -> None:
    cooldown_until = detail.get("cooldown_until")
    if cooldown_until is None or live.started is None:
        return
    seconds = max(0.0, cooldown_until - time.time())
    after_run = max((run.number for run in live.runs if run.status == RunStatus.SUCCEEDED), default=0)
    until = datetime.fromtimestamp(cooldown_until).strftime("%H:%M:%S")
    live.apply(CooldownStarted(at="", after_run=after_run, seconds=seconds, until=until, mode=live.started.cooldown.mode))
    live.cooldown_ends_at = live.now() + seconds
