"""``get_queue_entry``'s wait (Milestone 10): it follows the API's SSE watch, which an in-process transport collects
whole, so these tests run the API under uvicorn on a loopback port and the MCP server reaches it over the network. A
scripted worker changes what each snapshot holds, by the count of snapshots read."""

from __future__ import annotations

import time
import unittest
from typing import Any

import anyio

from draw_things_control.mcp_server.app import build_server
from draw_things_control.mcp_server.watch import FINISHED_STATES, WaitTimes
from draw_things_control.server.context import ServerContext
from draw_things_control.state.queue import FINISHED_STATES as STORE_FINISHED_STATES
from draw_things_control.state.queue import QueueState
from tests.mcp_server.mcp_case import McpCase
from tests.server.live_app import LiveApp, loopback_socket, socket_port
from tests.server.test_queue_routes import TOKEN
from tests.server.test_queue_watch import ScriptedWorker


class FinishedStatesTests(unittest.TestCase):
    def test_the_copy_of_the_finished_states_is_the_stores(self) -> None:
        self.assertEqual(FINISHED_STATES, {str(state) for state in STORE_FINISHED_STATES})


class InProcessWaitTests(McpCase):
    async def test_a_finished_entry_is_answered_at_once_without_a_watch(self) -> None:
        self.write_catalog_job("walk.yaml", run_count=1)
        queue_id = (await self.api("POST", "/v1/queue", json={"job": "walk.yaml"}, caller="mcp")).json()["queue_id"]
        await self.api("POST", f"/v1/queue/{queue_id}/cancel")
        async with self.client() as client:
            body = await self.ok(client, "get_queue_entry", {"queue_id": queue_id, "wait_seconds": 7200})
        self.assertEqual((body["state"], body["changed"]), ("cancelled", False))
        self.assertEqual({key: value for key, value in body.items() if key != "changed"}, (await self.api("GET", f"/v1/queue/{queue_id}")).json())
        self.assertEqual(self.transport.sent()[-1], ("GET", f"/v1/queue/{queue_id}"))
        self.assertNotIn(("GET", f"/v1/queue/{queue_id}/watch"), self.transport.sent())

    async def test_an_unknown_entry_is_not_found(self) -> None:
        async with self.client() as client:
            self.assertEqual((await self.error(client, "get_queue_entry", {"queue_id": "Q9999", "wait_seconds": 5}))["code"], "not_found")

    async def test_wait_seconds_is_one_to_7200(self) -> None:
        async with self.client() as client:
            for value in (0, 7201, -5, 1.5, "60"):
                with self.subTest(value=value):
                    refused = await self.error(client, "get_queue_entry", {"queue_id": "Q0001", "wait_seconds": value})
                    self.assertEqual((refused["code"], refused["field"]), ("invalid_input", "wait_seconds"))
            self.assertEqual(self.transport.sent(), [])


