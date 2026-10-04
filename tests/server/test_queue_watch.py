"""``GET /v1/queue/{id}/watch``, ``WatchQueueEntry``'s snapshots over SSE (Milestone 10). An in-process client collects
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

from draw_things_control.server.context import ServerContext
from draw_things_control.server.event_backlog import EventBacklog
from draw_things_control.server.generated import monitor_pb2
from draw_things_control.server.grpc_service import MonitorServicer
from draw_things_control.state.store import Store
from tests.server.live_app import LiveApp, loopback_socket, socket_port
from tests.server.test_queue_routes import TOKEN, FakeWorker, QueueRoutesTestCase

SNAPSHOT_FIELDS = ("queue_id", "kind", "state", "execution_id", "current_run", "current_run_elapsed_seconds", "cooldown_until", "error", "current_step", "current_step_total", "total_runs", "park_requested", "queue_held")


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
        """Snapshot 1 is the baseline; 2 changes the elapsed seconds alone; 3 a step; 4 ends the run, and the server
        stops after it."""

        def on_read(count: int) -> None:
            if count == 2:
                self.worker._current_run = (2, 9.0)
            elif count == 3:
                self.worker._current_step = (3, 30)
            elif count == 4:
                self.worker._current_run = self.worker._current_step = None
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
        self.assertEqual((first["park_requested"], first["queue_held"], first["current_step"]), (False, False, None))
        self.assertEqual((step["current_run"], step["current_run_elapsed_seconds"], step["current_step"], step["current_step_total"]), (2, 9.0, 3, 30))
        self.assertEqual((ended["current_run"], ended["current_step"]), (None, None))

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
        self.worker.reads, self.worker._current_run = 0, (2, 5.0)
        self.context.stopping.clear()
        self.assertEqual(asyncio.run(self.grpc_snapshots(queue_id, len(over_sse))), over_sse)

    async def grpc_snapshots(self, queue_id: str, count: int) -> list[dict[str, Any]]:
        servicer = MonitorServicer(self.context, poll_interval=0.01)
        snapshots: list[dict[str, Any]] = []
        stream = servicer.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=queue_id), None)  # pyright: ignore[reportArgumentType]  (a known entry never reaches the context)
        async for message in stream:
            snapshots.append({name: getattr(message, name) if name in ("queue_id", "kind", "state") or message.HasField(name) else None for name in SNAPSHOT_FIELDS})
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
