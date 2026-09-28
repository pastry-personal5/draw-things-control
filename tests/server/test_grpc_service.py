"""End-to-end tests for the gRPC Monitor service: auth, WatchEvents (replay, live, Reset, include_output), and
WatchQueueEntry (change detection, unknown entry). A real grpc.aio server on an OS-assigned loopback port; the
client is a real grpc.aio channel, not a network socket in the sense of anything but loopback."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

import grpc
import grpc.aio

from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server.context import ServerContext
from draw_things_control.server.generated import monitor_pb2, monitor_pb2_grpc
from draw_things_control.server.grpc_auth import TokenAuthInterceptor
from draw_things_control.server.grpc_service import MonitorServicer
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor

TOKEN = "a" * 64


class FakeWorker:
    def __init__(self) -> None:
        self._current_id: int | None = None
        self._current_run: tuple[int, float] | None = None
        self._cooldown_until: float | None = None

    def current_entry_id(self) -> int | None:
        return self._current_id

    def current_run(self) -> tuple[int, float] | None:
        return self._current_run

    def cooldown_until(self) -> float | None:
        return self._cooldown_until

    def is_alive(self) -> bool:
        return True


class GrpcServiceTestCase(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        executor: JobExecutor = job_executor(runner_factory=lambda *a: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False)
        self.worker = FakeWorker()
        self.context = ServerContext(paths=self.paths, global_config=self.global_config, store=self.store, worker=self.worker, executor=executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8766)  # pyright: ignore[reportArgumentType]  (FakeWorker only needs the methods the servicer calls)
        self.server: grpc.aio.Server | None = None
        self.channel: grpc.aio.Channel | None = None

    async def asyncSetUp(self) -> None:  # unittest.IsolatedAsyncioTestCase calls this
        self.server = grpc.aio.server(interceptors=[TokenAuthInterceptor(TOKEN)])
        monitor_pb2_grpc.add_MonitorServicer_to_server(MonitorServicer(self.context, poll_interval=0.02), self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        await self.server.start()
        self.channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.stub = monitor_pb2_grpc.MonitorStub(self.channel)

    async def asyncTearDown(self) -> None:
        assert self.channel is not None and self.server is not None
        await self.channel.close()
        await self.server.stop(None)

    def auth(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {TOKEN}"),)

    def submit(self, name: str = "job.yaml", **changes: object):
        changes.setdefault("run_timeout_seconds", 60)
        path = self.write_job(job_data(prompt_pairs=[{"name": "only", "positive": "text"}], **changes), name)
        return submit_job(path, self.global_config, self.params, self.store)

    @staticmethod
    async def read(call, timeout: float = 2.0):
        """``call.read()``, bounded: a defect that would otherwise hang forever fails the test instead."""
        return await asyncio.wait_for(call.read(), timeout=timeout)


class WatchEventsAuthTests(GrpcServiceTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_no_token_is_unauthenticated(self) -> None:
        with self.assertRaises(grpc.aio.AioRpcError) as caught:
            async for _event in self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=0)):
                pass
        self.assertEqual(caught.exception.code(), grpc.StatusCode.UNAUTHENTICATED)

    async def test_a_wrong_token_is_unauthenticated(self) -> None:
        with self.assertRaises(grpc.aio.AioRpcError) as caught:
            async for _event in self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=0), metadata=(("authorization", "Bearer wrong"),)):
                pass
        self.assertEqual(caught.exception.code(), grpc.StatusCode.UNAUTHENTICATED)


class WatchEventsTests(GrpcServiceTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_last_event_id_zero_only_streams_events_from_now_on(self) -> None:
        self.context.event_backlog.append("job_started", {"job_name": "before"})
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=0), metadata=self.auth())
        # Give the RPC a moment to actually reach the server (and so run WatchEvents' initial replay, seeding
        # last_id at the current latest) before appending "after": otherwise nothing guarantees the server has not
        # already read past "after" before computing that seed, an inherent race in this "only from now on" case.
        await asyncio.sleep(0.1)
        self.context.event_backlog.append("job_started", {"job_name": "after"})
        event = await self.read(call)
        self.assertEqual(event.kind, "job_started")
        self.assertIn("after", event.data_json)
        call.cancel()

    async def test_replay_from_a_known_id_then_live_events_follow(self) -> None:
        first = self.context.event_backlog.append("job_started", {})
        self.context.event_backlog.append("run_started", {"number": 1})
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=first.id), metadata=self.auth())
        replayed = await self.read(call)
        self.assertEqual(replayed.kind, "run_started")
        self.context.event_backlog.append("job_finished", {})
        live = await self.read(call)
        self.assertEqual(live.kind, "job_finished")
        call.cancel()

    async def test_run_output_is_filtered_unless_include_output_is_set(self) -> None:
        first = self.context.event_backlog.append("job_started", {})
        self.context.event_backlog.append("run_output", {"text": "line"})
        self.context.event_backlog.append("run_finished", {})
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=first.id, include_output=False), metadata=self.auth())
        event = await self.read(call)
        self.assertEqual(event.kind, "run_finished")
        call.cancel()

    async def test_include_output_true_delivers_run_output(self) -> None:
        first = self.context.event_backlog.append("job_started", {})
        self.context.event_backlog.append("run_output", {"text": "line"})
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=first.id, include_output=True), metadata=self.auth())
        event = await self.read(call)
        self.assertEqual(event.kind, "run_output")
        call.cancel()

    async def test_an_id_this_backlog_no_longer_holds_gets_reset(self) -> None:
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=1), metadata=self.auth())
        event = await self.read(call)
        self.assertEqual(event.kind, "reset")
        call.cancel()


class WatchQueueEntryTests(GrpcServiceTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_an_unknown_entry_ends_not_found(self) -> None:
        with self.assertRaises(grpc.aio.AioRpcError) as caught:
            async for _snapshot in self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id="Q9999"), metadata=self.auth()):
                pass
        self.assertEqual(caught.exception.code(), grpc.StatusCode.NOT_FOUND)

    async def test_a_malformed_id_ends_not_found(self) -> None:
        with self.assertRaises(grpc.aio.AioRpcError) as caught:
            async for _snapshot in self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id="not-an-id"), metadata=self.auth()):
                pass
        self.assertEqual(caught.exception.code(), grpc.StatusCode.NOT_FOUND)

    async def test_the_first_snapshot_and_a_state_change_are_both_delivered(self) -> None:
        entry = self.submit(run_count=1)
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        first = await self.read(call)
        self.assertEqual((first.queue_id, first.state), (entry.label, "queued"))
        self.store.queue.claim_oldest(datetime.now())
        second = await self.read(call)
        self.assertEqual(second.state, "running")
        call.cancel()

    async def test_no_second_message_when_nothing_changes(self) -> None:
        entry = self.submit(run_count=1)
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        await self.read(call)
        with self.assertRaises(TimeoutError):
            await self.read(call, timeout=0.15)
        call.cancel()


if __name__ == "__main__":
    unittest.main()
