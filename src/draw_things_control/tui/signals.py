"""The signals that would otherwise kill dtc tui outright, leaving the terminal in its alternate screen."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable

from draw_things_control.core.process.signals import HANDLED_SIGNALS


class SignalGuard:
    """Routes SIGHUP, SIGINT, and SIGTERM to the app's own ``exit()``, for a clean shutdown that restores the
    terminal -- the raw signal's own default disposition does not. Milestone 03: nothing here coordinates with a
    running job any more, since ``dtc serve`` is the only thing that ever runs one, and quitting the TUI does not
    touch it; the old install/remove/ignore-until-removed dance existed only to stop a child this process ran
    itself, which no longer happens."""

    def __init__(self, on_signal: Callable[[signal.Signals], None]) -> None:
        self._on_signal = on_signal

    def install(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for received in HANDLED_SIGNALS:
                loop.add_signal_handler(received, self._on_signal, received)
        except (NotImplementedError, RuntimeError, ValueError):
            # Not on the main thread, or no signals on this platform: nothing to register.
            pass


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
