"""Supervised execution of process-based backend commands."""

from __future__ import annotations

import codecs
import contextlib
import os
import queue
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any, Protocol, TypeVar

from loguru import logger

from framecontrol.core.arguments import CommandArguments, DrawThingsGenerateArguments
from framecontrol.core.process.groups import process_group_alive, send_to_process_group
from framecontrol.core.process.output import MessageCallback, OutputProcessor, OutputStream, ProcessMessage
from framecontrol.core.process.segments import PartialReading, SegmentSplitter
from framecontrol.core.process.signals import install_signal_handlers, restore_signal_handlers
from framecontrol.core.process.spawn import popen


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
# What a reader thread asks the operating system for at a time.
READ_SIZE = 65536


R_co = TypeVar("R_co", bound=Runner, covariant=True)


class RunnerFactory(Protocol[R_co]):
    """Creates the runner for one request; ``on_message`` receives each line the child prints, and ``on_start`` its PID and name; either may be None."""

    def __call__(self, arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, /) -> R_co: ...


class JobRunnerFactory(Protocol[R_co]):
    """Creates a supervised runner from any backend's prepared command."""

    def __call__(self, arguments: Any, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, /) -> R_co: ...


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
    """Run a prepared command with live output and bounded process-group shutdown.

    Each reader thread reads its pipe as bytes and stamps every segment (a line ended by ``\\n`` or ``\\r``) with the time its
    last byte arrived, so an offset says when the child wrote a line, not when the supervisor next drained the queue. A
    backend whose redraws end with no terminator (H3) offers, in its arguments, ``unterminated_reading``: a predicate
    that says whether text so far already reads as a whole reading. A segment it accepts is released once nothing more
    arrives within ``idle_release_seconds``, with its own stamp."""

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
        idle_release_seconds: float = 0.2,
    ) -> None:
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds must not be negative")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if output_drain_seconds < 0:
            raise ValueError("output_drain_seconds must not be negative")
        if kill_wait_seconds < 0:
            raise ValueError("kill_wait_seconds must not be negative")
        if idle_release_seconds < 0:
            raise ValueError("idle_release_seconds must not be negative")
        self._command = arguments.command
        self._environment = getattr(arguments, "environment", None)
        self._cwd = getattr(arguments, "cwd", None)
        self._partial_reading: PartialReading | None = getattr(arguments, "unterminated_reading", None)
        self._idle_release_seconds = idle_release_seconds
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
        output_queue: queue.Queue[tuple[OutputStream, str, float]] = queue.Queue()
        # Install handlers before the child exists, so a signal in between cannot orphan it.
        previous_handlers = install_signal_handlers(self.request_shutdown) if self._handle_signals else None
        process: subprocess.Popen[bytes] | None = None
        readers: tuple[threading.Thread, ...] = ()
        completed = False
        try:
            process = self._start_process()
            logger.info("Started subprocess (PID {}, output stamped as read)", process.pid)
            self._report_start(process.pid)
            if self._capture_output:
                readers = self._start_readers(process, output_queue)
            self._supervise(process, readers, output_queue, started_at)
            # Let the readers queue the child's last lines before the final drain.
            self._join_readers(readers)
            self._drain_output(output_queue, started_at)
            result = self._result(process, started_at)
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

    def _supervise(self, process: subprocess.Popen[bytes], readers: tuple[threading.Thread, ...], output_queue: queue.Queue[tuple[OutputStream, str, float]], started_at: float) -> None:
        """Relay output until the child is done, or its group is gone, or a shutdown has run its bounded course."""
        process_group_id = process.pid
        timeout_at = started_at + self._timeout_seconds if self._timeout_seconds is not None else None
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
                    return
                if self._kill_sent_at is not None and now - self._kill_sent_at >= self._kill_wait_seconds:
                    logger.warning("Process group {} is still present {} seconds after SIGKILL; giving up", process_group_id, self._kill_wait_seconds)
                    return
            elif process.poll() is not None:
                if leader_exited_at is None:
                    leader_exited_at = now
                if not any(reader.is_alive() for reader in readers) and output_queue.empty():
                    return
                # Descendants may retain inherited pipes after the leader exits.
                # Do not block indefinitely waiting for them to close the pipes.
                if now - leader_exited_at >= self._output_drain_seconds:
                    return
            time.sleep(0.02)

    def _result(self, process: subprocess.Popen[bytes], started_at: float) -> ProcessResult:
        return_code = process.poll()
        logger.info("Subprocess finished with exit code {}", return_code if return_code is not None else 125)
        return ProcessResult(
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

    def _cleanup_process_group(self, process_group_id: int, process: subprocess.Popen[bytes]) -> None:
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
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=max(0.1, self._output_drain_seconds))

    def _start_process(self) -> subprocess.Popen[bytes]:
        # Without capture the child inherits the terminal, so it can preview images inline.
        pipe = subprocess.PIPE if self._capture_output else None
        try:
            return popen(
                self._command,
                stdout=pipe,
                stderr=pipe,
                bufsize=0,
                start_new_session=True,
                env=self._environment,
                cwd=self._cwd,
            )
        except OSError as error:
            raise ValueError(f"Could not start executable {self._command[0]}: {error.strerror or error}") from error

    def _start_readers(self, process: subprocess.Popen[bytes], output_queue: queue.Queue[tuple[OutputStream, str, float]]) -> tuple[threading.Thread, threading.Thread]:
        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(target=self._read_stream, args=(process.stdout, OutputStream.STDOUT, output_queue), daemon=True),
            threading.Thread(target=self._read_stream, args=(process.stderr, OutputStream.STDERR, output_queue), daemon=True),
        )
        for reader in readers:
            reader.start()
        return readers

    def _read_stream(self, stream: IO[bytes], source: OutputStream, output_queue: queue.Queue[tuple[OutputStream, str, float]]) -> None:
        """Queue each segment of ``stream`` with the time its last byte arrived, until the stream ends."""
        # Undecodable bytes become U+FFFD, and a character split across two reads is whole.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        splitter = SegmentSplitter(self._partial_reading, idle_seconds=self._idle_release_seconds)

        def deliver(segments: list[Any]) -> None:
            for segment in segments:
                output_queue.put((source, segment.text, segment.arrived))

        try:
            descriptor = stream.fileno()
            while True:
                deadline = splitter.idle_deadline()
                if deadline is not None:
                    ready, _, _ = select.select([descriptor], [], [], max(0.0, deadline - time.monotonic()))
                    if not ready:
                        deliver(splitter.release_idle(time.monotonic()))
                        continue
                data = os.read(descriptor, READ_SIZE)
                if not data:
                    break
                deliver(splitter.feed(decoder.decode(data), time.monotonic()))
            tail = decoder.decode(b"", final=True)
            if tail:
                deliver(splitter.feed(tail, time.monotonic()))
        except (OSError, ValueError):
            # The pipe was closed under the reader as the run was torn down; what it had read is still delivered.
            pass
        finally:
            deliver(splitter.flush())
            stream.close()

    def _drain_output(self, output_queue: queue.Queue[tuple[OutputStream, str, float]], started_at: float) -> None:
        while True:
            try:
                stream, text, arrived = output_queue.get_nowait()
            except queue.Empty:
                return
            self._output_processor.process(stream, text, max(0.0, arrived - started_at))
