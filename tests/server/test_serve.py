"""End-to-end tests for `dtc serve`'s process lifecycle: it actually binds, answers, and — the regression this
guards against — releases the run lock on a real SIGTERM/SIGINT instead of dying before its cleanup runs (uvicorn's
own signal handling re-raises the caught signal after its internal shutdown, which raced this project's own
cleanup in `server/serve.py` until `QueueHost.stop()` was moved inside the same `capture_signals()` block)."""

from __future__ import annotations

import http.client
import signal
import socket
import subprocess
import sys
import time
import unittest

from draw_things_control.core.errors import InputError
from draw_things_control.server.serve import ServeOptions, _grpc_target
from draw_things_control.server.serve import run as run_server
from draw_things_control.services.toolkit import Toolkit
from tests.fixtures import JobTestCase

_ENTRY_POINT = """
from pathlib import Path
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.global_config import load_global_config
from draw_things_control.services.toolkit import Toolkit
from draw_things_control.server.serve import run, ServeOptions

paths = ProjectPaths(Path({root!r}))
settings = load_global_config(paths.global_config)
run(paths, settings, Toolkit(), ServeOptions(host="127.0.0.1", port={port}, grpc_port={grpc_port}))
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
            connection.request("GET", "/v1/health")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


class ServeProcessTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.paths.global_config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.global_config.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n", encoding="utf-8")
        self.port = _free_port()
        self.grpc_port = _free_port()
        self.process = subprocess.Popen([sys.executable, "-c", _ENTRY_POINT.format(root=str(self.root), port=self.port, grpc_port=self.grpc_port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self._ensure_stopped)

    def _ensure_stopped(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()

    def stop_and_wait(self, sig: signal.Signals) -> None:
        self.process.send_signal(sig)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.fail(f"dtc serve did not exit within 10s of {sig.name}; output:\n{self.process.stdout.read() if self.process.stdout else ''}")

    def test_the_server_starts_and_answers_health(self) -> None:
        self.assertTrue(_wait_for_health(self.port, timeout=10.0), "server never answered /v1/health")
        self.stop_and_wait(signal.SIGTERM)

    def test_sigterm_releases_the_run_lock(self) -> None:
        self.assertTrue(_wait_for_health(self.port, timeout=10.0))
        self.stop_and_wait(signal.SIGTERM)
        lock = self.paths.state / "run.lock"
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(encoding="utf-8"), "", "the run lock was not released: SIGTERM killed the process before cleanup ran")

    def test_sigint_also_releases_the_run_lock(self) -> None:
        self.assertTrue(_wait_for_health(self.port, timeout=10.0))
        self.stop_and_wait(signal.SIGINT)
        lock = self.paths.state / "run.lock"
        self.assertEqual(lock.read_text(encoding="utf-8"), "")


class HostValidationTests(JobTestCase):
    """`run()` refuses a non-loopback host before anything starts (no process, no port, nothing to clean up)."""

    def test_a_non_loopback_host_is_refused_without_allow_remote_bind(self) -> None:
        with self.assertRaisesRegex(InputError, "not loopback"):
            run_server(self.paths, self.global_config, Toolkit(), ServeOptions(host="192.168.1.5"))


class GrpcTargetTests(unittest.TestCase):
    """An IPv6 host must be bracketed for ``grpc.aio.Server.add_insecure_port``: unbracketed, ``::1:8766`` reads as
    three colon-separated fields, not one host and one port."""

    def test_an_ipv4_host_is_not_bracketed(self) -> None:
        self.assertEqual(_grpc_target("127.0.0.1", 8766), "127.0.0.1:8766")

    def test_a_hostname_is_not_bracketed(self) -> None:
        self.assertEqual(_grpc_target("localhost", 8766), "localhost:8766")

    def test_an_ipv6_host_is_bracketed(self) -> None:
        self.assertEqual(_grpc_target("::1", 8766), "[::1]:8766")

    def test_an_ipv6_wildcard_host_is_bracketed(self) -> None:
        self.assertEqual(_grpc_target("::", 8766), "[::]:8766")


if __name__ == "__main__":
    unittest.main()
