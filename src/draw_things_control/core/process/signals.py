"""Signals that stop a job: routing them to a handler, and a wait that they end at once."""

from __future__ import annotations

import os
import select
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

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
