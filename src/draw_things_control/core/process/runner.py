"""Supervised execution of Draw Things CLI commands."""

from __future__ import annotations

import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TextIO, TypeVar

from loguru import logger

from draw_things_control.core.arguments import CommandArguments, DrawThingsGenerateArguments
from draw_things_control.core.process.groups import process_group_alive, send_to_process_group
from draw_things_control.core.process.output import MessageCallback, OutputProcessor, OutputStream, ProcessMessage
from draw_things_control.core.process.signals import install_signal_handlers, restore_signal_handlers


class RunResult(Protocol):
    """The process outcome needed by the generation use case."""

    @property
    def return_code(self) -> int: ...

    @property
    def timed_out(self) -> bool: ...

    @property
    def termination_signal(self) -> signal.Signals | None: ...


class Runner(Protocol):
    """A runner that executes one prepared generation request."""

    def run(self) -> RunResult: ...


class StoppableRunner(Runner, Protocol):
    """A runner that can be asked to stop when the job is cancelled or receives a signal."""

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None: ...


# Receives the PID and executable name of a child once it has started, for example RunLock.record_child.
ChildStartCallback = Callable[[int, str], None]


R_co = TypeVar("R_co", bound=Runner, covariant=True)


class RunnerFactory(Protocol[R_co]):
    """Creates the runner for one request; ``on_message`` receives each line the child prints, and ``on_start`` its PID and name; either may be None."""

    def __call__(self, arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, /) -> R_co: ...


@dataclass(frozen=True)
class ProcessResult:
    """The final state of a supervised command."""

    command: tuple[str, ...]
    return_code: int
    elapsed_seconds: float
    termination_signal: signal.Signals | None
    timed_out: bool
    messages: tuple[ProcessMessage, ...]

    @property
    def succeeded(self) -> bool:
        """Whether the command exited successfully and was not interrupted."""
        return self.return_code == 0 and not self.timed_out and self.termination_signal is None


