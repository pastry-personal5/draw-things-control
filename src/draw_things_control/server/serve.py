"""``dtc serve``: runs the HTTP API and the gRPC monitoring service together in one process, sharing the run lock,
the state store, and the queue worker for the process's whole lifetime. Imported only inside the ``dtc serve``
command (``cli/app.py``), as ``dtc tui`` imports Textual only inside its own command."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import grpc
import grpc.aio
import uvicorn
from loguru import logger

from draw_things_control.core.errors import InputError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.server.event_backlog import EventBacklog
from draw_things_control.server.generated import monitor_pb2_grpc
from draw_things_control.server.grpc_auth import TokenAuthInterceptor
from draw_things_control.server.grpc_service import MonitorServicer
from draw_things_control.server.host_check import is_loopback_host
from draw_things_control.server.token_file import load_or_create_token
from draw_things_control.services.queue_host import QueueHost
from draw_things_control.services.toolkit import Toolkit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_GRPC_PORT = 8766
DEFAULT_SHUTDOWN_GRACE = 10.0


@dataclass(frozen=True)
class ServeOptions:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    grpc_port: int = DEFAULT_GRPC_PORT
    executable: str = "draw-things-cli"
    shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE
    allow_remote_bind: bool = False
    allow_write: bool = False


def run(paths: ProjectPaths, global_config: GlobalConfig, toolkit: Toolkit, options: ServeOptions) -> None:
    """Runs `dtc serve` until a signal stops it (uvicorn owns signal handling: the executor is built with
    ``handle_signals=False``). Raises before anything starts: InputError for a non-loopback host without
    ``--allow-remote-bind``; BusyError or StateUnavailableError from ``QueueHost.start()`` (the run lock, an
    orphaned child, or the state store)."""
    if not options.allow_remote_bind and not is_loopback_host(options.host):
        raise InputError(f"--host {options.host} is not loopback; pass --allow-remote-bind to bind beyond it (the token then crosses the network in plain HTTP)")
    if options.allow_remote_bind:
        logger.warning("--allow-remote-bind: the bearer token crosses the network in plain HTTP; prefer an SSH tunnel")
    token = load_or_create_token(paths.server_token)
    backlog = EventBacklog()
    executor = toolkit.job_executor(handle_signals=False)
    host = QueueHost(paths, executor, global_config, executable=options.executable, shutdown_grace=options.shutdown_grace, on_event=backlog.append)
    host.start()
    try:
        assert host.store is not None and host.worker is not None
        context = ServerContext(paths=paths, global_config=global_config, store=host.store, worker=host.worker, executor=executor, executable=options.executable, token=token, bound_host=options.host, bound_port=options.port, allow_write=options.allow_write, event_backlog=backlog)
        logger.info("dtc serve listening on http://{}:{} (gRPC on {}); token file: {}", options.host, options.port, options.grpc_port, paths.server_token)
        asyncio.run(_serve_async(context, options, host))
    finally:
        # A backstop for a non-signal exit (an exception _serve_async's own cleanup did not already handle):
        # QueueHost.stop() is safe to call twice, clearing itself to no-ops the second time.
        host.stop()


async def _serve_async(context: ServerContext, options: ServeOptions, host: QueueHost) -> None:
    """Starts the gRPC service, then blocks on uvicorn (which owns SIGINT/SIGTERM) until it shuts down; the gRPC
    service is then stopped, cancelling any open watch streams, and the worker stopped and the run lock released
    (``host.stop()``), before this returns.

    uvicorn's own signal handlers are installed by ``Server.capture_signals()``, which ``Server.serve()`` normally
    enters itself, and which re-raises the caught signal (its own default disposition restored) once its ``with``
    block exits, after this function returns to ``run()``. That means anything ``run()`` still meant to do in its
    own ``finally`` after ``asyncio.run()`` returns (releasing the run lock) would race that re-raised signal, which
    can kill the process before it runs; ``host.stop()`` is therefore called here, inside this ``with`` block,
    where it is guaranteed to complete first. Starting ``grpc.aio``'s server *before* ``capture_signals()`` is
    entered has a separate, sharper failure: SIGTERM keeps its default, immediate-kill disposition throughout
    (observed empirically: grpc's C core appears to reset it during its own startup), so no cleanup runs at all.
    Entering ``capture_signals()`` first, then starting grpc inside it, then calling the private ``Server._serve()``
    (skipping ``serve()``'s own nested ``capture_signals()``, already held) avoids that.
    """
    uvicorn_server = uvicorn.Server(uvicorn.Config(create_app(context), host=options.host, port=options.port, log_config=None))
    with uvicorn_server.capture_signals():
        grpc_server = grpc.aio.server(interceptors=[TokenAuthInterceptor(context.token)])
        monitor_pb2_grpc.add_MonitorServicer_to_server(MonitorServicer(context), grpc_server)
        grpc_server.add_insecure_port(f"{options.host}:{options.grpc_port}")
        await grpc_server.start()
        try:
            await uvicorn_server._serve()
        finally:
            # Cancels open watch streams at once (grace=0), before the worker stops, so neither can hold shutdown up.
            await grpc_server.stop(grace=0)
            host.stop()
