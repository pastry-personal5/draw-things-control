"""The forecast's knobs as plain values: the tunables (estimator decisions) and the calibration (what this host measured).

Nothing here reads a file or the clock. ``factory.py`` builds the factory pair, ``calibrate.py`` learns a calibration from
runs, and ``knob_codec.py`` turns either to and from the mappings the YAML files hold. Every value has hard bounds here; a
value outside them is ignored by the codec and the factory value stands."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol

# Where the effective numbers of a forecast came from (the basis's ``knobs``).
KNOBS_FACTORY, KNOBS_PROVISIONAL, KNOBS_LEARNED, KNOBS_PINNED = "factory", "provisional", "learned", "pinned"

# The tunables the Milestone 15 search may move, in the order tuning.yaml lists them.
SEARCHED = ("pause_factor", "size_offset_shrinkage", "prior_weight_cap", "half_life_runs", "unseen_knob_inflation", "rho", "slope_level", "factory_spread")
# The two levels a within-stage slope may be tested at: the lookup tables hold their Student-t quantiles.
SLOPE_LEVELS = (0.80, 0.95)


@dataclass(frozen=True)
class Tunables:
    """Estimator decisions. The first eight are searched (Milestone 15); the rest are safety floors and budgets that only a
    hand edit changes."""

    pause_factor: float = 3.0
    size_offset_shrinkage: float = 3.0
    prior_weight_cap: float = 12.0
    half_life_runs: float = 60.0
    unseen_knob_inflation: float = 1.5
    rho: float = 0.5
    slope_level: float = 0.95
    factory_spread: float = 0.25
    # Floors and budgets.
    slope_min_gaps: int = 6
    min_signature_runs: int = 3
    min_signature_executions: int = 2
    min_fit_runs: int = 6
    min_fit_executions: int = 3
    penalties: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)
    tau: float = 0.10
    heuristic_log_spread: float = 0.75
    heuristic_stage_spread: float = 0.60
    publish_interval_seconds: float = 2.0
    same_end_seconds: float = 1.0
    ring_size: int = 300
    backup_count: int = 20

    @property
    def searched(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in SEARCHED}


# (minimum, maximum) of each scalar tunable; an integer one is marked in INTEGER.
BOUNDS: dict[str, tuple[float, float]] = {
    "pause_factor": (1.5, 10.0),
    "size_offset_shrinkage": (0.5, 20.0),
    "prior_weight_cap": (1.0, 100.0),
    "half_life_runs": (5.0, 1000.0),
    "unseen_knob_inflation": (1.0, 4.0),
    "rho": (0.05, 0.95),
    "slope_level": (0.80, 0.95),
    "factory_spread": (0.05, 1.5),
    "slope_min_gaps": (3, 50),
    "min_signature_runs": (2, 50),
    "min_signature_executions": (1, 20),
    "min_fit_runs": (3, 100),
    "min_fit_executions": (2, 50),
    "tau": (0.02, 1.0),
    "heuristic_log_spread": (0.1, 3.0),
    "heuristic_stage_spread": (0.1, 3.0),
    "publish_interval_seconds": (0.5, 60.0),
    "same_end_seconds": (0.0, 30.0),
    "ring_size": (20, 5000),
    "backup_count": (1, 200),
}
INTEGER = frozenset({"slope_min_gaps", "min_signature_runs", "min_signature_executions", "min_fit_runs", "min_fit_executions", "ring_size", "backup_count"})
PENALTY_BOUNDS = (1e-4, 1e4)
MAX_PENALTIES = 12


def tunables_from_mapping(raw: Mapping[str, Any], base: Tunables | None = None) -> tuple[Tunables, list[str]]:
    """``base`` (the factory by default) with each valid value of ``raw`` applied, and a message for each one that was not:
    an invalid value never costs its siblings."""
    result = base or Tunables()
    problems: list[str] = []
    for name, value in raw.items():
        if name == "penalties":
            penalties = _penalties(value)
            if penalties is None:
                problems.append(f"penalties: expected up to {MAX_PENALTIES} positive numbers between {PENALTY_BOUNDS[0]} and {PENALTY_BOUNDS[1]}")
            else:
                result = replace(result, penalties=penalties)
            continue
        if name not in BOUNDS:
            problems.append(f"{name}: not a tunable")
            continue
        checked = _bounded(name, value)
        if checked is None:
            low, high = BOUNDS[name]
            problems.append(f"{name}: expected {'an integer' if name in INTEGER else 'a number'} from {low:g} to {high:g}, got {value!r}")
        elif name == "slope_level" and checked not in SLOPE_LEVELS:
            problems.append(f"slope_level: only {SLOPE_LEVELS} are supported, got {value!r}")
        else:
            result = replace(result, **{name: checked})
    return result, problems


def tunables_to_mapping(tunables: Tunables) -> dict[str, Any]:
    return {field.name: (list(getattr(tunables, field.name)) if field.name == "penalties" else getattr(tunables, field.name)) for field in fields(tunables)}


def _bounded(name: str, value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    low, high = BOUNDS[name]
    if not low <= value <= high:
        return None
    if name in INTEGER:
        return int(value) if float(value).is_integer() else None
    return float(value)


def _penalties(value: Any) -> tuple[float, ...] | None:
    if not isinstance(value, list | tuple) or not 1 <= len(value) <= MAX_PENALTIES:
        return None
    if not all(isinstance(item, int | float) and not isinstance(item, bool) and math.isfinite(item) and PENALTY_BOUNDS[0] <= item <= PENALTY_BOUNDS[1] for item in value):
        return None
    return tuple(float(item) for item in value)


# ------------------------------------------------------------------ calibration


@dataclass(frozen=True)
class FactoryStage:
    """One stage of a pipeline's expected sequence, priced at the calibration's reference workload.

    ``pixel_exponent`` and ``frame_exponent`` scale its seconds with the workload (FFmpeg and finishing grow with both);
    ``sigma`` is the spread in the log when learned, else the plan's own prior."""

    key: str
    name: str
    source: str
    seconds: float
    iterative: bool = False
    pixel_exponent: float = 0.0
    frame_exponent: float = 0.0
    sigma: float | None = None


