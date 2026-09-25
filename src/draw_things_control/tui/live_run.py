"""The state of the job the TUI runs, built from its events on the main thread, and the messages that carry them there."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
class OutputLine:
    """One line the child printed, by stream."""

    stream: str
    text: str


class LiveRun:
    """Everything the live view shows about one job; plain data, changed only on the thread that created it."""

    def __init__(self, job: JobDefinition, path: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._thread = threading.get_ident()
        self._clock = clock
        self.job_name = job.name
        self.path = path
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

    def apply(self, event: JobEvent) -> None:
        """Update the state from one event."""
        self._check_thread()
        if isinstance(event, JobStarted):
            self.started = event
        elif isinstance(event, RunStarted):
            run = self._run(event.number)
            run.status = "running"
            self.active_run = event.number
            self.run_started_at = self._clock()
            self.active_output = event.output
            self.command = event.command
            self.progress = self.percent = None
        elif isinstance(event, RunOutput):
            # The progress bar prints a new line per step; it updates the progress instead of filling the pane.
            if event.progress is not None or event.percent is not None:
                self.progress = event.progress or self.progress
                self.percent = event.percent if event.percent is not None else self.percent
            else:
                self.output.append(OutputLine(event.stream, event.text))
                self.output_count += 1
        elif isinstance(event, RunFinished):
            run = self._run(event.number)
            run.status, run.seconds, run.output = event.status, event.seconds, event.output
            self.active_run = self.run_started_at = None
        elif isinstance(event, CooldownStarted):
            self.cooldown = event
            self.cooldown_ends_at = self._clock() + event.seconds
        elif isinstance(event, CooldownEnded):
            self.cooldown = self.cooldown_ends_at = None
        elif isinstance(event, JobFinished):
            self.finished = event
            self.active_run = self.run_started_at = None
            self.cooldown = self.cooldown_ends_at = None

    def request_stop(self) -> None:
        self._check_thread()
        self.stop_requested = True

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
