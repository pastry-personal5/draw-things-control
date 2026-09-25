"""Typed events that describe a job as it runs, for front ends that show or record it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

# Every ``at`` is a local ISO 8601 timestamp with an offset, from the job service's clock.


@dataclass(frozen=True)
class JobStarted:
    """The job is about to start its first run."""

    at: str
    job_name: str
    job_file: str
    # The job file's exact text; job files reject unknown keys, so it holds no credential.
    source_text: str
    mode: str
    total_runs: int
    output_directory: str
    input: str | None
    model: str
    seed: int
    seed_source: str
    cooldown_seconds: float
    cooldown_source: str
    # Set only when records are written beside the outputs.
    manifest: str | None
    log: str | None


@dataclass(frozen=True)
class RunStarted:
    """A run's command is about to be launched."""

    at: str
    number: int
    total: int
    pair: str
    positive: str
    negative: str | None
    input: str | None
    # Run 1's resized copy, which the command passes as --image; removed after the run.
    resized_input: str | None
    output: str
    last_frame: str | None
    # The command with credentials redacted.
    command: tuple[str, ...]


@dataclass(frozen=True)
class RunOutput:
    """One line the child printed."""

    at: str
    number: int
    stream: str
    text: str
    # (current, total) when the line reports progress.
    progress: tuple[int, int] | None
    # 0-100 when the line shows a percentage, such as the progress bar's.
    percent: int | None = None


@dataclass(frozen=True)
class RunFinished:
    """A run ended: succeeded, failed, timed_out, or interrupted."""

    at: str
    number: int
    status: str
    # None when the run raised before it had an exit code.
    exit_code: int | None
    seconds: float | None
    # File names kept beside the outputs; None when nothing was written.
    output: str | None
    last_frame: str | None


@dataclass(frozen=True)
class CooldownStarted:
    """The job begins waiting before the next run."""

    at: str
    after_run: int
    seconds: float
    # Local wall-clock time the wait ends, HH:MM:SS.
    until: str


@dataclass(frozen=True)
class CooldownEnded:
    """The wait ended, in full or cut short by a stop."""

    at: str
    waited_seconds: float
    cut_short: bool


@dataclass(frozen=True)
class JobFinished:
    """The job ended: succeeded, failed, or interrupted. Always the last event of a started job."""

    at: str
    status: str
    # None when the job raised before it had an exit code.
    exit_code: int | None
    completed_runs: int
    total_runs: int
    # The signal that stopped the job, when it was interrupted.
    signal: str | None


JobEvent = JobStarted | RunStarted | RunOutput | RunFinished | CooldownStarted | CooldownEnded | JobFinished
JobObserver = Callable[[JobEvent], None]


def notify(observer: JobObserver, event: JobEvent) -> None:
    """Send ``event`` to ``observer``; an observer that raises is logged, never allowed to stop the job."""
    try:
        observer(event)
    except Exception:
        logger.exception("Job event observer failed on {}", type(event).__name__)


def combine_observers(*observers: JobObserver) -> JobObserver:
    """One observer that calls each of ``observers`` in order; one that raises does not stop the others."""

    def observe(event: JobEvent) -> None:
        for observer in observers:
            notify(observer, event)

    return observe