@dataclass(frozen=True)
class StepModel:
    """The step time of the iterative stage as ``seconds * (pixels/p0)^a * (frames/f0)^b * exp(shift + terms)``: the
    reference workload's measured step, a power law over pixels and frames, and learned terms (aspect, render size,
    references, each knob, and an offset per exact size). Every learned term starts at zero, so the factory model is the
    power law alone."""

    pixels: int
    frames: int
    seconds: float
    pixel_exponent: float = 1.0
    frame_exponent: float = 1.0
    shift: float = 0.0
    aspect: float = 0.0
    render_exponent: float = 0.0
    reference_exponent: float = 0.0
    knobs: Mapping[str, float] = field(default_factory=dict)
    sizes: Mapping[str, float] = field(default_factory=dict)

    def rate(self, pixels: float, frames: float, terms: float = 0.0) -> float:
        """Seconds per step for a workload, ``terms`` being the sum of the learned log terms the caller found for it."""
        rate = self.seconds * (pixels / self.pixels) ** self.pixel_exponent * (frames / self.frames) ** self.frame_exponent
        extra = self.shift + terms
        return rate * math.exp(extra) if extra else rate


@dataclass(frozen=True)
class Spreads:
    """What a pipeline's runs say that does not depend on the workload: how the step time moves within the stage, how the
    first gap compares, and how far runs and executions stray."""

    tau: float = 0.10
    rho: float = 0.5
    slope: float = 0.0
    slope_sd: float = 0.06
    first_ratio: float | None = None
    first_sd: float = 0.0
    step_noise: float = 0.10
    stage_factor: float = 1.0
    other_sd: float = 20.0
    pause_share: float = 0.0


@dataclass(frozen=True)
class Summary:
    """A bounded per-signature summary: how often it ran, its median stage seconds, and when it was last seen."""

    count: int
    last_seen: int
    stages: Mapping[str, float]
    step_seconds: float | None = None
    executions: int = 1


