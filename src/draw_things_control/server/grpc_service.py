"""The gRPC ``Monitor`` service (Milestone 02): ``WatchEvents`` and ``WatchQueueEntry``, both unary-request,
server-streaming, since nothing here writes (writes stay HTTP ``POST``). Only ``server/`` imports the generated
server code from ``server/proto/monitor.proto``; ``tui/`` and ``cli/`` import their own generated client stubs from
the same file. ``WatchQueueEntry``'s snapshots come from ``entry_snapshot.py``, which the API's SSE watch reads too."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio

from draw_things_control.server.context import ServerContext
from draw_things_control.server.entry_snapshot import entry_snapshot, snapshot_changed
from draw_things_control.server.event_backlog import BacklogEvent
from draw_things_control.server.generated import monitor_pb2, monitor_pb2_grpc
from draw_things_control.state.ids import QUEUE_LETTER, parse_typed_id

RESET_KIND = "reset"
RUN_OUTPUT_KIND = "run_output"
# How often WatchQueueEntry re-reads the entry and re-checks WatchEvents' backlog for more; low-frequency polling
# a monitoring RPC, not a hot path, and simple to reason about and test.
POLL_INTERVAL_SECONDS = 0.5


class MonitorServicer(monitor_pb2_grpc.MonitorServicer):
    def __init__(self, context: ServerContext, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._context = context
        self._poll_interval = poll_interval

    async def WatchEvents(self, request: monitor_pb2.WatchEventsRequest, context: grpc.aio.ServicerContext) -> AsyncIterator[monitor_pb2.Event]:
        backlog = self._context.event_backlog
        # The latest ID is read before anything is replayed, and every later read asks since() for what follows
        # the last ID handled: an event appended between two separate reads (since(), then latest_id()) would
        # otherwise be skipped. 0 ("only events from now on") starts from the latest ID; one past it (never
        # issued by this run) is clamped to it, so the client still receives what follows.
        latest = backlog.latest_id()
        last_id = min(request.last_event_id, latest) if request.last_event_id != 0 else latest
        loop = asyncio.get_running_loop()
        while True:
            replay = backlog.since(last_id)
            if replay is None:
                # Read before the Reset is sent: the client reloads its state on receiving it, so anything appended
                # after this read must still be streamed, not skipped by a read made once the client has moved on.
                last_id = backlog.latest_id()
                yield _reset_event()
                continue
            for event in replay:
                if _wanted(event, request.include_output):
                    yield _to_proto(event)
                last_id = event.id
            await loop.run_in_executor(None, backlog.wait_for_more, last_id, self._poll_interval)

    async def WatchQueueEntry(self, request: monitor_pb2.WatchQueueEntryRequest, context: grpc.aio.ServicerContext) -> AsyncIterator[monitor_pb2.QueueEntrySnapshot]:
        number = parse_typed_id(request.queue_id, QUEUE_LETTER)
        if number is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"No queue entry {request.queue_id}")
            return
        # The entry's row id never changes, so it is looked up by number once; each poll then reads the row by id.
        found = self._context.store.queue.by_number(number)
        if found is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"No queue entry {request.queue_id}")
            return
        last: dict[str, Any] | None = None
        loop = asyncio.get_running_loop()
        while True:
            snapshot = await loop.run_in_executor(None, entry_snapshot, self._context, found.id)
            if snapshot is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"No queue entry {request.queue_id}")
                return
            # A message only when something the contract names changes, carrying the elapsed seconds as of then.
            if snapshot_changed(last, snapshot):
                yield _snapshot_to_proto(snapshot)
                last = snapshot
            await asyncio.sleep(self._poll_interval)


def _snapshot_to_proto(snapshot: dict[str, Any]) -> monitor_pb2.QueueEntrySnapshot:
    """An unset field (None) is left unset, so a client reads it with ``HasField``."""
    return monitor_pb2.QueueEntrySnapshot(**{name: value for name, value in snapshot.items() if value is not None})


def _reset_event() -> monitor_pb2.Event:
    return monitor_pb2.Event(id=0, kind=RESET_KIND, data_json="")


def _to_proto(event: BacklogEvent) -> monitor_pb2.Event:
    return monitor_pb2.Event(id=event.id, kind=event.kind, data_json=event.data_json)


def _wanted(event: BacklogEvent, include_output: bool) -> bool:
    return include_output or event.kind != RUN_OUTPUT_KIND
