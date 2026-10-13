"""Signals that stop a job: routing them to a handler, and a wait that they end at once."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

HANDLED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
SignalHandlers = dict[int, Any]


def install_signal_handlers(on_signal: Callable[[signal.Signals], None]) -> SignalHandlers | None:
    """Route HANDLED_SIGNALS to ``on_signal``; return the previous handlers, or None off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return None
    previous_handlers: SignalHandlers = {number: signal.getsignal(number) for number in HANDLED_SIGNALS}

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
    previous_fd: int | None = None
    try:
        os.set_blocking(read_end, False)
        os.set_blocking(write_end, False)
        if wake_on_signal:
            previous_fd = _register_wakeup(write_end)
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
        if previous_fd is not None:
            signal.set_wakeup_fd(previous_fd)
        os.close(read_end)
        os.close(write_end)
    return time.monotonic() - started


def _register_wakeup(write_end: int) -> int | None:
    """Have Python's C-level signal handler write a byte to ``write_end``; the previous wake-up fd, or None off the main thread."""
    try:
        return signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)
    except ValueError:
        # Off the main thread no handler runs, so no signal can end the wait.
        return None


def _drain(read_end: int) -> None:
    """Empty a non-blocking wake-up pipe."""
    try:
        while os.read(read_end, 512):
            pass
    except BlockingIOError:
        pass


class _Stoppable(Protocol):
    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None: ...


class CancelToken:
    """Whether a running job was told to stop, and the way to tell it: ``cancel()`` from any thread, or a signal handler.

    A stop ends the current run, through its runner, and any wait between runs, through a wake-up pipe. The token belongs
    to one job at a time (``begin`` and ``end``). A signal handler takes no lock: it only sets a flag and asks the runner
    to stop, since a lock taken from a handler on the main thread could deadlock.

    A park (Milestone 05) is kept apart from a stop: it never reaches the runner, and ``requested`` never reads it, so a
    run finishes, its last frame included, as it would without one. It ends a wait between runs, and the executor
    commits to it at a run boundary with ``take_park()``; until then ``unpark()`` withdraws it.
    """

    def __init__(self, *, handle_signals: bool) -> None:
        self._handle_signals = handle_signals
        # Guards the running flag, the park flags, and the wake-up pipe, so cancel() or park() from another thread never
        # writes to a closed or reused descriptor.
        self._lock = threading.Lock()
        self._running = False
        self._signal: signal.Signals | None = None
        self._park = False
        # Set by take_park(): the park has taken effect, and unpark() is refused. Cleared by begin() only, so a
        # caller that asks after the job has ended still hears that its park took effect.
        self._park_taken = False
        # How many parks this job was asked for; see park_count.
        self._park_count = 0
        self._runner: _Stoppable | None = None
        self._wake_read: int | None = None
        self._wake_write: int | None = None

    @property
    def requested(self) -> signal.Signals | None:
        """The signal the stop was requested with, or None."""
        return self._signal

    @property
    def park_count(self) -> int:
        """How many parks this job was asked for (``begin`` resets it): how the executor tells a wait that a park
        ended, and that was then withdrawn, from one that ended early for its own reason."""
        with self._lock:
            return self._park_count

    def begin(self) -> None:
        """Start a job: no stop is requested yet. Raises RuntimeError when one is already running."""
        with self._lock:
            if self._running:
                raise RuntimeError("This JobExecutor is already running a job")
            read_end, write_end = os.pipe()
            os.set_blocking(read_end, False)
            os.set_blocking(write_end, False)
            self._wake_read, self._wake_write = read_end, write_end
            self._running = True
            self._signal = None
            self._park = self._park_taken = False
            self._park_count = 0

    def end(self) -> None:
        with self._lock:
            self._running = False
            for descriptor in (self._wake_read, self._wake_write):
                if descriptor is not None:
                    os.close(descriptor)
            self._wake_read = self._wake_write = None

    def cancel(self, received_signal: signal.Signals = signal.SIGTERM) -> bool:
        """Stop the running job as ``received_signal`` would; False, and nothing done, when no job is running."""
        with self._lock:
            if not self._running:
                return False
            self._signal = received_signal
            self._wake()
            runner = self._runner
        # The flag is set before the runner is read; attach() stores the runner before it reads the flag, so a cancel landing between the two still reaches the runner.
        if runner is not None:
            runner.request_shutdown(received_signal)
        return True

    def park(self) -> bool:
        """Ask the running job to end once its current run finishes; False, and nothing done, when no job is running.
        It never asks the runner to stop; a wait between runs ends at once."""
        with self._lock:
            if not self._running:
                return False
            self._park = True
            self._park_count += 1
            self._wake()
            return True

    def unpark(self, commit: Callable[[], object] | None = None) -> bool:
        """Withdraw a park; False only once it has taken effect (``take_park``), when the job ends parked anyway.
        ``commit`` runs under the lock, so no ``take_park`` can pass between it and the withdrawal: if it raises, the
        park stands as it was, and the error propagates."""
        with self._lock:
            if self._park_taken:
                return False
            if commit is not None:
                commit()
            self._park = False
            return True

    def take_park(self) -> bool:
        """The executor's check-and-commit at a run boundary: True, and the park has taken effect, when one is requested."""
        with self._lock:
            if self._park:
                self._park_taken = True
            return self._park_taken

    def _wake(self) -> None:
        """End a wait between runs; call with the lock held."""
        if self._wake_write is not None:
            # A full pipe already holds a byte, and one is enough.
            with contextlib.suppress(OSError):
                os.write(self._wake_write, b"\0")

    def receive(self, received_signal: signal.Signals) -> None:
        """The handler for the signals that stop a job (see ``install_signal_handlers``)."""
        self._signal = received_signal
        if self._runner is not None:
            self._runner.request_shutdown(received_signal)

    def attach(self, runner: _Stoppable) -> None:
        """Note the current run's runner, and stop it at once when a stop was already requested."""
        self._runner = runner
        if self._signal is not None:
            runner.request_shutdown(self._signal)

    def detach(self) -> None:
        self._runner = None

    def wait(self, seconds: float) -> float:
        """Wait up to ``seconds`` between runs; a stop or a park ends the wait at once. Returns the seconds waited."""
        # The park flag is read without the lock: a bool read is atomic, and park() sets it before writing the byte.
        return interruptible_wait(seconds, lambda: self._signal is not None or self._park, wake_on_signal=self._handle_signals, wake_fd=self._wake_read)