class DrawThingsProcessRunner:
    """Run typed arguments with live output and bounded process-group shutdown."""

    def __init__(
        self,
        arguments: CommandArguments,
        *,
        output_processor: OutputProcessor | None = None,
        shutdown_grace_seconds: float = 10.0,
        timeout_seconds: float | None = None,
        output_drain_seconds: float = 1.0,
        kill_wait_seconds: float = 5.0,
        handle_signals: bool = True,
        capture_output: bool = True,
        on_start: Callable[[int], None] | None = None,
    ) -> None:
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds must not be negative")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if output_drain_seconds < 0:
            raise ValueError("output_drain_seconds must not be negative")
        if kill_wait_seconds < 0:
            raise ValueError("kill_wait_seconds must not be negative")
        self._command = arguments.command
        if not self._command:
            raise ValueError("command must not be empty")
        self._output_processor = output_processor or OutputProcessor()
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._timeout_seconds = timeout_seconds
        self._output_drain_seconds = output_drain_seconds
        self._kill_wait_seconds = kill_wait_seconds
        self._handle_signals = handle_signals
        self._capture_output = capture_output
        # Told the child's PID (its process group id) once it exists, so the run lock can name it.
        self._on_start = on_start
        self._shutdown_requested = threading.Event()
        self._requested_signal: signal.Signals | None = None
        self._timed_out = False
        self._kill_sent_at: float | None = None

    def run(self) -> ProcessResult:
        """Run the command, relaying output until completion or bounded shutdown."""
        started_at = time.monotonic()
        timeout_at = started_at + self._timeout_seconds if self._timeout_seconds is not None else None
        output_queue: queue.Queue[tuple[OutputStream, str]] = queue.Queue()
        # Install handlers before the child exists, so a signal in between cannot orphan it.
        previous_handlers = install_signal_handlers(self.request_shutdown) if self._handle_signals else None
        process: subprocess.Popen[str] | None = None
        readers: tuple[threading.Thread, ...] = ()
        completed = False
        try:
            process = self._start_process()
            logger.info("Started subprocess (PID {})", process.pid)
            self._report_start(process.pid)
            process_group_id = process.pid
            if self._capture_output:
                readers = self._start_readers(process, output_queue)
            shutdown_started_at: float | None = None
            leader_exited_at: float | None = None
            while True:
                now = time.monotonic()
                self._drain_output(output_queue, started_at)
                if timeout_at is not None and now >= timeout_at and not self._shutdown_requested.is_set():
                    self._timed_out = True
                    logger.warning("Generation exceeded the {} second timeout", self._timeout_seconds)
                    self.request_shutdown(signal.SIGTERM)
                if self._shutdown_requested.is_set():
                    shutdown_started_at = self._shutdown_process_group(process_group_id, shutdown_started_at, now)
                    # Reap the leader: an unreaped zombie keeps its group visible on Linux.
                    process.poll()
                    if not process_group_alive(process_group_id, denied_means_alive=False):
                        break
                    if self._kill_sent_at is not None and now - self._kill_sent_at >= self._kill_wait_seconds:
                        logger.warning("Process group {} is still present {} seconds after SIGKILL; giving up", process_group_id, self._kill_wait_seconds)
                        break
                elif process.poll() is not None:
                    if leader_exited_at is None:
                        leader_exited_at = now
                    if not any(reader.is_alive() for reader in readers) and output_queue.empty():
                        break
                    # Descendants may retain inherited pipes after the leader exits.
                    # Do not block indefinitely waiting for them to close the pipes.
                    if now - leader_exited_at >= self._output_drain_seconds:
                        break
                time.sleep(0.02)

            # Let the readers queue the child's last lines before the final drain.
            self._join_readers(readers)
            self._drain_output(output_queue, started_at)
            return_code = process.poll()
            logger.info("Subprocess finished with exit code {}", return_code if return_code is not None else 125)
            result = ProcessResult(
                command=self._command,
                # Do not block on a leader that cannot be reaped because its
                # process group became unavailable.  This preserves the
                # runner's bounded-shutdown contract.
                return_code=return_code if return_code is not None else 125,
                elapsed_seconds=time.monotonic() - started_at,
                termination_signal=self._requested_signal,
                timed_out=self._timed_out,
                messages=self._output_processor.messages,
            )
            completed = True
            return result
        finally:
            if process is not None:
                if not completed:
                    self.request_shutdown()
                self._cleanup_process_group(process.pid, process)
                if not completed:
                    self._join_readers(readers)
            restore_signal_handlers(previous_handlers)

    def _report_start(self, pid: int) -> None:
        if self._on_start is None:
            return
        try:
            self._on_start(pid)
        except Exception:
            logger.exception("Could not report the started subprocess")

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        """Request graceful shutdown programmatically or from a signal handler."""
        self._requested_signal = received_signal
        self._shutdown_requested.set()

    def _shutdown_process_group(
        self,
        process_group_id: int,
        shutdown_started_at: float | None,
        now: float,
    ) -> float:
        if shutdown_started_at is None:
            logger.warning("Sending SIGTERM to process group {}", process_group_id)
            send_to_process_group(process_group_id, signal.SIGTERM)
            return now
        if now - shutdown_started_at >= self._shutdown_grace_seconds and self._kill_sent_at is None:
            self._kill_sent_at = now
            logger.warning("Sending SIGKILL to process group {}", process_group_id)
            send_to_process_group(process_group_id, signal.SIGKILL)
        return shutdown_started_at

    def _join_readers(self, readers: tuple[threading.Thread, ...]) -> None:
        # One deadline for all readers, so waiting stays bounded by the drain time.
        deadline = time.monotonic() + self._output_drain_seconds
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))

    def _cleanup_process_group(self, process_group_id: int, process: subprocess.Popen[str]) -> None:
        if not self._shutdown_requested.is_set() or not process_group_alive(process_group_id, denied_means_alive=False):
            return
        if self._kill_sent_at is not None:
            # The run loop already sent SIGKILL and gave up; do not restart the shutdown.
            process.poll()
            return
        send_to_process_group(process_group_id, signal.SIGTERM)
        deadline = time.monotonic() + self._shutdown_grace_seconds
        while process.poll() is None or process_group_alive(process_group_id, denied_means_alive=False):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if process_group_alive(process_group_id, denied_means_alive=False):
            send_to_process_group(process_group_id, signal.SIGKILL)
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, self._output_drain_seconds))
            except subprocess.TimeoutExpired:
                pass

    def _start_process(self) -> subprocess.Popen[str]:
        # Without capture the child inherits the terminal, so it can preview images inline.
        pipe = subprocess.PIPE if self._capture_output else None
        try:
            return subprocess.Popen(
                self._command,
                stdout=pipe,
                stderr=pipe,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            raise ValueError(f"Could not start executable {self._command[0]}: {error.strerror or error}") from error

    @staticmethod
    def _start_readers(process: subprocess.Popen[str], output_queue: queue.Queue[tuple[OutputStream, str]]) -> tuple[threading.Thread, threading.Thread]:
        def read_stream(stream: TextIO, source: OutputStream) -> None:
            try:
                for line in iter(stream.readline, ""):
                    output_queue.put((source, line))
            finally:
                stream.close()

        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(target=read_stream, args=(process.stdout, OutputStream.STDOUT), daemon=True),
            threading.Thread(target=read_stream, args=(process.stderr, OutputStream.STDERR), daemon=True),
        )
        for reader in readers:
            reader.start()
        return readers

    def _drain_output(self, output_queue: queue.Queue[tuple[OutputStream, str]], started_at: float) -> None:
        while True:
            try:
                stream, text = output_queue.get_nowait()
            except queue.Empty:
                return
            self._output_processor.process(stream, text, time.monotonic() - started_at)
