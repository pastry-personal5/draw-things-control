"""``dtc serve``: runs the HTTP API and the gRPC monitoring service together in one process, sharing the run lock,
the state store, and the queue worker for the process's whole lifetime. Imported only inside the ``dtc serve``
command (``cli/app.py``), as ``dtc tui`` imports Textual only inside its own command."""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import sys
from dataclasses import dataclass

import grpc
import grpc.aio
import uvicorn
from loguru import logger

from draw_things_control.core.errors import InputError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.network import grpc_target
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.server.event_backlog import EventBacklog
from draw_things_control.server.generated import monitor_pb2_grpc
from draw_things_control.server.grpc_auth import TokenAuthInterceptor
from draw_things_control.server.grpc_service import MonitorServicer
from draw_things_control.server.host_check import is_loopback_host
from draw_things_control.server.token_file import load_or_create_token
from draw_things_control.services.queue_hold import hold_text
from draw_things_control.services.queue_host import QueueHost
from draw_things_control.services.toolkit import Toolkit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_GRPC_PORT = 8766
DEFAULT_SHUTDOWN_GRACE = 10.0


def _bind_http_socket(host: str, port: int) -> list[socket.socket]:
    """Bind the HTTP port ourselves, exactly as asyncio's own ``loop.create_server`` would from ``host`` and
    ``port`` alone (its ``sock=`` path skips all of this, which is why a plain, single ``socket.bind`` here once
    missed it): every address ``getaddrinfo`` resolves ``host`` to (``--host localhost`` can mean both an IPv4 and
    an IPv6 socket). This makes a port already in use fail the same clean way (``InputError``, exit 2) the gRPC
    port already does, instead of uvicorn's own bind failure (``sys.exit(1)``, no error code). Handed to uvicorn's
    private ``Server._serve(sockets=...)``, which then owns them: it calls ``listen()`` itself and closes them at
    shutdown (``Server.shutdown(sockets=...)``); nothing here does either."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except OSError as error:
        raise InputError(f"Cannot resolve {host}:{port} for the HTTP service: {error}", field="port") from error
    reuse_address = os.name == "posix" and sys.platform != "cygwin"
    sockets: list[socket.socket] = []
    completed = False
    try:
        for family, socktype, proto, _canonname, sockaddr in infos:
            sock = _bind_one(family, socktype, proto, sockaddr, reuse_address, host, port)
            if sock is not None:
                sockets.append(sock)
        if not sockets:
            raise InputError(f"Cannot bind the HTTP service to {host}:{port}: no address could be bound", field="port")
        completed = True
    finally:
        if not completed:
            for sock in sockets:
                sock.close()
    return sockets


def _bind_one(family: socket.AddressFamily, socktype: socket.SocketKind, proto: int, sockaddr: tuple[object, ...], reuse_address: bool, host: str, port: int) -> socket.socket | None:
    """Bind one address from ``_bind_http_socket``'s own ``getaddrinfo``: ``SO_REUSEADDR`` on POSIX (without it, a
    quick restart on the same port fails while the old socket sits in ``TIME_WAIT``, which none of this project's
    own tests hit, since each uses a fresh port), and ``IPV6_V6ONLY`` on an IPv6 one. None when this family just
    isn't enabled on the machine (as asyncio's own ``create_server`` tolerates): the caller skips it, not the whole
    bind, so long as some other address still binds."""
    sock = socket.socket(family, socktype, proto)
    if reuse_address:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
    if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, True)
    try:
        sock.bind(sockaddr)
    except OSError as error:
        sock.close()
        if error.errno == errno.EADDRNOTAVAIL:
            return None
        raise InputError(f"Cannot bind the HTTP service to {host}:{port}; is --port {port} already in use?", field="port") from error
    return sock


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
    orphaned child, or the state store). The worker itself does not start here: ``host.start(start_worker=False)``
    only takes the lock and recovers; ``_serve_async`` starts it once both ports are bound, so a taken port fails
    cleanly instead of a queued entry being claimed and then interrupted by the refusal."""
    if not options.allow_remote_bind and not is_loopback_host(options.host):
        raise InputError(f"--host {options.host} is not loopback; pass --allow-remote-bind to bind beyond it (the token then crosses the network in plain HTTP)")
    if options.allow_remote_bind:
        logger.warning("--allow-remote-bind: the bearer token crosses the network in plain HTTP; prefer an SSH tunnel")
    token = load_or_create_token(paths.server_token)
    backlog = EventBacklog()
    executor = toolkit.job_executor(handle_signals=False)
    host = QueueHost(paths, executor, global_config, executable=options.executable, shutdown_grace=options.shutdown_grace, on_event=backlog.append)
    host.start(start_worker=False)
    try:
        assert host.store is not None and host.worker is not None
        context = ServerContext(paths=paths, global_config=global_config, store=host.store, worker=host.worker, executor=executor, executable=options.executable, token=token, bound_host=options.host, bound_port=options.port, grpc_port=options.grpc_port, allow_write=options.allow_write, event_backlog=backlog)
        logger.info("dtc serve listening on http://{}:{} (gRPC on {}); token file: {}", options.host, options.port, options.grpc_port, paths.server_token)
        hold = host.worker.hold_state()
        if hold.held:
            logger.info("{}; 'dtc queue release' starts it", hold_text(hold))
        asyncio.run(_serve_async(context, options, host))
    finally:
        # A backstop for a non-signal exit (an exception _serve_async's own cleanup did not already handle):
        # QueueHost.stop() is safe to call twice, clearing itself to no-ops the second time.
        host.stop()


