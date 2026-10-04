"""What the queue worker is doing right now, for ``GET /queue``, ``GET /queue/{id}``, and ``WatchQueueEntry``
(Milestone 02): idle, running, or cooling down between jobs; the current run and when it started; and when the
between-jobs wait ends. Kept separate from ``QueueWorker``'s own claim and cancel bookkeeping, with its own lock, so
that class's body stays under the project's size limit."""

from __future__ import annotations

import threading
from datetime import datetime

from draw_things_control.core.clock import Clock
from draw_things_control.jobs.events import CooldownStarted, JobEvent, RunFinished, RunOutput, RunStarted, RunStatus


class WorkerStatus:
    def __init__(self, *, clock: Clock = datetime.now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self._cooldown_until: float | None = None
        self._current_run: int | None = None
        self._current_run_started_epoch: float | None = None
        self._current_step: tuple[int, int] | None = None
        # The succeeded run the claimed entry is between runs after, until its next run starts, a cooldown between the
        # two included (Milestone 05); apart from _cooldown_until, the wait between two queued jobs, None while a job runs.
        self._between_runs_after: int | None = None

    def entry_claimed(self) -> None:
        with self._lock:
            self._running = True

    def entry_released(self) -> None:
        with self._lock:
            self._running, self._current_run, self._current_run_started_epoch, self._current_step = False, None, None, None
            self._between_runs_after = None

    def cooldown_started(self, seconds: float) -> None:
        with self._lock:
            self._cooldown_until = self._clock().timestamp() + seconds

    def cooldown_ended(self) -> None:
        with self._lock:
            self._cooldown_until = None

    def observe_run(self, event: JobEvent) -> None:
        """A job observer: tracks the claimed entry's current run number and when it started, for ``current_run()``,
        and its latest progress-bar reading, for ``current_step()``. A new run starts with no reading of its own
        (the previous run's last step would otherwise read as this run's, until output for it arrives)."""
        if isinstance(event, RunStarted):
            with self._lock:
                self._current_run, self._current_run_started_epoch, self._current_step = event.number, self._clock().timestamp(), None
                self._between_runs_after = None
        elif isinstance(event, RunOutput):
            if event.progress is not None:
                with self._lock:
                    self._current_step = event.progress
        elif isinstance(event, RunFinished):
            with self._lock:
                self._current_run, self._current_run_started_epoch, self._current_step = None, None, None
                self._between_runs_after = event.number if event.status == RunStatus.SUCCEEDED else None
        elif isinstance(event, CooldownStarted):
            # Kept past CooldownEnded: a park that ends the cooldown ends it before the entry is marked parked, and a
            # front end reading the entry in between still needs the run.
            with self._lock:
                self._between_runs_after = event.after_run

    def generation_started(self) -> None:
        """Mark the one run of a Milestone 12 entry live; it has no job events to observe."""
        with self._lock:
            self._current_run, self._current_run_started_epoch, self._current_step = 1, self._clock().timestamp(), None

    def generation_finished(self) -> None:
        with self._lock:
            self._current_run, self._current_run_started_epoch, self._current_step = None, None, None

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

    def current_step(self) -> tuple[int, int] | None:
        """The active run's latest (step, total) reading from its progress bar, or None before the first reading
        arrives, between runs, or when nothing is claimed."""
        with self._lock:
            return self._current_step

    def between_runs_after(self) -> int | None:
        """The succeeded run the claimed entry is between runs after, a cooldown included, until its next run starts;
        None while a run is going, before the first, or when nothing is claimed (Milestone 05: a front end words a
        park's outcome from it, since a park between two runs takes effect after that run)."""
        with self._lock:
            return self._between_runs_after
