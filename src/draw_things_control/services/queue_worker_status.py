"""What the queue worker is doing right now, for ``GET /queue``, ``GET /queue/{id}``, and ``WatchQueueEntry``
(Milestone 02): idle, running, or cooling down between jobs; the current run and when it started; and when the
between-jobs wait ends. Kept separate from ``QueueWorker``'s own claim and cancel bookkeeping, with its own lock, so
that class's body stays under the project's size limit."""

from __future__ import annotations

import threading
from datetime import datetime

from draw_things_control.core.clock import Clock
from draw_things_control.jobs.events import JobEvent, RunFinished, RunStarted


class WorkerStatus:
    def __init__(self, *, clock: Clock = datetime.now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self._cooldown_until: float | None = None
        self._current_run: int | None = None
        self._current_run_started_epoch: float | None = None

    def entry_claimed(self) -> None:
        with self._lock:
            self._running = True

    def entry_released(self) -> None:
        with self._lock:
            self._running, self._current_run, self._current_run_started_epoch = False, None, None

    def cooldown_started(self, seconds: float) -> None:
        with self._lock:
            self._cooldown_until = self._clock().timestamp() + seconds

    def cooldown_ended(self) -> None:
        with self._lock:
            self._cooldown_until = None

    def observe_run(self, event: JobEvent) -> None:
        """A job observer: tracks the claimed entry's current run number and when it started, for ``current_run()``."""
        if isinstance(event, RunStarted):
            with self._lock:
                self._current_run, self._current_run_started_epoch = event.number, self._clock().timestamp()
        elif isinstance(event, RunFinished):
            with self._lock:
                self._current_run, self._current_run_started_epoch = None, None

    def state(self) -> str:
        """``running``, ``cooling_down``, or ``idle``."""
        with self._lock:
            if self._running:
                return "running"
            return "cooling_down" if self._cooldown_until is not None else "idle"

    def cooldown_until(self) -> float | None:
        """When the between-jobs wait ends (an epoch), or None outside it."""
        with self._lock:
            return self._cooldown_until

    def current_run(self) -> tuple[int, float] | None:
        """The claimed entry's current run number and its elapsed seconds, or None between runs or when nothing is
        claimed."""
        with self._lock:
            if self._current_run is None or self._current_run_started_epoch is None:
                return None
            return self._current_run, self._clock().timestamp() - self._current_run_started_epoch
