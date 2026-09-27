"""The signals that would otherwise kill dtc and leave draw-things-cli running, handled on the app's event loop."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from collections.abc import Callable
from typing import Any

from draw_things_control.core.process.signals import HANDLED_SIGNALS


class SignalGuard:
    """Routes SIGHUP, SIGINT, and SIGTERM to the app's handler while it runs, and puts the old handlers back after."""

    def __init__(self, on_signal: Callable[[signal.Signals], None]) -> None:
        self._on_signal = on_signal
        self._previous: dict[signal.Signals, Any] = {}

    def install(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for received in HANDLED_SIGNALS:
                self._previous[received] = signal.getsignal(received)
                loop.add_signal_handler(received, self._on_signal, received)
        except (NotImplementedError, RuntimeError, ValueError):
            # Not on the main thread, or no signals on this platform: nothing to register.
            pass

    def remove(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for received, previous in self._previous.items():
            loop.remove_signal_handler(received)
            if previous is not None:
                signal.signal(received, previous)
        self._previous.clear()

    def ignore_until_removed(self) -> None:
        """After the app has gone, while its job stops: a second signal must not kill dtc and leave draw-things-cli running."""
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            for received in self._previous:
                loop.add_signal_handler(received, self.ignore, received)

    def ignore(self, received: signal.Signals) -> None:
        """A signal with nothing left to do: the job is already stopping."""


class QuitPress:
    """A first Ctrl-C arms the quit for a few seconds; the second, while armed, quits."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        # Monotonic time a first Ctrl-C stops waiting for the second.
        self._until = 0.0

    @property
    def armed(self) -> bool:
        return time.monotonic() < self._until

    def arm(self) -> None:
        self._until = time.monotonic() + self._seconds

    def disarm(self) -> None:
        self._until = 0.0
