"""``dtc queue add --wait``: watches one entry over gRPC (``WatchQueueEntry``) until it finishes, printing each run's
start and its outcome, `run-job`'s own replacement now that only `dtc serve` ever starts a run. Its gRPC target is
derived from ``GET /v1/health``'s ``grpc_port`` and the already-configured HTTP host, with no flag of its own
(Milestone 03); the stub comes from ``cli/generated/``, this front end's own copy (``server/`` holds another,
neither importing the other's: ``tests/test_architecture.py``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit

import typer
from loguru import logger

from draw_things_control.cli.generated import monitor_pb2, monitor_pb2_grpc
from draw_things_control.core.exit_codes import EXIT_CODES_BY_QUEUE_STATE, EXIT_INVALID_INPUT, EXIT_STATE_UNAVAILABLE
from draw_things_control.core.network import grpc_target
from draw_things_control.services.queue_park_text import park_point
from draw_things_control.state.queue import FINISHED_STATES

# Whether a state is final is state/queue.py's own FINISHED_STATES, not EXIT_CODES_BY_QUEUE_STATE (which exists to
# map an already-known-final state to an exit code): a state added to one without the other must not leave this
# stuck watching forever while tui/feed.py, which already keys off FINISHED_STATES, correctly stops following it.
_FINISHED_STATE_VALUES = frozenset(str(state) for state in FINISHED_STATES)

if TYPE_CHECKING:
    import grpc.aio
    import httpx


class QueueEntryCall(Protocol):
    """What ``stub.WatchQueueEntry(...)`` returns: a real ``grpc.aio.UnaryStreamCall`` (untyped, since ``make
    proto`` writes no ``.pyi`` for ``monitor_pb2_grpc.py``), or a test's own fake -- either way, async-iterable and
    cancellable."""

    def __aiter__(self) -> AsyncIterator[monitor_pb2.QueueEntrySnapshot]: ...
    def cancel(self) -> None: ...


class MonitorStub(Protocol):
    """What ``add --wait`` needs from a gRPC stub: only ``WatchQueueEntry``, real or faked (``grpc_stub_factory``,
    a test's own seam)."""

    def WatchQueueEntry(self, request: monitor_pb2.WatchQueueEntryRequest, metadata: tuple[tuple[str, str], ...] | None = None) -> QueueEntryCall: ...


def wait_for_entry(client: "httpx.Client", queue_id: str, grpc_stub_factory: Callable[[str], object] | None) -> int:
    """Watches ``queue_id`` until it reaches a final state; returns the exit code ``dtc queue add --wait`` should
    end with. ``client`` is the already-authenticated HTTP client ``add`` built (reused for ``GET /v1/health`` and,
    on Ctrl-C, the cancel it issues); ``grpc_stub_factory``, when given (a test's own, typed generically on
    ``CliServices`` so ``cli/context.py`` need not import this module's own ``MonitorStub`` back), replaces dialing
    a real channel -- cast to ``MonitorStub`` in ``_stub`` below, the one place its actual shape matters."""
    import httpx

    try:
        health = client.get("/v1/health")
        grpc_port = health.json()["grpc_port"]
    except httpx.HTTPError as error:
        logger.error("Cannot reach the server at {}: {}; is 'dtc serve' running?", client.base_url, error)
        return EXIT_STATE_UNAVAILABLE
    except KeyError:
        logger.error("The server at {} has no gRPC port in its health check; restart 'dtc serve'", client.base_url)
        return EXIT_STATE_UNAVAILABLE
    target = grpc_target(urlsplit(str(client.base_url)).hostname or "127.0.0.1", grpc_port)
    token = client.headers.get("authorization", "")
    metadata = (("authorization", token),)
    import grpc.aio

    try:
        state, error, last_run, total_runs = asyncio.run(_run(target, grpc_stub_factory, queue_id, metadata))
    except KeyboardInterrupt:
        try:
            cancel_response = client.post(f"/v1/queue/{queue_id}/cancel")
            cancel_response.raise_for_status()
        except httpx.HTTPError as cancel_error:
            logger.error("Cannot reach the server at {}: {}; is 'dtc serve' running?", client.base_url, cancel_error)
            return EXIT_STATE_UNAVAILABLE
        typer.echo(f"{queue_id} cancelled")
        return EXIT_CODES_BY_QUEUE_STATE["cancelled"]
    except grpc.aio.AioRpcError as error:
        return _exit_for_rpc_error(queue_id, target, error)
    parked_after = _parked_after(client, queue_id, last_run) if state == "parked" else None
    _print_outcome(queue_id, state, error, last_run, total_runs, parked_after)
    return EXIT_CODES_BY_QUEUE_STATE.get(state, 1)


def _parked_after(client: "httpx.Client", queue_id: str, last_run: int | None) -> int | None:
    """The run a parked entry parked after: its stored succeeded count (``GET /v1/queue/{id}``), since a run shorter
    than a poll is never seen as a snapshot's current run. The last run seen when that read fails."""
    import httpx

    try:
        response = client.get(f"/v1/queue/{queue_id}")
        response.raise_for_status()
        return int(response.json()["succeeded"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return last_run


def _exit_for_rpc_error(queue_id: str, target: str, error: "grpc.aio.AioRpcError") -> int:
    import grpc

    if error.code() == grpc.StatusCode.NOT_FOUND:
        logger.error("No queue entry {}", queue_id)
        return EXIT_INVALID_INPUT
    logger.error("Cannot reach the server's gRPC service at {}: {}; is 'dtc serve' running?", target, error)
    return EXIT_STATE_UNAVAILABLE


async def _run(target: str, grpc_stub_factory: Callable[[str], object] | None, queue_id: str, metadata: tuple[tuple[str, str], ...]) -> tuple[str, str | None, int | None, int | None]:
    async with _stub(target, grpc_stub_factory) as stub:
        return await _watch(stub, queue_id, metadata)


@asynccontextmanager
async def _stub(target: str, grpc_stub_factory: Callable[[str], object] | None) -> AsyncIterator[MonitorStub]:
    if grpc_stub_factory is not None:
        yield cast(MonitorStub, grpc_stub_factory(target))
        return
    import grpc.aio

    async with grpc.aio.insecure_channel(target) as channel:
        yield monitor_pb2_grpc.MonitorStub(channel)


async def _watch(stub: MonitorStub, queue_id: str, metadata: tuple[tuple[str, str], ...]) -> tuple[str, str | None, int | None, int | None]:
    """Streams snapshots until one names a final state; ``WatchQueueEntry`` never ends on its own, so this always
    cancels the call itself on the way out, finished or not (Ctrl-C, or a gRPC error, from the caller)."""
    request = monitor_pb2.WatchQueueEntryRequest(queue_id=queue_id)
    call = stub.WatchQueueEntry(request, metadata=metadata)
    last_printed_run: int | None = None
    last_run: int | None = None
    total_runs: int | None = None
    parking = False
    told_held = False
    try:
        async for snapshot in call:
            if snapshot.HasField("total_runs"):
                total_runs = snapshot.total_runs
            if snapshot.HasField("current_run") and snapshot.current_run != last_printed_run:
                if last_printed_run is not None:
                    typer.echo(f"run {last_printed_run}/{total_runs} succeeded")
                typer.echo(f"run {snapshot.current_run}/{total_runs} started")
                last_printed_run = last_run = snapshot.current_run
            if snapshot.state in _FINISHED_STATE_VALUES:
                return snapshot.state, snapshot.error if snapshot.HasField("error") else None, last_run, total_runs
            # Milestone 05: a park reservation made or withdrawn, from anywhere, and a hold its queued entry waits behind.
            if snapshot.state == "running" and snapshot.park_requested != parking:
                parking = snapshot.park_requested
                typer.echo(_parking_text(queue_id, parking, last_run, total_runs))
            if snapshot.state == "queued" and snapshot.queue_held and not told_held:
                told_held = True
                typer.echo(f"{queue_id} waits: the queue is held ('dtc queue release' starts it)")
        raise RuntimeError("WatchQueueEntry ended without a final state")
    finally:
        call.cancel()


def _parking_text(queue_id: str, parking: bool, last_run: int | None, total_runs: int | None) -> str:
    if not parking:
        return f"{queue_id} runs on"
    point = park_point(last_run, total_runs)
    return f"{queue_id} parking: " + (f"it ends after {point}" if point is not None else "it is on its last run and will finish")


def _print_outcome(queue_id: str, state: str, error: str | None, last_run: int | None, total_runs: int | None, parked_after: int | None = None) -> None:
    if state == "parked":
        # The last run seen start succeeded; the run it parked after, numbered in the chain, is the stored one.
        if last_run is not None:
            typer.echo(f"run {last_run}/{total_runs} succeeded")
        if parked_after:
            typer.echo(f"{queue_id} parked after run {parked_after}/{total_runs}; 'dtc queue resume {queue_id}' continues at run {parked_after + 1}")
        else:
            typer.echo(f"{queue_id} parked; 'dtc queue resume {queue_id}' continues at its next run")
        return
    if state == "succeeded":
        if last_run is not None:
            typer.echo(f"run {last_run}/{total_runs} succeeded")
        typer.echo(f"{queue_id} succeeded")
        return
    if last_run is not None:
        typer.echo(f"run {last_run}/{total_runs} {state}")
    if error:
        logger.error("{}", error)
    typer.echo(f"{queue_id} {state}")
