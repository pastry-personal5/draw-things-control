"""The state of the job the TUI runs, built from its events on the main thread, and the messages that carry them there."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from textual.message import Message

from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted

MAX_OUTPUT_LINES = 2000


class JobEventMessage(Message):
    """One job event, posted from the job worker's thread to the app."""

    def __init__(self, event: JobEvent) -> None:
        super().__init__()
        self.event = event


class JobWorkerEnded(Message):
    """The job worker has released the run lock; always the last message of a job. ``error`` is why it raised, if it did."""

    def __init__(self, error: str | None = None) -> None:
        super().__init__()
        self.error = error


@dataclass
class RunState:
    """One run in the run table."""

    number: int
    pair: str
    status: str = "pending"
    seconds: float | None = None
    output: str | None = None


@dataclass(frozen=True)
class StepReading:
    """One reading of the child's step counter, at a monotonic time."""

    step: int
    total: int
    at: float


@dataclass(frozen=True)
class PastRun:
    """A run that already succeeded, which a run estimates from until its own step counter gives a rate."""

    # draw-things-cli's own time for the whole run, and its step count, when known.
    seconds: float
    steps: int | None = None


class PastRunFound(Message):
    """The latest successful run of any job, read from the state store before the job starts; None when there is none."""

    def __init__(self, past_run: PastRun | None) -> None:
        super().__init__()
        self.past_run = past_run


@dataclass(frozen=True)
class FinishedRun:
    """A run of this job that has ended, timed on the TUI's clock for the estimates."""

    number: int
    status: str
    # draw-things-cli's own time, as RunFinished reports it; what the cooldown follows.
    seconds: float | None
    # RunStarted to RunFinished: loading, the decode, last-frame extraction, tagging, and measuring included.
    full_seconds: float
    # From the last step counter reading to RunFinished; None when the counter never showed.
    tail_seconds: float | None
    # The counter's total, when it showed.
    steps: int | None = None


@dataclass(frozen=True)
class OutputLine:
    """One line the child printed, by stream."""

    stream: str
    text: str


class LiveRun:
    """Everything the live view shows about one job; plain data, changed only on the thread that created it."""

    def __init__(self, job: JobDefinition, path: Path, *, clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], datetime] = datetime.now) -> None:
        self._thread = threading.get_ident()
        self._clock = clock
        self._wall_clock = wall_clock
        self.job_name = job.name
        self.path = path
        # The execution's ID (E0012), once JobStarted has been recorded; None before, or when it is not recorded.
        self.execution_id: str | None = None
        # The last run with a command (its number and argument rows), which the next run's message is compared with.
        self.previous_arguments: tuple[int, Any] | None = None
        self.started: JobStarted | None = None
        self.runs = [RunState(number, pair.name) for number, pair in enumerate(job.schedule(), start=1)]
        self.active_run: int | None = None
        # Monotonic time the active run's RunStarted was applied.
        self.run_started_at: float | None = None
        self.active_output: str | None = None
        self.command: tuple[str, ...] = ()
        self.progress: tuple[int, int] | None = None
        self.percent: int | None = None
        self.cooldown: CooldownStarted | None = None
        # Monotonic time the cooldown ends.
        self.cooldown_ends_at: float | None = None
        # The run the last cooldown followed, once it has ended; the next run starts without another wait.
        self.cooled_after_run: int | None = None
        # Monotonic time JobStarted was applied, and the time a stop was requested (the estimates freeze there).
        self.job_started_at: float | None = None
        self.stopped_at: float | None = None
        # The active run's first counter reading, since the counter last started over, and its latest.
        self.first_step: StepReading | None = None
        self.last_step: StepReading | None = None
        self.finished_runs: list[FinishedRun] = []
        # The latest successful run of any job when this one started, from the state store.
        self.past_run: PastRun | None = None
        self.output: deque[OutputLine] = deque(maxlen=MAX_OUTPUT_LINES)
        # Every line ever added, so a view knows how many it has not written yet.
        self.output_count = 0
        self.stop_requested = False
        self.finished: JobFinished | None = None
        self.worker_ended = False
        self.error: str | None = None

    @property
    def phase(self) -> str:
        """``starting``, ``running``, ``cooling_down``, ``stopping``, ``finished``, or ``not_started``."""
        if self.worker_ended:
            return "finished" if self.finished is not None else "not_started"
        if self.stop_requested:
            return "stopping"
        if self.finished is not None:
            return "finished"
        if self.cooldown is not None:
            return "cooling_down"
        return "running" if self.started is not None else "starting"

    def now(self) -> float:
        return self._clock()

    def wall_now(self) -> datetime:
        return self._wall_clock()

    def apply(self, event: JobEvent) -> None:
        """Update the state from one event."""
        self._check_thread()
        if isinstance(event, JobStarted):
            self.started = event
            self.job_started_at = self._clock()
        elif isinstance(event, RunStarted):
            run = self._run(event.number)
            run.status = "running"
            self.active_run = event.number
            self.run_started_at = self._clock()
            self.active_output = event.output
            self.command = event.command
            self.progress = self.percent = None
            self.first_step = self.last_step = None
        elif isinstance(event, RunOutput):
            # The progress bar prints a new line per step; it updates the progress instead of filling the pane.
            if event.progress is not None or event.percent is not None:
                self.progress = event.progress or self.progress
                self.percent = event.percent if event.percent is not None else self.percent
                if event.progress is not None:
                    self._read_step(*event.progress)
            else:
                self.output.append(OutputLine(event.stream, event.text))
                self.output_count += 1
        elif isinstance(event, RunFinished):
            run = self._run(event.number)
            run.status, run.seconds, run.output = event.status, event.seconds, event.output
            now = self._clock()
            full = now - self.run_started_at if self.run_started_at is not None else event.seconds or 0.0
            tail = now - self.last_step.at if self.last_step is not None else None
            steps = self.last_step.total if self.last_step is not None else None
            self.finished_runs.append(FinishedRun(event.number, event.status, event.seconds, max(0.0, full), tail, steps))
            self.active_run = self.run_started_at = None
        elif isinstance(event, CooldownStarted):
            self.cooldown = event
            self.cooldown_ends_at = self._clock() + event.seconds
        elif isinstance(event, CooldownEnded):
            self.cooled_after_run = self.cooldown.after_run if self.cooldown is not None else None
            self.cooldown = self.cooldown_ends_at = None
        elif isinstance(event, JobFinished):
            self.finished = event
            self.active_run = self.run_started_at = None
            self.cooldown = self.cooldown_ends_at = None

    def reference_run(self) -> PastRun | None:
        """The run a run estimates from before its own rate: this job's last successful run, or else the store's latest."""
        own = next((run for run in reversed(self.finished_runs) if run.status == "succeeded" and run.seconds), None)
        if own is not None and own.seconds is not None:
            return PastRun(own.seconds, own.steps)
        return self.past_run

    def request_stop(self) -> None:
        self._check_thread()
        self.stop_requested = True
        if self.stopped_at is None:
            self.stopped_at = self._clock()

    def _read_step(self, step: int, total: int) -> None:
        """Keep the counter's first and latest readings; a counter that goes back or changes its total starts over."""
        now = self._clock()
        last = self.last_step
        if self.first_step is None or last is None or step < last.step or total != last.total:
            self.first_step = StepReading(step, total, now)
        self.last_step = StepReading(step, total, now)

    def end(self, error: str | None) -> None:
        """The worker ended; ``error`` is the exception it caught, if any."""
        self._check_thread()
        self.worker_ended = True
        self.error = error

    def _run(self, number: int) -> RunState:
        while len(self.runs) < number:
            self.runs.append(RunState(len(self.runs) + 1, "?"))
        return self.runs[number - 1]

    def _check_thread(self) -> None:
        assert threading.get_ident() == self._thread, "LiveRun changed off the thread that created it"
