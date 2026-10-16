"""End-to-end tests for the gRPC Monitor service: auth, WatchEvents (replay, live, Reset, include_output), and
WatchQueueEntry (change detection, unknown entry). A real grpc.aio server on an OS-assigned loopback port; the
client is a real grpc.aio channel, not a network socket in the sense of anything but loopback."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import grpc
import grpc.aio
import httpx

from framecontrol.jobs.executor import JobExecutor
from framecontrol.jobs.timing.forecast import Forecast
from framecontrol.server.app import create_app
from framecontrol.server.context import ServerContext
from framecontrol.server.generated import monitor_pb2, monitor_pb2_grpc
from framecontrol.server.grpc_auth import TokenAuthInterceptor
from framecontrol.server.grpc_service import MonitorServicer
from framecontrol.services.queue_hold import HoldState
from framecontrol.services.queue_submit import submit_job
from framecontrol.state.queue import QueueState
from framecontrol.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor
from tests.timing.builders import sample_forecast

TOKEN = "a" * 64


class FakeWorker:
    def __init__(self) -> None:
        self._current_id: int | None = None
        self._current_run: tuple[int, float] | None = None
        self._forecast: Forecast | None = None
        self._cooldown_until: float | None = None
        self.parking: set[int] = set()
        self.held = False
        # Called as the reservation is read, before the answer: a test's way to land a change between two reads.
        self.on_park_read: Callable[[int], None] | None = None

    def park_requested(self, entry_id: int) -> bool:
        if self.on_park_read is not None:
            self.on_park_read(entry_id)
        return entry_id in self.parking

    def hold_state(self) -> HoldState:
        return HoldState(held=self.held)

    def current_entry_id(self) -> int | None:
        return self._current_id

    def current_run(self) -> tuple[int, float] | None:
        return self._current_run

    def latest_forecast(self, entry_id: int) -> Forecast | None:
        return self._forecast if entry_id == self._current_id else None

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
        self.context = ServerContext(paths=self.paths, global_config=self.global_config, store=self.store, worker=self.worker, executor=executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8766, grpc_port=8767)  # pyright: ignore[reportArgumentType]  (FakeWorker only needs the methods the servicer calls)
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

    async def test_timing_events_arrive_whole_and_in_order_even_without_run_output(self) -> None:
        from framecontrol.jobs.events import StageFinished, StageStarted, TimingFinished, TimingStarted, event_from_dict
        from framecontrol.services.queue_events import QueueEventPublisher

        at = "2026-10-16T10:00:00+00:00"
        sent = [
            TimingStarted(at=at, number=1, backend="h3-metal-cli", pipeline="r2av", workload={"frames": 243}, environment={"load_start": [1.0, 0.5, 0.25]}),
            StageStarted(at=at, number=1, stage="denoise", phase="denoise enqueue", source="child", start=24.5),
            StageFinished(at=at, number=1, stage="denoise", phase="denoise enqueue", source="child", start=24.5, end=2174.2, done=30, total=30, unit="steps", marks=((120.5, 1), (2174.2, 30))),
            TimingFinished(at=at, number=1, complete=True, load_end=(2.0, 1.0, 0.5)),
        ]
        first = self.context.event_backlog.append("job_started", {})
        publisher = QueueEventPublisher(self.context.event_backlog.append)
        for event in sent:
            publisher.job_event(event)
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=first.id, include_output=False), metadata=self.auth())
        received = [await self.read(call) for _ in sent]
        call.cancel()
        self.assertEqual([event.kind for event in received], ["timing_started", "stage_started", "stage_finished", "timing_finished"])
        self.assertEqual([event_from_dict(json.loads(event.data_json)) for event in received], sent)

    async def test_an_id_this_backlog_no_longer_holds_gets_reset(self) -> None:
        call = self.stub.WatchEvents(monitor_pb2.WatchEventsRequest(last_event_id=1), metadata=self.auth())
        event = await self.read(call)
        self.assertEqual(event.kind, "reset")
        call.cancel()


class WatchQueueEntryTests(GrpcServiceTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_snapshot_does_not_stall_http_health(self) -> None:
        entry = self.submit()
        entered = threading.Event()
        release = threading.Event()

        def block_snapshot(_entry_id: int) -> None:
            entered.set()
            release.wait(2)

        self.worker.on_park_read = block_snapshot
        fallback = threading.Timer(2, release.set)
        fallback.start()
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        start = time.monotonic()
        reading = asyncio.create_task(call.read())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.context)), base_url="http://127.0.0.1:8766") as client:
                response = await asyncio.wait_for(client.get("/v1/health"), timeout=1)
                self.assertEqual(response.status_code, 200)
                self.assertLess(time.monotonic() - start, 1)
        finally:
            release.set()
            fallback.cancel()
            await asyncio.wait_for(reading, timeout=2)
            call.cancel()

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
        self.assertEqual((first.queue_id, first.state, first.total_runs), (entry.label, "queued", 1))
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

    async def test_the_current_run_is_reported_while_the_entry_is_the_one_running(self) -> None:
        entry = self.submit(run_count=1)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        first = await self.read(call)
        self.assertFalse(first.HasField("current_run"))
        self.worker._current_id = claimed.id
        self.worker._current_run = (1, 12.5)
        second = await self.read(call)
        self.assertEqual((second.current_run, second.current_run_elapsed_seconds), (1, 12.5))
        call.cancel()

    async def test_the_forecast_arrives_as_a_nested_message_and_each_republish_sends_one(self) -> None:
        entry = self.submit(run_count=1)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        self.worker._current_id = claimed.id
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        first = await self.read(call)
        self.assertFalse(first.HasField("forecast"))
        self.worker._forecast = sample_forecast(1_000_000.0, penalty=2.0)
        second = await self.read(call)
        forecast = second.forecast
        self.assertEqual((forecast.as_of, forecast.stage, forecast.stage_done, forecast.stage_total), (1_000_000.0, "denoise", 12, 30))
        self.assertEqual((forecast.run.started_at, forecast.run.end_at, forecast.run.low, forecast.run.high, forecast.run.floor), (999_400.0, 1_001_400.0, 1_001_200.0, 1_001_700.0, 0.3))
        self.assertEqual((forecast.job.end_at, forecast.job.floor), (1_005_000.0, 0.15))
        self.assertEqual((forecast.basis.tier, forecast.basis.runs, forecast.basis.executions, list(forecast.basis.unseen), forecast.basis.penalty), ("signature", 8, 4, ["other"], 2.0))
        # A forecast with no number for the run, no stage units, and no penalty leaves each of them unset.
        self.worker._forecast = replace(sample_forecast(1_000_001.0, run=False), stage_done=None, stage_total=None)
        third = await self.read(call)
        self.assertFalse(third.forecast.HasField("run"))
        self.assertFalse(third.forecast.HasField("stage_done"))
        self.assertFalse(third.forecast.basis.HasField("penalty"))
        self.assertTrue(third.forecast.HasField("job"))
        # Cleared, the field is unset again.
        self.worker._forecast = None
        self.assertFalse((await self.read(call)).HasField("forecast"))
        call.cancel()

    async def test_elapsed_seconds_alone_changing_sends_no_message(self) -> None:
        entry = self.submit(run_count=1)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        self.worker._current_id = claimed.id
        self.worker._current_run = (1, 1.0)
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        await self.read(call)
        self.worker._current_run = (1, 2.0)
        await asyncio.sleep(0.15)  # several polls with only the elapsed seconds changed
        self.worker._current_run = (2, 0.5)
        second = await self.read(call)
        self.assertEqual((second.current_run, second.current_run_elapsed_seconds), (2, 0.5))
        call.cancel()

    async def test_a_park_reservation_and_the_hold_each_send_a_snapshot(self) -> None:
        entry = self.submit(run_count=1)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        first = await self.read(call)
        # Always set, False included.
        self.assertTrue(first.HasField("park_requested") and first.HasField("queue_held"))
        self.assertEqual((first.park_requested, first.queue_held), (False, False))
        self.worker.parking.add(claimed.id)
        self.assertEqual((await self.read(call)).park_requested, True)
        self.worker.held = True
        self.assertEqual((await self.read(call)).queue_held, True)
        self.worker.parking.clear()
        self.assertEqual((await self.read(call)).park_requested, False)
        call.cancel()

    async def test_a_job_that_parks_between_the_reads_never_shows_running_without_its_reservation(self) -> None:
        entry = self.submit(run_count=2)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        self.worker.parking.add(claimed.id)
        call = self.stub.WatchQueueEntry(monitor_pb2.WatchQueueEntryRequest(queue_id=entry.label), metadata=self.auth())
        first = await self.read(call)
        self.assertEqual((first.state, first.park_requested), ("running", True))

        def park_as_it_is_read(entry_id: int) -> None:
            # As the worker does: the entry is marked parked, then the reservation is dropped.
            self.store.queue.finish(entry_id, state=QueueState.PARKED, finished_at="2026-09-29T12:00:00+00:00")
            self.worker.parking.discard(entry_id)

        self.worker.on_park_read = park_as_it_is_read
        self.assertEqual((await self.read(call)).state, "parked")
        call.cancel()


if __name__ == "__main__":
    unittest.main()
