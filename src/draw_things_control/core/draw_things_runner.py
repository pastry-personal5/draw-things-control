"""Supervised execution of Draw Things CLI commands."""

from __future__ import annotations

import errno
import os
import queue
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from loguru import logger

from draw_things_control.core.draw_things_arguments import CommandArguments
from draw_things_control.core.process_output import OutputProcessor, OutputStream, ProcessMessage

HANDLED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
SignalHandlers = dict[int, signal.Handlers | int | None]


def install_signal_handlers(on_signal: Callable[[signal.Signals], None]) -> SignalHandlers | None:
    """Route HANDLED_SIGNALS to ``on_signal``; return the previous handlers, or None off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return None
    previous_handlers = {number: signal.getsignal(number) for number in HANDLED_SIGNALS}

    def handle_signal(signum: int, _frame: object) -> None:
        on_signal(signal.Signals(signum))

    for handled_signal in HANDLED_SIGNALS:
        signal.signal(handled_signal, handle_signal)
    return previous_handlers


def restore_signal_handlers(previous_handlers: SignalHandlers | None) -> None:
    """Put back the handlers returned by install_signal_handlers."""
    if previous_handlers is not None:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def interruptible_wait(seconds: float, stopped: Callable[[], bool], *, wake_on_signal: bool = True, wake_fd: int | None = None) -> float:
    """Wait ``seconds``, ending early once ``stopped()`` is true; return the seconds waited.

    ``wake_fd`` is the non-blocking read end of a pipe the caller owns. Another thread ends the wait by
    making ``stopped()`` true and then writing a byte to the pipe; a byte already there ends it at once.

    With ``wake_on_signal``, a wake-up pipe ends the wait as soon as a signal with a Python handler
    arrives (such as those from install_signal_handlers): Python's C-level handler writes a byte to
    it, which wakes ``select``, and the Python handler then makes ``stopped()`` true. The handler must
    only set a flag: a lock or event taken from a handler on the main thread could deadlock.
    """
    started = time.monotonic()
    deadline = started + seconds
    read_end, write_end = os.pipe()
    registered = False
    previous_fd = -1
    try:
        os.set_blocking(read_end, False)
        os.set_blocking(write_end, False)
        if wake_on_signal:
            try:
                previous_fd = signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)
                registered = True
            except ValueError:
                # Off the main thread no handler runs, so no signal can end the wait.
                pass
        # Checked after registering, so a signal arriving now still leaves a byte in the pipe.
        while not stopped():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            watched = [read_end] if wake_fd is None else [read_end, wake_fd]
            readable, _, _ = select.select(watched, [], [], remaining)
            for ready in readable:
                _drain(ready)
    finally:
        if registered:
            signal.set_wakeup_fd(previous_fd)
        os.close(read_end)
        os.close(write_end)
    return time.monotonic() - started


def _drain(read_end: int) -> None:
    """Empty a non-blocking wake-up pipe."""
    try:
        while os.read(read_end, 512):
            pass
    except BlockingIOError:
        pass


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
                    if not self._is_process_group_alive(process_group_id):
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
            self._send_to_process_group(process_group_id, signal.SIGTERM)
            return now
        if now - shutdown_started_at >= self._shutdown_grace_seconds and self._kill_sent_at is None:
            self._kill_sent_at = now
            logger.warning("Sending SIGKILL to process group {}", process_group_id)
            self._send_to_process_group(process_group_id, signal.SIGKILL)
        return shutdown_started_at

    def _join_readers(self, readers: tuple[threading.Thread, ...]) -> None:
        # One deadline for all readers, so waiting stays bounded by the drain time.
        deadline = time.monotonic() + self._output_drain_seconds
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))

    def _cleanup_process_group(self, process_group_id: int, process: subprocess.Popen[str]) -> None:
        if not self._shutdown_requested.is_set() or not self._is_process_group_alive(process_group_id):
            return
        if self._kill_sent_at is not None:
            # The run loop already sent SIGKILL and gave up; do not restart the shutdown.
            process.poll()
            return
        self._send_to_process_group(process_group_id, signal.SIGTERM)
        deadline = time.monotonic() + self._shutdown_grace_seconds
        while process.poll() is None or self._is_process_group_alive(process_group_id):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if self._is_process_group_alive(process_group_id):
            self._send_to_process_group(process_group_id, signal.SIGKILL)
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

    @staticmethod
    def _is_process_group_alive(process_group_id: int) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # We created the group, so an inaccessible group cannot be
            # supervised further. Treat it as unavailable rather than looping
            # forever or risking a signal to a recycled process-group ID.
            return False
        return True

    @staticmethod
    def _send_to_process_group(process_group_id: int, sig: signal.Signals) -> None:
        if os.name == "posix":
            try:
                os.killpg(process_group_id, sig)
            except OSError as error:
                if error.errno not in (errno.ESRCH, errno.EPERM):
                    raise