class LiveWaitTests(McpCase):
    """Under uvicorn, with short times: a progress notification every 0.05 seconds, 3 seconds without a token, and a
    watch silent for 0.5 seconds counted as dropped. The read before the watch counts as one snapshot read, so the
    watch's own first is read 2."""

    times = WaitTimes(progress_every=0.05, without_progress_token=3.0, silence=0.5)

    def setUp(self) -> None:
        super().setUp()
        self.worker = ScriptedWorker(self.store, self.event_backlog)
        self.serve()
        sock = loopback_socket()
        self.live_context = ServerContext(paths=self.paths, global_config=self.global_config, store=self.store, worker=self.worker, executor=self.executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=socket_port(sock), grpc_port=8766, event_backlog=self.event_backlog, watch_poll_seconds=0.01, watch_keepalive_seconds=0.1)  # pyright: ignore[reportArgumentType]  (the fake worker only needs the methods the routes call)
        self.live = LiveApp(self.live_context, sock)
        self.live.start()
        self.addCleanup(self.live.stop)
        self.server = build_server(self.live.base_url, self.paths.server_token, wait_times=self.times)

    async def running_entry(self, current_run: tuple[int, float] | None = (2, 5.0)) -> str:
        self.write_catalog_job("walk.yaml", run_count=5)
        queue_id = (await self.api("POST", "/v1/queue", json={"job": "walk.yaml"}, caller="mcp")).json()["queue_id"]
        self.make_running(queue_id)
        self.worker._current_run = current_run
        return queue_id

    def script(self, changes: dict[int, Any]) -> None:
        """Calls ``changes[n]`` as the watch reads snapshot ``n`` (counted from the first the watch reads)."""
        self.worker.reads = 0
        self.worker.on_read = lambda count: changes[count]() if count in changes else None

    async def wait(self, client: Any, queue_id: str, wait_seconds: int, **options: Any) -> tuple[dict[str, Any], float]:
        started = time.monotonic()
        result = await client.call_tool("get_queue_entry", {"queue_id": queue_id, "wait_seconds": wait_seconds}, **options)
        assert isinstance(result.structured_content, dict)
        return result.structured_content, time.monotonic() - started

    async def test_it_returns_when_the_entry_finishes_with_what_an_agent_needs_next(self) -> None:
        queue_id = await self.running_entry()
        entry = self.store.queue.by_number(int(queue_id[1:]))
        assert entry is not None
        self.script({5: lambda: self.store.queue.finish(entry.id, state=QueueState.FAILED, finished_at="2026-10-03T10:00:00+00:00")})
        async with self.client() as client:
            body, elapsed = await self.wait(client, queue_id, 60)
        self.assertEqual((body["state"], body["changed"]), ("failed", True))
        self.assertTrue({"last_run_seconds", "resumable", "resume_from_run", "resume_refused_reason"} <= set(body))
        self.assertLess(elapsed, 5)

    async def test_it_returns_at_a_runs_end_not_at_a_step(self) -> None:
        queue_id = await self.running_entry()

        def end_run() -> None:
            self.worker._current_run = self.worker._current_step = None

        self.script({3: lambda: setattr(self.worker, "_current_step", (4, 30)), 6: lambda: setattr(self.worker, "_current_step", (5, 30)), 10: end_run})
        async with self.client() as client:
            body, _elapsed = await self.wait(client, queue_id, 60)
        self.assertEqual((body["changed"], body["current_run"]), (True, None))
        self.assertGreaterEqual(self.worker.reads, 10)

    async def test_the_next_runs_start_and_its_steps_do_not_end_it(self) -> None:
        queue_id = await self.running_entry(current_run=None)
        self.script({3: lambda: setattr(self.worker, "_current_run", (3, 0.0)), 6: lambda: setattr(self.worker, "_current_step", (1, 30))})
        async with self.client() as client:
            body, elapsed = await self.wait(client, queue_id, 1)
        self.assertEqual((body["changed"], body["current_run"], body["current_step"]), (False, 3, 1))
        self.assertGreaterEqual(elapsed, 1)
        self.assertGreater(self.worker.reads, 6)

    async def test_a_park_and_the_hold_end_it(self) -> None:
        queue_id = await self.running_entry()
        self.script({4: lambda: self.worker.parking.add(self.worker._current_id or 0)})
        async with self.client() as client:
            body, _elapsed = await self.wait(client, queue_id, 60)
        self.assertEqual((body["changed"], body["park_requested"]), (True, True))

    async def test_each_field_an_agent_acts_on_ends_it(self) -> None:
        def entry_id() -> int:
            assert self.worker._current_id is not None
            return self.worker._current_id

        changes: dict[str, Any] = {
            "the next run, with no cooldown between": lambda: setattr(self.worker, "_current_run", (3, 0.0)),
            "the hold": lambda: self.worker.hold("tui"),
            "the between-jobs wait": lambda: setattr(self.worker, "cooldown_until", lambda: 1_900_000_000.0),
            "an error": lambda: self.store.queue.set_error(entry_id(), "boom"),
            "the execution": lambda: self.store.queue.link_execution(entry_id(), 7),
        }
        async with self.client() as client:
            for name, change in changes.items():
                with self.subTest(name):
                    queue_id = await self.running_entry()
                    self.script({4: change})
                    body, elapsed = await self.wait(client, queue_id, 30)
                    self.assertEqual(body["changed"], True)
                    self.assertLess(elapsed, 5)
                    self.worker.release("tui")
                    self.worker.__dict__.pop("cooldown_until", None)
                    self.store.queue.finish(entry_id(), state=QueueState.FAILED, finished_at="2026-10-03T10:00:00+00:00")
                    self.worker._current_id = None

    async def test_an_entry_that_finishes_between_the_read_and_the_watch_ends_it_at_once(self) -> None:
        queue_id = await self.running_entry()
        entry = self.store.queue.by_number(int(queue_id[1:]))
        assert entry is not None
        # Read 1 is the read before the watch; read 2, the watch's first snapshot, finds the entry finished.
        self.script({2: lambda: self.store.queue.finish(entry.id, state=QueueState.SUCCEEDED, finished_at="2026-10-03T10:00:00+00:00")})
        async with self.client() as client:
            body, elapsed = await self.wait(client, queue_id, 30)
        self.assertEqual((body["state"], body["changed"]), ("succeeded", True))
        self.assertLess(elapsed, 5)

    async def test_a_watch_the_entry_leaves_answers_not_found(self) -> None:
        queue_id = await self.running_entry()
        entry = self.store.queue.by_number(int(queue_id[1:]))
        assert entry is not None

        def remove() -> None:
            self.store.queue.finish(entry.id, state=QueueState.FAILED, finished_at="2020-01-01T00:00:00+00:00")
            self.store.queue.prune(time.time())

        self.script({4: remove})
        async with self.client() as client:
            body, _elapsed = await self.wait(client, queue_id, 30)
        self.assertEqual(body["code"], "not_found")

    async def test_with_a_progress_token_it_reports_progress_and_waits_past_the_limit_without_one(self) -> None:
        queue_id = await self.running_entry()
        progress: list[tuple[float, float | None]] = []

        async def on_progress(value: float, total: float | None, _message: str | None) -> None:
            progress.append((value, total))

        self.server = build_server(self.live.base_url, self.paths.server_token, wait_times=WaitTimes(progress_every=0.05, without_progress_token=0.3, silence=0.5))
        async with self.client(mode="legacy") as client:
            body, elapsed = await self.wait(client, queue_id, 1, progress_callback=on_progress)
        self.assertEqual(body["changed"], False)
        self.assertGreaterEqual(elapsed, 1)
        self.assertGreaterEqual(len(progress), 5)
        self.assertEqual({total for _value, total in progress}, {1.0})
        self.assertEqual([value for value, _total in progress], sorted(value for value, _total in progress))

    async def test_without_a_progress_token_it_waits_no_longer_than_the_limit(self) -> None:
        queue_id = await self.running_entry()
        self.server = build_server(self.live.base_url, self.paths.server_token, wait_times=WaitTimes(progress_every=0.05, without_progress_token=0.3, silence=0.5))
        async with self.client(mode="legacy") as client:
            body, elapsed = await self.wait(client, queue_id, 7200)
        self.assertEqual(body["changed"], False)
        self.assertLess(elapsed, 2)

    async def test_it_never_waits_longer_than_asked(self) -> None:
        queue_id = await self.running_entry()
        async with self.client() as client:
            body, elapsed = await self.wait(client, queue_id, 1)
        self.assertEqual(body["changed"], False)
        self.assertLess(elapsed, 2)

    async def test_a_watch_that_drops_is_server_unreachable(self) -> None:
        queue_id = await self.running_entry()
        body: dict[str, Any] = {}
        async with self.client() as client:
            async with anyio.create_task_group() as group:
                group.start_soon(anyio.to_thread.run_sync, self.stop_soon)
                body, _elapsed = await self.wait(client, queue_id, 60)
        self.assertEqual(body["code"], "server_unreachable")
        self.assertIn("dtc serve", body["message"])

    def stop_soon(self) -> None:
        time.sleep(0.3)
        self.live.stop()

    async def test_a_silent_watch_is_server_unreachable(self) -> None:
        self.live_context.watch_keepalive_seconds = 100
        queue_id = await self.running_entry()
        async with self.client() as client:
            body, elapsed = await self.wait(client, queue_id, 60)
        self.assertEqual(body["code"], "server_unreachable")
        self.assertIn("0.5 seconds", body["message"])
        self.assertLess(elapsed, 5)

    async def test_a_keep_alive_keeps_a_quiet_watch_from_counting_as_dropped(self) -> None:
        queue_id = await self.running_entry()
        async with self.client() as client:
            body, elapsed = await self.wait(client, queue_id, 2)
        self.assertEqual(body["changed"], False)
        self.assertGreaterEqual(elapsed, 2)

    async def test_a_cancelled_call_closes_its_watch(self) -> None:
        queue_id = await self.running_entry()
        async with self.client() as client:
            with anyio.move_on_after(0.5):
                await self.wait(client, queue_id, 60)
            await anyio.sleep(0.5)
            reads = self.worker.reads
            await anyio.sleep(0.5)
        self.assertEqual(self.worker.reads, reads, "the watch kept reading its entry after the call was cancelled")

    async def test_another_call_is_answered_while_it_waits(self) -> None:
        queue_id = await self.running_entry()
        answered: list[float] = []
        async with self.client() as client:
            async with anyio.create_task_group() as group:
                group.start_soon(self.wait, client, queue_id, 2)
                await anyio.sleep(0.3)
                started = time.monotonic()
                await self.ok(client, "get_queue")
                answered.append(time.monotonic() - started)
        self.assertLess(answered[0], 1)


if __name__ == "__main__":
    unittest.main()
