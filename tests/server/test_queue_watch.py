"""``GET /v1/queue/{id}/watch``, ``WatchQueueEntry``'s snapshots over SSE. An in-process client collects
a whole response before it returns it, so the tests that run in process end the stream themselves (the entry gone,
or the server stopping); a keep-alive, a client's disconnect, and a shutdown are tested under uvicorn."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx

from framecontrol.server.context import ServerContext
from framecontrol.server.event_backlog import EventBacklog
from framecontrol.server.generated import monitor_pb2
from framecontrol.server.grpc_service import MonitorServicer
from framecontrol.state.store import Store
from tests.server.live_app import LiveApp, loopback_socket, socket_port
from tests.server.test_queue_routes import TOKEN, FakeWorker, QueueRoutesTestCase
from tests.timing.builders import sample_forecast

SNAPSHOT_FIELDS = ("queue_id", "kind", "state", "execution_id", "current_run", "current_run_elapsed_seconds", "cooldown_until", "error", "total_runs", "park_requested", "queue_held", "forecast")


class ScriptedWorker(FakeWorker):
    """Counts the snapshots read, and calls ``on_read`` with each count as the reservation is read, before the rest:
    a test's way to change what that snapshot holds, or to stop the server."""

    def __init__(self, store: Store, event_backlog: EventBacklog) -> None:
        super().__init__(store, event_backlog)
        self.reads = 0
        self.on_read: Callable[[int], object] | None = None

    def park_requested(self, entry_id: int) -> bool:
        self.reads += 1
        if self.on_read is not None:
            self.on_read(self.reads)
        return super().park_requested(entry_id)


def message_dict(message: Any, names: tuple[str, ...] | None = None) -> dict[str, Any]:
    """A protobuf message as the mapping a client of the SSE watch reads: a field it did not set is None, a nested message
    is a mapping, a repeated field a list."""
    result: dict[str, Any] = {}
    for field in message.DESCRIPTOR.fields:
        if names is not None and field.name not in names:
            continue
        value = getattr(message, field.name)
        if field.is_repeated:
            result[field.name] = list(value)
            continue
        try:
            present = message.HasField(field.name)
        except ValueError:
            # A field with no presence of its own is always there, as its default when it was left out.
            present = True
        result[field.name] = (message_dict(value) if field.message_type is not None else value) if present else None
    return dict(sorted(result.items(), key=lambda item: item[0])) if names is None else {name: result[name] for name in names}


def sse_messages(text: str) -> list[tuple[str, Any]]:
    """Each message of an SSE body, as ``(event, data)`` for an event, its data parsed, or ``("comment", text)``."""
    messages: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        if not lines:
            continue
        if lines[0].startswith(":"):
            messages.append(("comment", lines[0][1:].strip()))
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        messages.append((fields["event"], json.loads(fields["data"])))
    return messages


