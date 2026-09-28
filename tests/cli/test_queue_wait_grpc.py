"""``dtc queue add --wait`` against a real gRPC server (``MonitorServicer``, not a fake stub): the only test that
exercises ``queue_wait._run``'s real ``grpc.aio.insecure_channel`` -> ``MonitorStub`` -> ``call.cancel()`` path, and
the auth metadata it sends."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

import grpc
import grpc.aio
import httpx

from draw_things_control.cli.queue_wait import _run, wait_for_entry
from draw_things_control.core.exit_codes import EXIT_INVALID_INPUT, EXIT_STATE_UNAVAILABLE
from draw_things_control.server.generated import monitor_pb2_grpc
from draw_things_control.server.grpc_auth import TokenAuthInterceptor
from draw_things_control.server.grpc_service import MonitorServicer
from draw_things_control.state.queue import QueueState
from tests.server.test_grpc_service import TOKEN, GrpcServiceTestCase


class QueueWaitRealGrpcTests(GrpcServiceTestCase, unittest.IsolatedAsyncioTestCase):
    """Its own ``asyncSetUp``, not the base class's: that one builds a channel and stub for the *server's* own
    tests to read from directly, but keeps the port it bound to itself; ``_run`` below dials its own channel from a
    target string, exactly as ``dtc queue add --wait`` does from ``GET /v1/health``'s ``grpc_port``, so this test
    needs the port kept, not a channel already open."""

    async def asyncSetUp(self) -> None:
        self.server = grpc.aio.server(interceptors=[TokenAuthInterceptor(TOKEN)])
        monitor_pb2_grpc.add_MonitorServicer_to_server(MonitorServicer(self.context, poll_interval=0.02), self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        await self.server.start()
        self.target = f"127.0.0.1:{port}"
        self.channel = None

    async def asyncTearDown(self) -> None:
        assert self.server is not None
        await self.server.stop(None)

    def auth(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {TOKEN}"),)

    async def test_a_finished_entry_is_read_over_a_real_channel_and_the_metadata_is_accepted(self) -> None:
        entry = self.submit(run_count=1)
        self.store.queue.claim_oldest(datetime.now())
        self.store.queue.finish(entry.id, state=QueueState.SUCCEEDED, finished_at="2026-09-29T10:00:00+00:00")
        state, error, last_run, total_runs = await asyncio.wait_for(_run(self.target, None, entry.label, self.auth()), timeout=5.0)
        self.assertEqual((state, error, total_runs), ("succeeded", None, 1))
        self.assertIsNone(last_run)

    async def test_a_wrong_token_ends_the_wait_with_an_aio_rpc_error(self) -> None:
        entry = self.submit(run_count=1)
        with self.assertRaises(grpc.aio.AioRpcError) as caught:
            await asyncio.wait_for(_run(self.target, None, entry.label, (("authorization", "Bearer wrong"),)), timeout=5.0)
        self.assertEqual(caught.exception.code(), grpc.StatusCode.UNAUTHENTICATED)

    async def test_an_unknown_entry_ends_the_wait_with_not_found(self) -> None:
        with self.assertRaises(grpc.aio.AioRpcError) as caught:
            await asyncio.wait_for(_run(self.target, None, "Q9999", self.auth()), timeout=5.0)
        self.assertEqual(caught.exception.code(), grpc.StatusCode.NOT_FOUND)

    def health_client(self) -> httpx.Client:
        """A real HTTP client isn't needed: ``wait_for_entry`` only ever reads ``GET /v1/health`` from it, for the
        gRPC port -- everything else it does goes straight to the real gRPC server this test already started."""
        host, port = self.target.split(":")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok", "version": "1.0.0", "worker_alive": True, "grpc_port": int(port)})

        return httpx.Client(base_url=f"http://{host}:8765", transport=httpx.MockTransport(handler), headers={"Authorization": f"Bearer {TOKEN}"})

    async def test_wait_for_entry_maps_not_found_to_exit_2(self) -> None:
        with self.health_client() as client:
            exit_code = await asyncio.get_running_loop().run_in_executor(None, wait_for_entry, client, "Q9999", None)
        self.assertEqual(exit_code, EXIT_INVALID_INPUT)

    async def test_wait_for_entry_maps_a_wrong_token_to_exit_1(self) -> None:
        with self.health_client() as client:
            client.headers["Authorization"] = "Bearer wrong"
            entry = self.submit(run_count=1)
            exit_code = await asyncio.get_running_loop().run_in_executor(None, wait_for_entry, client, entry.label, None)
        self.assertEqual(exit_code, EXIT_STATE_UNAVAILABLE)
