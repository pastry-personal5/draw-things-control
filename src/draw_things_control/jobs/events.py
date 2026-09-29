"""Typed events that describe a job as it runs, for front ends that show or record it."""

from __future__ import annotations

import types
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, get_args, get_origin, get_type_hints

from loguru import logger

from draw_things_control.core.cooldown import CooldownPolicy

# Every ``at`` is a local ISO 8601 timestamp with an offset, from the job service's clock.


class RunStatus(StrEnum):
    """How one run of a job stands or ended. The words are what manifests and the state store keep."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


class JobStatus(StrEnum):
    """How a job stands or ended."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    # Ended at a run boundary because it was parked (Milestone 05): every run it made succeeded, and a resume continues
    # it at the next one. Runs are never parked, only jobs.
    PARKED = "parked"


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
    cooldown: CooldownPolicy
    cooldown_source: str
    # Set only when records are written beside the outputs.
    manifest: str | None
    log: str | None
    # What the state store keeps so history shows which configuration ran.
    config_file: str = ""
    config_override: dict[str, Any] = field(default_factory=dict)
    input_resize: dict[str, Any] | None = None
    # The execution's ID (E0012), which the front end reserved in the state store before the job started.
    execution_id: str | None = None
    # The first run's number: above 1 only for a resume, which starts here instead of at 1.
    first_run: int = 1
    # The execution (E0012) this one resumes, when it is a resume.
    resumes_execution: str | None = None


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
    status: RunStatus
    # None when the run raised before it had an exit code.
    exit_code: int | None
    seconds: float | None
    # File names kept beside the outputs; None when nothing was written.
    output: str | None
    last_frame: str | None
    # What the output file actually holds, measured after a successful run: Draw Things may round or crop the size it
    # was asked for, and change the frame count. None when the run failed or the file could not be measured; frames is
    # None for an image.
    output_width: int | None = None
    output_height: int | None = None
    output_frames: int | None = None


@dataclass(frozen=True)
class CooldownStarted:
    """The job begins waiting before the next run."""

    at: str
    after_run: int
    seconds: float
    # Local wall-clock time the wait ends, HH:MM:SS.
    until: str
    # The cooldown mode and, for auto, its ratio; the time of the run the wait follows; and the bound that set the wait
    # (minimum or maximum), if one did. Together they say why the wait is that long.
    mode: str = "manual"
    ratio: float | None = None
    run_seconds: float | None = None
    bound: str | None = None


@dataclass(frozen=True)
class CooldownEnded:
    """The wait ended, in full or cut short by a stop."""

    at: str
    waited_seconds: float
    cut_short: bool


@dataclass(frozen=True)
class JobFinished:
    """The job ended: succeeded, failed, interrupted, or parked. Always the last event of a started job."""

    at: str
    status: JobStatus
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


# The name each event type goes by in JSON.
EVENT_KINDS: dict[type, str] = {
    JobStarted: "job_started",
    RunStarted: "run_started",
    RunOutput: "run_output",
    RunFinished: "run_finished",
    CooldownStarted: "cooldown_started",
    CooldownEnded: "cooldown_ended",
    JobFinished: "job_finished",
}


def event_to_dict(event: JobEvent) -> dict[str, Any]:
    """The event as a mapping ``json.dumps`` accepts, for the event stream: a ``kind``, then each field; statuses as their
    words, the cooldown policy as its mapping, and tuples as lists. Events hold no credential: a command is redacted."""
    data: dict[str, Any] = {"kind": EVENT_KINDS[type(event)]}
    for event_field in fields(event):
        data[event_field.name] = _plain(getattr(event, event_field.name))
    return data


def _plain(value: Any) -> Any:
    if isinstance(value, CooldownPolicy):
        return value.as_dict()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


EVENT_TYPES_BY_KIND: dict[str, type] = {name: cls for cls, name in EVENT_KINDS.items()}


def event_from_dict(data: dict[str, Any]) -> JobEvent:
    """The exact inverse of ``event_to_dict``: given a mapping with a ``kind`` and that kind's own fields (as a
    gRPC client decodes an ``Event.data_json``, Milestone 03), rebuilds the matching ``JobEvent`` dataclass."""
    event_type = EVENT_TYPES_BY_KIND[data["kind"]]
    hints = get_type_hints(event_type)
    values = {event_field.name: _typed(data[event_field.name], hints[event_field.name]) for event_field in fields(event_type)}
    return event_type(**values)


def _typed(value: Any, hint: Any) -> Any:
    """``value`` (already plain JSON) converted back to what ``hint`` (a field's real type, Optional unwrapped)
    names: a ``CooldownPolicy``, a ``StrEnum`` member, or a tuple; anything else (str, int, float, dict, None)
    round-trips as itself."""
    hint = _unwrap_optional(hint)
    if value is None:
        return None
    if hint is CooldownPolicy:
        return CooldownPolicy(**value)
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return hint(value)
    if get_origin(hint) is tuple:
        return tuple(value)
    return value


def _unwrap_optional(hint: Any) -> Any:
    if get_origin(hint) is types.UnionType:
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint
