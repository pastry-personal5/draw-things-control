"""What a forecast reads out of a run's history: the rows themselves, the signature that groups them, the feature
values a workload gives, and the per-step statistics of an iterative stage.

A ``HistoryRun`` is a complete run as the state store holds it, with no database in it, so this module and the ones that
fit and forecast from it import only ``core`` and ``numpy``."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, TypeGuard

import numpy as np

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.timing.knobs import Tunables

# Run knobs that do not change the work a run does: the seed, the clip length (the effective ``frames`` carries it), the
# timeout, and the name of the reference pair (its references are counted in the workload).
IGNORED_KNOBS = frozenset({"seed", "seconds", "timeout_seconds", "reference_pair"})
# Workload keys that are not features of the step time: the steps are counted, not timed, and the strings name what the
# pipeline and signature already hold.
# The reuse knobs are not features either: they change how many steps are evaluated (see ``evaluated``), which the step
# weights carry, so a reuse value the history has not seen is still priced.
_NOT_FEATURES = frozenset({"width", "height", "pixels", "frames", "steps", "seconds", "reference_count", "references", "render_width", "render_height", "knobs", "pipeline", "mode", "seed", "denoise_reuse", "core_reuse"})
# A gap longer than ``pause_factor`` times the median of the others is a pause, and a within-stage slope is fitted only from
# ``slope_min_gaps`` ordinary gaps: both are tunables (``knobs.Tunables``), these are their factory values.
PAUSE_FACTOR = Tunables().pause_factor
SLOPE_MIN_GAPS = Tunables().slope_min_gaps
# The most a stage name can repeat is counted by occurrence.
FRAMECONTROL = "framecontrol"

Marks = tuple[tuple[float, int], ...]
Signature = tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class HistoryStage:
    """One span of a recorded run, in seconds from the run's start."""

    stage: str
    source: str
    start: float
    end: float
    done: int | None = None
    total: int | None = None
    unit: str | None = None
    marks: Marks = ()
    # The first mark's offset was estimated from a log rather than measured.
    uncertain: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class HistoryRun:
    """A complete run of the history. ``workload`` and ``environment`` are the row's own, as ``TimingStarted`` recorded them
    (a log row's are the few a log holds); ``output_frames`` is what the run's output measured, which stands in for
    ``frames`` where the workload holds none."""

    execution: int
    run: int
    fidelity: str
    backend: str
    pipeline: str | None
    workload: Mapping[str, Any]
    environment: Mapping[str, Any]
    stages: tuple[HistoryStage, ...]
    output_frames: int | None = None

    @cached_property
    def signature(self) -> Signature:
        return signature(self.backend, self.pipeline, self.workload, self.environment)

    @cached_property
    def counted_stages(self) -> tuple[HistoryStage, ...]:
        """The stages of the run itself in time order, leaving out work done before it started (negative offsets)."""
        return tuple(sorted((stage for stage in self.stages if stage.start >= 0), key=lambda stage: (stage.start, stage.end)))

    @property
    def order(self) -> tuple[int, int]:
        """Where the run falls in time: later executions and later runs of one are newer."""
        return (self.execution, self.run)

    @property
    def seconds(self) -> float:
        """The run's time, from its start to the end of its last stage."""
        stages = self.counted_stages
        return max((stage.end for stage in stages), default=0.0)

    @property
    def child_seconds(self) -> float:
        """The sum of the child's stages, which is what a cooldown's ``wait_after`` is given."""
        return sum(stage.duration for stage in self.counted_stages if stage.source != FRAMECONTROL)


@dataclass(frozen=True)
class HistoryExecution:
    """The runs of one execution in the order they ran, and the cooldown policy that spaced them."""

    number: int
    cooldown: CooldownPolicy
    runs: tuple[HistoryRun, ...]
    # The seconds waited after each run, where the execution recorded them; none for one that follows its policy.
    waits: tuple[float, ...] | None = None

    def wait_after(self, run: HistoryRun) -> float:
        """The wait that followed ``run``: the recorded one, else what the policy gives for its child's time."""
        if self.waits is not None:
            return self.waits[run.run - 1] if 0 < run.run <= len(self.waits) else 0.0
        return self.cooldown.wait_after(run.child_seconds).seconds


def reuse_interval(workload: Mapping[str, Any]) -> int:
    """H3's reuse interval: ``denoise_reuse`` or ``core_reuse``, whichever is set (the job cannot set both above 1), else 1."""
    interval = 1
    for name in ("denoise_reuse", "core_reuse"):
        value = workload.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > interval:
            interval = value
    return interval


