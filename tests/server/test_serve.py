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
from draw_things_control.server.serve import ServeOptions, _bind_http_socket
from draw_things_control.server.serve import run as run_server
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.services.toolkit import Toolkit
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data

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

    def test_restarting_immediately_on_the_same_port_still_binds(self) -> None:
        """``SO_REUSEADDR`` (``serve.py``'s own ``_bind_http_socket``): a first, simpler version of the HTTP bind
        fix skipped it, so a restart right after stopping could fail with 'address already in use' while the old
        socket's connection still sat in ``TIME_WAIT`` -- a race no test using a fresh port each time, as every
        other test here does, would ever exercise."""
        self.assertTrue(_wait_for_health(self.port, timeout=10.0))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2.0)
        connection.request("GET", "/v1/health")
        connection.getresponse().read()
        connection.close()
        self.stop_and_wait(signal.SIGTERM)
        restarted = subprocess.Popen([sys.executable, "-c", _ENTRY_POINT.format(root=str(self.root), port=self.port, grpc_port=self.grpc_port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            healthy = _wait_for_health(self.port, timeout=10.0)
            if not healthy and restarted.poll() is not None and restarted.stdout is not None:
                self.fail(f"the restart never answered /v1/health; output:\n{restarted.stdout.read()}")
            self.assertTrue(healthy, "the restart never answered /v1/health")
        finally:
            if restarted.poll() is None:
                restarted.send_signal(signal.SIGTERM)
                try:
                    restarted.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    restarted.kill()
                    restarted.wait(timeout=5)
            if restarted.stdout is not None:
                restarted.stdout.close()


class HostValidationTests(JobTestCase):
    """`run()` refuses a non-loopback host before anything starts (no process, no port, nothing to clean up)."""

    def test_a_non_loopback_host_is_refused_without_allow_remote_bind(self) -> None:
        with self.assertRaisesRegex(InputError, "not loopback"):
            run_server(self.paths, self.global_config, Toolkit(), ServeOptions(host="192.168.1.5"))


class BindFailureTests(JobTestCase):
    """The startup-order fix (Milestone 02, phase-3 changelog 2026-09-28): both ports are bound, inside
    ``_serve_async``, before ``host.start_worker()`` runs, so a taken port raises ``InputError`` before the worker's
    thread ever starts, instead of it claiming a queued entry that the late bind failure then tears down as
    ``interrupted``."""

    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        submit_job(path, self.global_config, self.params, store)
        store.close()

    def assert_queued_entry_untouched(self) -> None:
        store = Store.open(self.paths.database, mode=StoreMode.BROWSE)
        (entry,) = store.queue.list()
        store.close()
        self.assertEqual(entry.state, str(QueueState.QUEUED))

    def test_a_taken_http_port_is_refused_before_the_worker_starts(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            taken_port = blocker.getsockname()[1]
            with self.assertRaisesRegex(InputError, "already in use"):
                run_server(self.paths, self.global_config, Toolkit(), ServeOptions(host="127.0.0.1", port=taken_port, grpc_port=_free_port()))
        self.assert_queued_entry_untouched()

    def test_a_taken_grpc_port_is_refused_before_the_worker_starts(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            taken_port = blocker.getsockname()[1]
            with self.assertRaisesRegex(InputError, "already in use"):
                run_server(self.paths, self.global_config, Toolkit(), ServeOptions(host="127.0.0.1", port=_free_port(), grpc_port=taken_port))
        self.assert_queued_entry_untouched()


class BindHttpSocketTests(unittest.TestCase):
    """``_bind_http_socket`` mirrors what asyncio's own ``loop.create_server`` does from a host and a port alone, so
    a first, simpler version (a single plain ``socket.bind``) is not repeated: ``SO_REUSEADDR`` (a restart right
    after stopping should not fail while the old connection sits in ``TIME_WAIT``) and every address ``getaddrinfo``
    resolves the host to (``--host localhost`` can mean both an IPv4 and an IPv6 socket)."""

    def test_so_reuseaddr_is_set_on_every_socket(self) -> None:
        sockets = _bind_http_socket("127.0.0.1", 0)
        try:
            for sock in sockets:
                self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
        finally:
            for sock in sockets:
                sock.close()

    def test_a_taken_port_is_refused_as_an_input_error(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            taken_port = blocker.getsockname()[1]
            with self.assertRaisesRegex(InputError, "already in use"):
                _bind_http_socket("127.0.0.1", taken_port)

    def test_localhost_binds_at_least_one_resolved_address(self) -> None:
        # Not necessarily both IPv4 and IPv6: some sandboxes (this project's CI included) disable one family.
        sockets = _bind_http_socket("localhost", 0)
        try:
            self.assertGreaterEqual(len(sockets), 1)
        finally:
            for sock in sockets:
                sock.close()


if __name__ == "__main__":
    unittest.main()