@dataclass(frozen=True)
class PipelineCalibration:
    """One pipeline's calibration. ``source`` says where its numbers came from; ``checkpoint`` is the highest completed-run
    count (1, 2, 4, 8, 16, 32) it was fitted at, 0 for the factory."""

    pipeline: str
    step: StepModel
    spreads: Spreads
    stages: tuple[FactoryStage, ...]
    # The log spread of the step prior; None until a fit says (the plan then uses its tunable).
    spread: float | None = None
    source: str = KNOBS_FACTORY
    runs: int = 0
    executions: int = 0
    checkpoint: int = 0
    updated_at: str | None = None
    observed: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    summaries: Mapping[str, Summary] = field(default_factory=dict)

    @property
    def learned(self) -> bool:
        return self.source != KNOBS_FACTORY


class ExactPoint(Protocol):
    """A measured workload on one build, priced narrowly where a run matches it exactly."""

    @property
    def backend(self) -> str: ...

    @property
    def pipeline(self) -> str: ...

    def matches(self, backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> bool: ...


@dataclass(frozen=True)
class BackendCalibration:
    """One backend: its measured exact points, the pipeline whose figures a pipeline without its own borrows (``anchor``),
    and the calibration of each pipeline it has."""

    backend: str
    anchor: str
    pipelines: Mapping[str, PipelineCalibration]
    points: tuple[ExactPoint, ...] = ()
    # The workload and build the factory numbers were measured at, kept for the owner's reading.
    baseline: Mapping[str, Any] = field(default_factory=dict)
    # The frame exponent a pipeline without a calibration of its own borrows the anchor's figures with.
    frame_exponents: Mapping[str, float] = field(default_factory=dict)
    updated_at: str | None = None
    notes: str = ""

    def pipeline(self, pipeline: str | None) -> PipelineCalibration:
        """This pipeline's calibration, else the anchor's with the pipeline's own frame exponent."""
        if pipeline is not None and pipeline in self.pipelines:
            return self.pipelines[pipeline]
        anchor = self.pipelines[self.anchor]
        exponent = self.frame_exponents.get(pipeline) if pipeline is not None else None
        return replace(anchor, step=replace(anchor.step, frame_exponent=exponent)) if exponent is not None else anchor

    def baseline_pipeline(self, pipeline: str) -> PipelineCalibration:
        """The factory starting point of a pipeline a learned calibration may add: its own entry, else the anchor's figures
        under its name, with nothing observed."""
        if pipeline in self.pipelines:
            return self.pipelines[pipeline]
        return replace(self.pipeline(pipeline), pipeline=pipeline, source=KNOBS_FACTORY, runs=0, executions=0, checkpoint=0, updated_at=None, observed={}, summaries={})

    def has(self, pipeline: str | None) -> bool:
        return pipeline is not None and pipeline in self.pipelines


@dataclass(frozen=True)
class Calibration:
    """Every supported backend's calibration, and hand-pinned ``backend/path/to/value`` or ``tuning/path/to/value`` names."""

    backends: Mapping[str, BackendCalibration]
    pinned: frozenset[str] = frozenset()

    def backend(self, backend: str) -> BackendCalibration | None:
        return self.backends.get(backend)

    def point(self, backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> ExactPoint | None:
        found = self.backends.get(backend)
        if found is None:
            return None
        return next((point for point in found.points if point.matches(backend, pipeline, workload, environment)), None)

    def source_of(self, backend: str, pipeline: str | None) -> str:
        """``pinned`` when a hand-edited value touches the pipeline, else its factory, provisional, or learned source."""
        found = self.backends.get(backend)
        if found is None:
            return KNOBS_FACTORY
        prefix = f"{backend}/pipelines/{pipeline}/" if pipeline is not None else None
        if prefix is not None and any(name.startswith(prefix) for name in self.pinned):
            return KNOBS_PINNED
        return found.pipeline(pipeline).source if found.has(pipeline) else KNOBS_FACTORY

    def updated_at(self, backend: str) -> str | None:
        found = self.backends.get(backend)
        return found.updated_at if found is not None else None
