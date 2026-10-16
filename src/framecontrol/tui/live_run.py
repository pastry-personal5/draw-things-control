"""The state of the job the TUI follows, built from gRPC events (and, attaching mid-run, a seeding read) on the
app's own asyncio loop -- never a worker thread: the TUI has no job-running thread of its own."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted, RunStatus

MAX_OUTPUT_LINES = 2000


@dataclass
class RunState:
    """One run in the run table."""

    number: int
    pair: str
    status: str = "pending"
    seconds: float | None = None
    output: str | None = None


@dataclass(frozen=True)
class FinishedRun:
    """A run of this job that has ended."""

    number: int
    status: str
    # draw-things-cli's own time, as RunFinished reports it; what the cooldown follows.
    seconds: float | None


@dataclass(frozen=True)
class OutputLine:
    """One line the child printed, by stream."""

    stream: str
    text: str


class LiveRun:
    """Everything the live view shows about the queue entry the TUI follows; plain data, changed only on the thread
    that created it (the app's own asyncio loop, driven directly by the gRPC feed worker -- no thread hop).

    Starts empty: the TUI no longer holds the running entry's job file, so there is no schedule to build a run table
    from up front. ``JobStarted`` (live, or built from a seeding read attaching mid-run) seeds it instead, and
    pre-sizes the run table to ``total_runs`` placeholder rows, rows before ``first_run`` marked already succeeded
    (a resumed chain's earlier runs, which this session never itself ran and so never gets a RunStarted for).

    ``ended`` becomes true, however it is learned: a ``JobFinished`` arrived, its queue entry reached a final state
    (``queue_entry_changed``), or a reseed found nothing running -- a server crash or restart, a ``JobFinished``
    lost in a backlog gap that became a Reset, or an entry that failed before ``JobStarted``.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], datetime] = datetime.now) -> None:
        self._thread = threading.get_ident()
        self._clock = clock
        self._wall_clock = wall_clock
        # The queue entry followed (Q0007) and the execution's ID (E0012, read straight off JobStarted itself).
        self.queue_id: str | None = None
        self.job_name: str | None = None
        self.path: Path | None = None
        self.execution_id: str | None = None
        self.previous_arguments: tuple[int, Any] | None = None
        self.started: JobStarted | None = None
        self.runs: list[RunState] = []
        self.active_run: int | None = None
        self.run_started_at: float | None = None  # Monotonic time the active run's RunStarted was applied.
        # Verbose medium's own window opens when /verbose medium is typed mid-run, not only at a run's start.
        self.output_window_start: float | None = None
        self.active_output: str | None = None
        self.command: tuple[str, ...] = ()
        self.cooldown: CooldownStarted | None = None
        self.cooldown_ends_at: float | None = None  # Monotonic time the cooldown ends.
        self.cooled_after_run: int | None = None  # The run the last cooldown followed; the next starts without another wait.
        self.job_started_at: float | None = None  # Monotonic time JobStarted was applied.
        self.stopped_at: float | None = None  # Monotonic time a stop was requested; the bars freeze there.
        self.stopped_wall: float | None = None  # The same moment on the wall clock, which the forecast is read by.
        self.finished_runs: list[FinishedRun] = []
        # The forecast the server last published for this job, as its snapshot carries it; none before the first and after
        # the job. Its times are epoch seconds, read by the wall clock, as ``cooldown_until`` is.
        self.forecast: dict[str, Any] | None = None
        self.output: deque[OutputLine] = deque(maxlen=MAX_OUTPUT_LINES)
        self.output_count = 0  # Every line ever added, so a view knows how many it has not written yet.
        # Set only once a cancel this pane issued (/stop, or the Queue list's own) is confirmed by the API
        # succeeding, never optimistically: moment() pins the bars here, so a failed cancel must not freeze them.
        self.stop_requested = False
        # Whether the entry has a park reservation: from this TUI's own park or unpark once the API
        # confirms it, from queue_park_changed for one made elsewhere, and from GET /queue/{id} when the feed reseeds.
        # The bars keep moving: the run goes on.
        self.park_requested = False
        self.finished: JobFinished | None = None
        # The followed entry is over, however that was learned (see the class docstring); replaces the old
        # worker_ended, a local job-running thread that no longer exists.
        self.ended = False
        self.error: str | None = None

    @property
    def phase(self) -> str:
        """``starting``, ``running``, ``cooling_down``, ``stopping``, ``parking``, ``finished``, ``not_started``, or
        ``ended`` (the entry is over, but no JobFinished ever said how: it started, unlike ``not_started``, which never
        even got that far -- an entry that failed before JobStarted). A stop wins over a park reservation."""
        if self.ended and self.finished is None:
            return "not_started" if self.started is None else "ended"
        if self.finished is not None:
            return "finished"
        if self.stop_requested:
            return "stopping"
        if self.park_requested:
            return "parking"
        if self.cooldown is not None:
            return "cooling_down"
        return "running" if self.started is not None else "starting"

    def now(self) -> float:
        return self._clock()

    def wall_now(self) -> datetime:
        return self._wall_clock()

    def apply(self, event: JobEvent) -> None:
        """Update the state from one event, live or (seeding) synthetic."""
        self._check_thread()
        if isinstance(event, JobStarted):
            self._job_started(event)
        elif isinstance(event, RunStarted):
            self._run_started(event)
        elif isinstance(event, RunOutput):
            self._run_output(event)
        elif isinstance(event, RunFinished):
            self._run_finished(event)
        elif isinstance(event, CooldownStarted):
            self.cooldown = event
            self.cooldown_ends_at = self._clock() + event.seconds
        elif isinstance(event, CooldownEnded):
            self.cooled_after_run = self.cooldown.after_run if self.cooldown is not None else None
            self.cooldown = self.cooldown_ends_at = None
        elif isinstance(event, JobFinished):
            self.finished = event
            self.ended = True
            self.active_run = self.run_started_at = None
            self.cooldown = self.cooldown_ends_at = None
            self.forecast = None

    def _job_started(self, event: JobStarted) -> None:
        self.started = event
        self.job_name = event.job_name
        self.path = Path(event.job_file)
        self.execution_id = event.execution_id
        self.job_started_at = self._clock()
        self.runs = [RunState(number, "?", status=RunStatus.SUCCEEDED if number < event.first_run else "pending") for number in range(1, event.total_runs + 1)]

    def _run_started(self, event: RunStarted) -> None:
        run = self._run(event.number)
        run.status = RunStatus.RUNNING
        run.pair = event.pair
        self.active_run = event.number
        self.run_started_at = self._clock()
        self.active_output = event.output
        self.command = event.command

    def _run_output(self, event: RunOutput) -> None:
        self.output.append(OutputLine(event.stream, event.text))
        self.output_count += 1

    def _run_finished(self, event: RunFinished) -> None:
        run = self._run(event.number)
        run.status, run.seconds, run.output = event.status, event.seconds, event.output
        self.finished_runs.append(FinishedRun(event.number, event.status, event.seconds))
        self.active_run = self.run_started_at = None

    def apply_forecast(self, forecast: dict[str, Any] | None) -> None:
        """The server published a forecast for this job (or cleared it): shown from now on, in place of the last."""
        self._check_thread()
        self.forecast = forecast

    def request_stop(self) -> None:
        self._check_thread()
        self.stop_requested = True
        if self.stopped_at is None:
            self.stopped_at = self._clock()
            self.stopped_wall = self._wall_clock().timestamp()

    def shown_at(self) -> float:
        """The time, in epoch seconds, that the forecast is read at: now, or when a stop was requested, so a stopping job's
        bars stay where the stop found them."""
        return self.stopped_wall if self.stopped_wall is not None else self._wall_clock().timestamp()

    def end(self, *, error: str | None = None) -> None:
        """The followed entry is over, with no JobFinished to explain it (a queue_entry_changed to a final state, or
        a reseed that found nothing running); ``error`` is the entry's own, when it has one."""
        self._check_thread()
        self.ended = True
        self.forecast = None
        if error is not None:
            self.error = error

    def _run(self, number: int) -> RunState:
        while len(self.runs) < number:
            self.runs.append(RunState(len(self.runs) + 1, "?"))
        return self.runs[number - 1]

    def _check_thread(self) -> None:
        assert threading.get_ident() == self._thread, "LiveRun changed off the thread that created it"
