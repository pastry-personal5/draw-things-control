"""Runs the API under uvicorn on a loopback port, in a thread of its own, for what an in-process client cannot test:
``httpx.ASGITransport`` and Starlette's ``TestClient`` collect a whole response before they return it, which an
endless SSE watch never finishes (Milestone 10). The server is ``dtc serve``'s own ``UvicornServer``, so stopping it
ends every open watch as ``dtc serve``'s shutdown does."""

from __future__ import annotations

import socket
import threading
import time

import uvicorn

from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.server.serve import UvicornServer


def loopback_socket() -> socket.socket:
    """A socket bound to an OS-assigned loopback port: its port goes into the context first, which the ``Host`` check
    compares the request's ``Host`` header with."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


def socket_port(sock: socket.socket) -> int:
    return sock.getsockname()[1]


class LiveApp:
    """``create_app(context)`` served on ``sock`` until ``stop()``."""

    def __init__(self, context: ServerContext, sock: socket.socket) -> None:
        self.server = UvicornServer(uvicorn.Config(create_app(context), log_config=None, lifespan="off"), context)
        self.base_url = f"http://127.0.0.1:{socket_port(sock)}"
        self._thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, name="live-app", daemon=True)

    def start(self, timeout: float = 5.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.01)

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask uvicorn to shut down, as a signal does; True once it has, within ``timeout``."""
        self.server.should_exit = True
        self._thread.join(timeout)
        return not self._thread.is_alive()
