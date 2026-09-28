"""The gRPC ``Monitor`` service (Milestone 02): ``WatchEvents`` and ``WatchQueueEntry``, both unary-request,
server-streaming, since nothing here writes (writes stay HTTP ``POST``). Only ``server/`` imports the generated
server code from ``server/proto/monitor.proto``; ``mcp_server/``, ``tui/``, and ``cli/`` import their own generated
client stubs from the same file."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc
import grpc.aio

from draw_things_control.server.context import ServerContext
from draw_things_control.server.event_backlog import BacklogEvent
from draw_things_control.server.generated import monitor_pb2, monitor_pb2_grpc
from draw_things_control.state.ids import QUEUE_LETTER, execution_id_text, parse_typed_id

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
        replay = backlog.since(request.last_event_id)
        if replay is None:
            yield _reset_event()
            last_id = backlog.latest_id()
        else:
            for event in replay:
                if _wanted(event, request.include_output):
                    yield _to_proto(event)
            last_id = replay[-1].id if replay else backlog.latest_id()
        loop = asyncio.get_running_loop()
        while True:
            await loop.run_in_executor(None, backlog.wait_for_more, last_id, self._poll_interval)
            replay = backlog.since(last_id)
            if replay is None:
                yield _reset_event()
                last_id = backlog.latest_id()
                continue
            for event in replay:
                if _wanted(event, request.include_output):
                    yield _to_proto(event)
                last_id = event.id

    async def WatchQueueEntry(self, request: monitor_pb2.WatchQueueEntryRequest, context: grpc.aio.ServicerContext) -> AsyncIterator[monitor_pb2.QueueEntrySnapshot]:
        number = parse_typed_id(request.queue_id, QUEUE_LETTER)
        if number is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"No queue entry {request.queue_id}")
            return
        last: monitor_pb2.QueueEntrySnapshot | None = None
        while True:
            snapshot = self._snapshot(number)
            if snapshot is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"No queue entry {request.queue_id}")
                return
            if snapshot != last:
                yield snapshot
                last = snapshot
            await asyncio.sleep(self._poll_interval)

    def _snapshot(self, number: int) -> monitor_pb2.QueueEntrySnapshot | None:
        entry = self._context.store.queue.by_number(number)
        if entry is None:
            return None
        snapshot = monitor_pb2.QueueEntrySnapshot(queue_id=entry.label, state=entry.state)
        if entry.execution_number is not None:
            snapshot.execution_id = execution_id_text(entry.execution_number)
        if entry.error is not None:
            snapshot.error = entry.error
        if self._context.worker.current_entry_id() == entry.id:
            current_run = self._context.worker.current_run()
            if current_run is not None:
                snapshot.current_run, snapshot.current_run_elapsed_seconds = current_run
            current_step = self._context.worker.current_step()
            if current_step is not None:
                snapshot.current_step, snapshot.current_step_total = current_step
        cooldown_until = self._context.worker.cooldown_until()
        if cooldown_until is not None:
            snapshot.cooldown_until = cooldown_until
        return snapshot


def _reset_event() -> monitor_pb2.Event:
    return monitor_pb2.Event(id=0, kind=RESET_KIND, data_json="")


def _to_proto(event: BacklogEvent) -> monitor_pb2.Event:
    return monitor_pb2.Event(id=event.id, kind=event.kind, data_json=event.data_json)


def _wanted(event: BacklogEvent, include_output: bool) -> bool:
    return include_output or event.kind != RUN_OUTPUT_KIND
