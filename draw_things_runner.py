"""Supervised execution of Draw Things CLI commands."""

from __future__ import annotations

import errno
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TextIO

from loguru import logger

from draw_things_arguments import CommandArguments
from process_output import OutputProcessor, OutputStream, ProcessMessage


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
        handle_signals: bool = True,
    ) -> None:
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds must not be negative")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if output_drain_seconds < 0:
            raise ValueError("output_drain_seconds must not be negative")
        self._command = arguments.command
        if not self._command:
            raise ValueError("command must not be empty")
        self._output_processor = output_processor or OutputProcessor()
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._timeout_seconds = timeout_seconds
        self._output_drain_seconds = output_drain_seconds
        self._handle_signals = handle_signals
        self._shutdown_requested = threading.Event()
        self._requested_signal: signal.Signals | None = None
        self._timed_out = False
        self._kill_sent = False

    def run(self) -> ProcessResult:
        """Run the command, relaying output until completion or bounded shutdown."""
        started_at = time.monotonic()
        timeout_at = started_at + self._timeout_seconds if self._timeout_seconds is not None else None
        process = self._start_process()
        logger.info("Started subprocess (PID {})", process.pid)
        process_group_id = process.pid
        output_queue: queue.Queue[tuple[OutputStream, str]] = queue.Queue()
        readers = self._start_readers(process, output_queue)
        previous_handlers = self._install_signal_handlers()
        shutdown_started_at: float | None = None
        leader_exited_at: float | None = None
        completed = False
        try:
            while True:
                now = time.monotonic()
                self._drain_output(output_queue, started_at)
                if timeout_at is not None and now >= timeout_at and not self._shutdown_requested.is_set():
                    self._timed_out = True
                    logger.warning("Generation exceeded the {} second timeout", self._timeout_seconds)
                    self.request_shutdown(signal.SIGTERM)
                if self._shutdown_requested.is_set():
                    shutdown_started_at = self._shutdown_process_group(process_group_id, shutdown_started_at, now)
                    if not self._is_process_group_alive(process_group_id):
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
            if not completed:
                self.request_shutdown()
            self._cleanup_process_group(process_group_id, process)
            for reader in readers:
                reader.join(timeout=self._output_drain_seconds)
            self._restore_signal_handlers(previous_handlers)

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
        if now - shutdown_started_at >= self._shutdown_grace_seconds and not self._kill_sent:
            self._kill_sent = True
            logger.warning("Sending SIGKILL to process group {}", process_group_id)
            self._send_to_process_group(process_group_id, signal.SIGKILL)
        return shutdown_started_at

    def _cleanup_process_group(self, process_group_id: int, process: subprocess.Popen[str]) -> None:
        if not self._shutdown_requested.is_set() or not self._is_process_group_alive(process_group_id):
            return
        self._send_to_process_group(process_group_id, signal.SIGTERM)
        deadline = time.monotonic() + self._shutdown_grace_seconds
        while self._is_process_group_alive(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._is_process_group_alive(process_group_id):
            self._send_to_process_group(process_group_id, signal.SIGKILL)
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, self._output_drain_seconds))
            except subprocess.TimeoutExpired:
                pass

    def _start_process(self) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise ValueError(f"Could not start executable: {self._command[0]}") from error

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

    def _install_signal_handlers(self) -> dict[int, signal.Handlers] | None:
        if not self._handle_signals or threading.current_thread() is not threading.main_thread():
            return None
        handled_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        previous_handlers = {number: signal.getsignal(number) for number in handled_signals}

        def handle_signal(signum: int, _frame: object) -> None:
            self.request_shutdown(signal.Signals(signum))

        for handled_signal in handled_signals:
            signal.signal(handled_signal, handle_signal)
        return previous_handlers

    @staticmethod
    def _restore_signal_handlers(previous_handlers: dict[int, signal.Handlers] | None) -> None:
        if previous_handlers is not None:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

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