def _bind_grpc_server(context: ServerContext, options: ServeOptions) -> grpc.aio.Server:
    """Builds the gRPC server and binds it to ``options.grpc_port`` (0 asks the OS for an ephemeral one), then sets
    ``context.grpc_port`` to the port actually bound -- ``add_insecure_port``'s own return value, which differs from
    the request when it was 0. ``context.grpc_port`` is what ``/v1/health`` reports, so every client that derives
    its gRPC target from health (``tui/feed.py``, ``cli/queue_wait.py``) must see the real bound port, not the
    request that asked for "any"."""
    grpc_server = grpc.aio.server(interceptors=[TokenAuthInterceptor(context.token)])
    monitor_pb2_grpc.add_MonitorServicer_to_server(MonitorServicer(context), grpc_server)
    target = grpc_target(options.host, options.grpc_port)
    try:
        context.grpc_port = grpc_server.add_insecure_port(target)
    except RuntimeError as error:
        # grpc raises a bare RuntimeError (no reason given) when the port is taken or the address unusable;
        # run()'s own finally stops the host, as for any other exception before a signal.
        raise InputError(f"Cannot bind the gRPC service to {target}; is --grpc-port {options.grpc_port} already in use?", field="grpc_port") from error
    return grpc_server


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

    Both ports are bound here, before ``host.start_worker()``: binding first means a taken port (gRPC or HTTP)
    raises a plain ``InputError`` with nothing yet running, rather than the worker having already claimed a queued
    entry that a late bind failure then interrupts (Milestone 02's startup-order fix, phase-3 changelog 2026-09-28).
    """
    uvicorn_server = uvicorn.Server(uvicorn.Config(create_app(context), host=options.host, port=options.port, log_config=None))
    with uvicorn_server.capture_signals():
        grpc_server = _bind_grpc_server(context, options)
        http_sockets = _bind_http_socket(options.host, options.port)
        host.start_worker()
        await grpc_server.start()
        try:
            await uvicorn_server._serve(sockets=http_sockets)
        finally:
            # Cancels open watch streams at once (grace=0), before the worker stops, so neither can hold shutdown up.
            await grpc_server.stop(grace=0)
            host.stop()