def evaluated(index: int, total: int, interval: int) -> bool:
    """Whether step ``index`` (from 0) of ``total`` does the work of a step. With a reuse interval above 1 H3 evaluates
    the first step, the last, and every step at a multiple of the interval, and the rest reuse an earlier result and
    arrive in the same burst as the evaluated step before them (``h3_dit_reuse_schedule``, and the core refresh)."""
    return interval <= 1 or index == 0 or index == total - 1 or index % interval == 0


def evaluated_count(total: int, interval: int) -> int:
    """How many of ``total`` steps are evaluated."""
    return sum(1 for index in range(total) if evaluated(index, total, interval))


def evaluated_between(done: int, later: int, total: int, interval: int) -> int:
    """How many of the steps the counter passes from ``done`` to ``later`` (steps ``done`` to ``later - 1``) are evaluated."""
    return sum(1 for index in range(done, later) if evaluated(index, total, interval))


def signature(backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> Signature:
    """What makes two runs the same job for timing: the backend, the pipeline, the model, and every workload value but
    ``seconds`` (the effective ``frames`` carries it), keys sorted, nested mappings flattened, floats rounded to six places.
    The executable's size and modification time and the host's load are not in it: a rebuilt H3 would start every one over."""
    items = [(key, value) for key, value in _flatten("", workload)]
    return (("backend", backend), ("pipeline", pipeline), ("model", environment.get("model")), *sorted(items))


def _flatten(prefix: str, values: Mapping[str, Any]) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in values.items():
        name = f"{prefix}{key}"
        if prefix == "" and key == "seconds":
            continue
        if prefix == "knobs." and key in IGNORED_KNOBS:
            continue
        if isinstance(value, Mapping):
            items.extend(_flatten(f"{name}.", value))
        else:
            items.append((name, _plain(value)))
    return items


def _plain(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list | tuple):
        return tuple(_plain(item) for item in value)
    return value


def stage_keys(run: HistoryRun) -> list[str]:
    """The run's stage names in order, a name that repeats (a counter that started over) suffixed with its occurrence."""
    seen: dict[str, int] = {}
    keys = []
    for stage in run.counted_stages:
        seen[stage.stage] = seen.get(stage.stage, 0) + 1
        keys.append(stage.stage if seen[stage.stage] == 1 else f"{stage.stage}#{seen[stage.stage]}")
    return keys


def expected_run(history: Sequence[HistoryRun], run_signature: Signature, backend: str, pipeline: str | None) -> HistoryRun | None:
    """The run whose stages a run is expected to go through: the newest with its signature, else the newest of its backend
    and pipeline; none when the history holds neither."""
    same = [run for run in history if run.signature == run_signature]
    pool = same or [run for run in history if run.backend == backend and run.pipeline == pipeline]
    return max(pool, key=lambda run: run.order) if pool else None


def expected_stages(history: Sequence[HistoryRun], run_signature: Signature, backend: str, pipeline: str | None) -> list[HistoryStage]:
    """The stages of ``expected_run``, in order; none when the history holds no such run."""
    run = expected_run(history, run_signature, backend, pipeline)
    return list(run.counted_stages) if run is not None else []


def iterative_stage(run: HistoryRun) -> HistoryStage | None:
    """The stage that counts steps and keeps their marks, the longest if the counter started over."""
    candidates = [stage for stage in run.counted_stages if stage.unit == "steps" and len(stage.marks) >= 2]
    return max(candidates, key=lambda stage: stage.duration, default=None)


@dataclass(frozen=True)
class StepSeries:
    """What one iterative stage says about its step time."""

    # Seconds a step took, over the steps that were not the first gap or a pause.
    mean_seconds: float
    # The first gap's step time over the median of the others, when it was measured and there are others to compare.
    first_ratio: float | None
    # Change of log step time across the whole stage (position 0 to 1), when enough steps support a line.
    slope: float | None
    # The spread of log step time about the mean or line.
    noise: float
    pauses: int
    steps: int
    # Seconds the pauses took beyond the steps they replaced.
    pause_seconds: float = 0.0


def step_series(stage: HistoryStage, interval: int = 1, pause_factor: float = PAUSE_FACTOR, slope_min_gaps: int = SLOPE_MIN_GAPS) -> StepSeries | None:
    """The per-step statistics of ``stage``'s marks, per evaluated step when ``interval`` is a reuse interval; None when it
    keeps none."""
    gaps = _gaps(stage, interval)
    if not gaps:
        return None
    later = gaps[1:]
    median = statistics.median(time for time, _, _ in later) if later else None
    ordinary = [gap for gap in later if median is None or gap[0] <= pause_factor * median]
    pauses = len(later) - len(ordinary)
    pause_seconds = sum((time - median) * count for time, count, _ in later if median is not None and time > pause_factor * median)
    pool = ordinary or gaps
    units = sum(count for _, count, _ in pool)
    mean = sum(time * count for time, count, _ in pool) / units
    first_ratio = None
    first_time = gaps[0][0]
    if not stage.uncertain and len(ordinary) >= 2:
        first_ratio = first_time / statistics.median(time for time, _, _ in ordinary)
    slope = None
    logs = np.log([time for time, _, _ in pool])
    if len(pool) >= slope_min_gaps:
        positions = np.array([position for _, _, position in pool])
        coefficients = np.polyfit(positions, logs, 1)
        slope = float(coefficients[0])
        residuals = logs - np.polyval(coefficients, positions)
        noise = float(math.sqrt(float(np.sum(residuals**2)) / (len(pool) - 2)))
    elif len(pool) >= 2:
        noise = float(np.std(logs, ddof=1))
    else:
        noise = 0.0
    return StepSeries(mean, first_ratio, slope, noise, pauses, sum(count for _, count, _ in gaps), pause_seconds)


def _gaps(stage: HistoryStage, interval: int = 1) -> list[tuple[float, int, float]]:
    """(seconds an evaluated step took, evaluated steps in the gap, the stage's position at the gap's middle) for each pair
    of marks. Under a reuse interval a reused step's reading arrives with the evaluated step before it, so a gap that
    holds no evaluated step has no step time: its seconds join the next gap that does."""
    marks = stage.marks
    total = stage.total or (marks[-1][1] if marks else 0)
    gaps = []
    carried = 0.0
    for (earlier_time, earlier_done), (later_time, later_done) in zip(marks, marks[1:], strict=False):
        steps, seconds = later_done - earlier_done, later_time - earlier_time
        if steps < 1 or seconds < 0 or total < 1:
            continue
        count = evaluated_between(earlier_done, later_done, total, interval)
        if count < 1:
            carried += seconds
            continue
        seconds += carried
        carried = 0.0
        if seconds > 0:
            gaps.append((seconds / count, count, (earlier_done + steps / 2) / total))
    return gaps


def feature_values(workload: Mapping[str, Any], output_frames: int | None = None) -> dict[str, float]:
    """The numbers the step time is fitted on: log pixels and frames, log of one plus the references, log render pixels,
    and every other knob the workload holds (a number as itself, a flag as 0 or 1, a text as an indicator named
    ``key=value``). The run knobs are prefixed ``knob.``. A key the workload lacks is not in the result."""
    values: dict[str, float] = {}
    pixels = workload.get("pixels")
    width, height = workload.get("width"), workload.get("height")
    if not _is_number(pixels) and _is_number(width) and _is_number(height):
        pixels = width * height
    if _is_number(pixels) and pixels > 0:
        values["log_pixels"] = math.log(pixels)
    frames = workload.get("frames")
    if not _is_number(frames):
        frames = output_frames
    values["log_frames"] = math.log(max(1, frames)) if _is_number(frames) else 0.0
    if _is_number(workload.get("reference_count")):
        values["log_refs"] = math.log1p(workload["reference_count"])
    render_width, render_height = workload.get("render_width"), workload.get("render_height")
    if _is_number(render_width) and _is_number(render_height) and render_width > 0 and render_height > 0:
        values["log_render_pixels"] = math.log(render_width * render_height)
    for key, value in workload.items():
        if key not in _NOT_FEATURES:
            values.update(_knob_value(key, value))
    knobs = workload.get("knobs")
    if isinstance(knobs, Mapping):
        for key, value in knobs.items():
            if key not in IGNORED_KNOBS and key != "frames":
                values.update(_knob_value(f"knob.{key}", value))
    return values


def _knob_value(name: str, value: Any) -> dict[str, float]:
    if isinstance(value, bool):
        return {name: float(value)}
    if _is_number(value):
        return {name: float(value)}
    if isinstance(value, str):
        return {f"{name}={value}": 1.0}
    return {}


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