class WatchTestCase(QueueRoutesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.worker = ScriptedWorker(self.store, self.event_backlog)
        self.client = self.build_client()
        self.context.watch_poll_seconds = 0.01

    def running_entry(self) -> str:
        entry = self.submit(run_count=3)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        self.worker._current_id = claimed.id
        self.worker._current_run = (2, 5.0)
        return entry["queue_id"]

    def script(self, context: ServerContext) -> None:
        """Snapshot 1 is the baseline; 2 changes the elapsed seconds alone; 3 republishes the forecast; 4 ends the run, and
        the server stops after it."""

        def on_read(count: int) -> None:
            if count == 2:
                self.worker._current_run = (2, 9.0)
            elif count == 3:
                self.worker._forecast = sample_forecast(1_000_000.0, done=3)
            elif count == 4:
                self.worker._current_run = None
                context.stopping.set()

        self.worker.reads, self.worker.on_read = 0, on_read


class InProcessWatchTests(WatchTestCase):
    def test_the_current_snapshot_comes_first_then_one_per_change_not_the_elapsed_seconds(self) -> None:
        queue_id = self.running_entry()
        self.script(self.context)
        response = self.request("get", f"/v1/queue/{queue_id.lower()}/watch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual((response.headers["cache-control"], response.headers["x-accel-buffering"]), ("no-cache", "no"))
        messages = sse_messages(response.text)
        self.assertEqual([event for event, _data in messages], ["snapshot", "snapshot", "snapshot"])
        first, step, ended = (data for _event, data in messages)
        self.assertEqual(tuple(first), SNAPSHOT_FIELDS)
        self.assertEqual((first["queue_id"], first["state"], first["current_run"], first["current_run_elapsed_seconds"], first["total_runs"]), (queue_id, "running", 2, 5.0, 3))
        self.assertEqual((first["park_requested"], first["queue_held"], first["forecast"]), (False, False, None))
        self.assertEqual((step["current_run"], step["current_run_elapsed_seconds"], step["forecast"]["stage_done"]), (2, 9.0, 3))
        self.assertEqual((ended["current_run"], ended["forecast"]["stage_done"]), (None, 3))

    def test_a_republished_forecast_is_a_new_snapshot_and_time_passing_is_not(self) -> None:
        queue_id = self.running_entry()
        now = time.time()

        def republish(count: int) -> None:
            if count == 2:
                self.worker._current_run = (2, 9.0)
            elif count == 3:
                self.worker._forecast = sample_forecast(now, done=3)
            elif count == 4:
                self.worker._current_run = (2, 14.0)
            elif count == 5:
                self.worker._forecast = sample_forecast(now + 60.0, done=4)
            elif count == 6:
                self.context.stopping.set()

        self.worker.reads, self.worker.on_read = 0, republish
        messages = sse_messages(self.request("get", f"/v1/queue/{queue_id.lower()}/watch").text)
        snapshots = [data for _event, data in messages]
        # The baseline with no forecast, the first forecast, and the second: the elapsed seconds alone sent nothing.
        self.assertEqual([item["forecast"] and item["forecast"]["stage_done"] for item in snapshots], [None, 3, 4])
        self.assertEqual(snapshots[1]["forecast"]["run"]["floor"], 0.3)

    def test_the_stream_ends_when_the_entry_is_gone(self) -> None:
        queue_id = self.submit(run_count=1)["queue_id"]
        self.request("post", f"/v1/queue/{queue_id}/cancel")
        self.worker.reads, self.worker.on_read = 0, lambda count: self.store.queue.prune(time.time() + 3600) if count == 2 else None
        messages = sse_messages(self.request("get", f"/v1/queue/{queue_id}/watch").text)
        self.assertEqual([(event, data["state"]) for event, data in messages], [("snapshot", "cancelled")])

    def test_an_unknown_entry_is_not_found_before_the_stream_starts(self) -> None:
        for queue_id in ("Q9999", "nope"):
            response = self.request("get", f"/v1/queue/{queue_id}/watch")
            self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))

    def test_the_token_is_required(self) -> None:
        queue_id = self.submit(run_count=1)["queue_id"]
        self.assertEqual(self.client.get(f"/v1/queue/{queue_id}/watch").status_code, 401)
        self.assertEqual(self.client.get(f"/v1/queue/{queue_id}/watch", headers={"Authorization": "Bearer wrong"}).status_code, 401)

    def test_a_watch_is_not_audited(self) -> None:
        queue_id = self.submit(run_count=1)["queue_id"]
        self.worker.on_read = lambda count: self.context.stopping.set()
        before = self.request("get", "/v1/audit").json()["audit"]
        self.request("get", f"/v1/queue/{queue_id}/watch")
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"], before)

    def test_it_sends_the_snapshots_watch_queue_entry_sends_for_the_same_changes(self) -> None:
        queue_id = self.running_entry()
        self.script(self.context)
        over_sse = [data for _event, data in sse_messages(self.request("get", f"/v1/queue/{queue_id}/watch").text)]
        self.worker.reads, self.worker._current_run, self.worker._forecast = 0, (2, 5.0), None
        self.context.stopping.clear()
        self.assertEqual(asyncio.run(self.grpc_snapshots(queue_id, len(over_sse))), over_sse)

    def test_a_forecast_comes_over_grpc_with_every_field_it_has_over_sse(self) -> None:
        queue_id = self.running_entry()
        now = time.time()
        self.worker._forecast = sample_forecast(now, penalty=1.0)
        self.worker.reads, self.worker.on_read = 0, lambda count: self.context.stopping.set() if count == 2 else None
        over_sse = [data for _event, data in sse_messages(self.request("get", f"/v1/queue/{queue_id}/watch").text)]
        self.worker.reads, self.worker._current_run = 0, (2, 5.0)
        self.context.stopping.clear()
        over_grpc = asyncio.run(self.grpc_snapshots(queue_id, 1))
        self.assertEqual(over_grpc, over_sse[:1])
        forecast = over_grpc[0]["forecast"]
        self.assertEqual((forecast["stage"], forecast["stage_done"], forecast["run"]["floor"], forecast["basis"]["penalty"], forecast["basis"]["unseen"]), ("denoise", 12, 0.3, 1.0, ["other"]))

    def test_a_forecast_with_no_run_or_penalty_leaves_them_unset_over_grpc(self) -> None:
        queue_id = self.running_entry()
        self.worker._forecast = sample_forecast(time.time(), run=False)
        self.worker.reads, self.worker.on_read = 0, None
        over_grpc = asyncio.run(self.grpc_snapshots(queue_id, 1))
        forecast = over_grpc[0]["forecast"]
        self.assertEqual((forecast["run"], forecast["basis"]["penalty"]), (None, None))
        self.assertIsNotNone(forecast["job"])

    async def grpc_snapshots(self, queue_id: str, count: int) -> list[dict[str, Any]]:
        servicer = MonitorServicer(self.context, poll_interval=0.01)
        snapshots: list[dict[str, Any]] = []
        stream = servicer.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=queue_id), None)  # pyright: ignore[reportArgumentType]  (a known entry never reaches the context)
        async for message in stream:
            snapshots.append(message_dict(message, SNAPSHOT_FIELDS))
            if len(snapshots) == count:
                break
        return snapshots


class LiveWatchTests(WatchTestCase):
    """Under uvicorn: what an in-process client cannot see."""

    def setUp(self) -> None:
        super().setUp()
        sock = loopback_socket()
        self.live_context = ServerContext(paths=self.paths, global_config=self.global_config, store=self.store, worker=self.worker, executor=self.executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=socket_port(sock), grpc_port=8766, event_backlog=self.event_backlog, watch_poll_seconds=0.01)  # pyright: ignore[reportArgumentType]  (the fake worker only needs the methods the routes call)
        self.live = LiveApp(self.live_context, sock)
        self.live.start()
        self.addCleanup(self.live.stop)
        self.http = httpx.Client(base_url=self.live.base_url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5.0)
        self.addCleanup(self.http.close)

    def read_messages(self, chunks: Iterator[str], count: int) -> list[tuple[str, Any]]:
        text = ""
        for chunk in chunks:
            text += chunk
            if len(sse_messages(text)) >= count and text.endswith("\n\n"):
                break
        return sse_messages(text)

    def test_a_keep_alive_comment_while_nothing_changes(self) -> None:
        self.live_context.watch_keepalive_seconds = 0.2
        queue_id = self.running_entry()
        with self.http.stream("GET", f"/v1/queue/{queue_id}/watch") as response:
            messages = self.read_messages(response.iter_text(), 2)
        self.assertEqual([event for event, _data in messages[:2]], ["snapshot", "comment"])
        self.assertEqual(messages[1][1], "keep-alive")

    def test_a_client_that_disconnects_ends_the_watch(self) -> None:
        queue_id = self.running_entry()
        with self.http.stream("GET", f"/v1/queue/{queue_id}/watch") as response:
            self.read_messages(response.iter_text(), 1)
        time.sleep(0.3)
        reads = self.worker.reads
        time.sleep(0.3)
        self.assertEqual(self.worker.reads, reads, "the watch kept reading its entry after the client left")

    def test_shutdown_ends_an_open_watch(self) -> None:
        queue_id = self.running_entry()
        with self.http.stream("GET", f"/v1/queue/{queue_id}/watch") as response:
            chunks = response.iter_text()
            self.assertEqual(self.read_messages(chunks, 1)[0][0], "snapshot")
            started = time.monotonic()
            self.assertTrue(self.live.stop(timeout=5.0), "uvicorn waited on the open watch")
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertTrue(self.live_context.stopping.is_set())
            self.assertEqual(list(chunks), [], "the watch sent more after the server stopped")


if __name__ == "__main__":
    import unittest

    unittest.main()
