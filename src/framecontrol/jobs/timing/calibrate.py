"""The calibration fit: a pipeline's runs in, a bounded provisional calibration out. Pure, closed form, ``numpy`` only.

The step time is the factory's power law plus learned log terms. A shift (the intercept) opens with the first run, shrunk
toward the factory as if it were one more execution. A slope term (pixels, frames, aspect, render size, references, one run
knob) opens only when at least three independent executions vary it, and is a ridge toward the factory value with a prior the
``prior_weight_cap`` tunable sets. Each exact size then gets a residual offset shrunk by ``n / (n + k)``. Evidence is weighted
by execution, so many runs of one job do not stand for many independent measurements, and fades with the runs' age
(``half_life_runs``). Every change from the factory is clamped. The other stages carry their own log-linear fit on pixels and
frames. A hand-pinned value is held where the owner set it and the rest is fitted around it."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np

from framecontrol.jobs.timing.features import HistoryRun, feature_values
from framecontrol.jobs.timing.fit import MIN_STAGE_SIGMA, BackendStats, Row, backend_stats, read_row
from framecontrol.jobs.timing.knobs import KNOBS_LEARNED, KNOBS_PROVISIONAL, FactoryStage, PipelineCalibration, Spreads, StepModel, Summary, Tunables
from framecontrol.jobs.timing.ring import signature_id
from framecontrol.jobs.timing.step_terms import KNOB_PREFIX, STRUCTURAL, columns, learned_terms, size_key

CHECKPOINTS = (1, 2, 4, 8, 16, 32)
# Independent executions that must vary a term before it is fitted.
MIN_TERM_EXECUTIONS = 3
# The factory counts as this many pseudo-executions when the shift is fitted.
PRIOR_EXECUTIONS = 1.0
# Clamps on what a fit may move: the shift, an exponent away from the factory's, another term, and a size offset.
MAX_SHIFT = 1.5
MAX_EXPONENT_MOVE = 0.75
MAX_EXPONENT = 3.0
MAX_TERM = 2.0
MAX_SIZE_OFFSET = 1.0
MAX_OBSERVED_VALUES = 20
MAX_SIGNATURES = 40
MAX_STAGE_SIGMA = 3.0
MAX_OBSERVED_NAMES = 60
_RIDGE_FLOOR = 1e-9
VALIDATED_MAX_MOVE = 0.1


def checkpoint_at(count: int) -> bool:
    return count in CHECKPOINTS


def checkpoint_reached(count: int) -> int:
    """The highest checkpoint a count of completed runs has reached (0 before the first)."""
    return max((point for point in CHECKPOINTS if point <= count), default=0)


@dataclass(frozen=True)
class _Sample:
    row: Row
    weight: float
    columns: dict[str, float]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _weights(rows: Sequence[Row], half_life: float) -> list[float]:
    """One weight per row: the age decay (the newest run first, halving every ``half_life`` runs) over the number of the
    row's execution's runs in ``rows``, so an execution counts once however many runs it holds."""
    ordered = sorted(range(len(rows)), key=lambda index: rows[index].run.order, reverse=True)
    age = {index: rank for rank, index in enumerate(ordered)}
    per_execution: dict[int, int] = defaultdict(int)
    for row in rows:
        per_execution[row.execution] += 1
    return [0.5 ** (age[index] / half_life) / per_execution[row.execution] for index, row in enumerate(rows)]


def _independent(samples: Sequence[_Sample], name: str) -> bool:
    """Whether at least three independent executions carry the column and it takes more than one value among them."""
    values = {round(sample.columns[name], 6) for sample in samples if name in sample.columns}
    executions = {sample.row.execution for sample in samples if name in sample.columns}
    return len(values) >= 2 and len(executions) >= MIN_TERM_EXECUTIONS


def _ridge(samples: Sequence[_Sample], target: np.ndarray, names: Sequence[str], penalty: float) -> dict[str, float]:
    """A weighted ridge of ``target`` on the named columns (an intercept left free), each column standardized and penalized by
    ``penalty`` pseudo-runs: the coefficient of each in raw units."""
    weights = np.array([sample.weight for sample in samples])
    total = float(weights.sum())
    if not names:
        return {}
    if names:
        raw = np.array([[sample.columns.get(name, 0.0) for name in names] for sample in samples], dtype=float)
        means = (weights[:, None] * raw).sum(axis=0) / total
        centred = raw - means
        spread = np.sqrt((weights[:, None] * centred**2).sum(axis=0) / total)
        usable = spread > 1e-9
        scale = np.where(usable, spread, 1.0)
        design = centred / scale
        centre = float((weights * target).sum() / total)
        gram = design.T @ (weights[:, None] * design) + penalty * np.diag(usable.astype(float)) + _RIDGE_FLOOR * np.eye(len(names))
        beta = np.linalg.solve(gram, design.T @ (weights * (target - centre)))
        beta = np.where(usable, beta, 0.0)
        raw_beta = beta / scale
        return {name: float(raw_beta[index]) for index, name in enumerate(names)}
    return {}


def _shift_and_variance(samples: Sequence[_Sample], target: np.ndarray, terms: Mapping[str, float]) -> tuple[float, float]:
    """The shift once the term coefficients are fixed (the weighted mean residual, shrunk by the prior pseudo-execution) and
    the weighted variance left."""
    weights = np.array([sample.weight for sample in samples])
    offset = np.array([sum(terms.get(name, 0.0) * sample.columns.get(name, 0.0) for name in terms) for sample in samples])
    residual = target - offset
    total = float(weights.sum())
    shift = float((weights * residual).sum() / (total + PRIOR_EXECUTIONS))
    variance = float((weights * (residual - shift) ** 2).sum() / total)
    return shift, variance


def fit_step(base: StepModel, samples: Sequence[_Sample], tunables: Tunables, pins: Mapping[str, float]) -> tuple[StepModel, float, list[str]]:
    """The step model fitted on ``samples`` (rows with a mean step) away from ``base``, the weighted residual variance, and the
    names of the knob terms opened. ``pins`` hold the step values the owner fixed, by ``step/...`` path."""
    seconds = pins.get("step/seconds", base.seconds)
    prior = {
        "pixel_exponent": base.pixel_exponent,
        "frame_exponent": base.frame_exponent,
        "aspect": base.aspect,
        "render_exponent": base.render_exponent,
        "reference_exponent": base.reference_exponent,
        **{f"knob:{name}": value for name, value in base.knobs.items()},
    }
    fixed = {name: pins[f"step/{name}"] for name in ("pixel_exponent", "frame_exponent", "aspect", "render_exponent", "reference_exponent") if f"step/{name}" in pins}
    fixed.update({f"knob:{name}": pins[f"step/knobs/{name}"] for name in {path.split("/", 2)[2] for path in pins if path.startswith("step/knobs/")}})
    starting = {**prior, **fixed}
    target = np.array([sample.row.y if sample.row.y is not None else 0.0 for sample in samples]) - math.log(seconds)
    target = target - np.array([sum(starting.get(name, 0.0) * value for name, value in sample.columns.items()) for sample in samples])
    prior_sizes = {**base.sizes, **{path.split("/", 2)[2]: value for path, value in pins.items() if path.startswith("step/sizes/")}}
    target = target - np.array([prior_sizes.get(size_key(sample.row.run.workload) or "", 0.0) for sample in samples])
    candidates = ["pixel_exponent", "frame_exponent", "aspect", "render_exponent", "reference_exponent"]
    candidates.extend(sorted({*{name for name in prior if name.startswith(KNOB_PREFIX)}, *{name for sample in samples for name in sample.columns if name.startswith(KNOB_PREFIX)}}))
    opened = [name for name in candidates if name not in fixed and _independent(samples, name)]
    # A term whose column the fixed ones already span adds nothing; the ridge keeps the solve stable.
    coefficients = _ridge(samples, target, opened, tunables.prior_weight_cap)
    moves: dict[str, float] = {}
    for name, value in coefficients.items():
        if name in ("pixel_exponent", "frame_exponent"):
            moves[name] = _clamp(value, -MAX_EXPONENT_MOVE, MAX_EXPONENT_MOVE)
        else:
            moves[name] = _clamp(value, -MAX_TERM, MAX_TERM)
    shift_prior = pins.get("step/shift", base.shift)
    shift_move, variance = _shift_and_variance(samples, target - shift_prior, moves)
    if "step/shift" in pins:
        shift_move = 0.0
    shift = shift_prior if "step/shift" in pins else _clamp(shift_prior + shift_move, -MAX_SHIFT, MAX_SHIFT)
    exponent_values = {name: fixed.get(name, _clamp(prior[name] + moves.get(name, 0.0), 0.0, MAX_EXPONENT)) for name in ("pixel_exponent", "frame_exponent")}
    terms = {name: fixed.get(name, _clamp(prior.get(name, 0.0) + moves.get(name, 0.0), -MAX_TERM, MAX_TERM)) for name in {*prior, *fixed, *moves} if name not in ("pixel_exponent", "frame_exponent")}
    knob_terms = {name[len(KNOB_PREFIX) :]: value for name, value in terms.items() if name.startswith(KNOB_PREFIX)}
    sizes: dict[str, float] = prior_sizes
    if len({sample.row.execution for sample in samples}) >= 2:
        pinned_sizes = {path.split("/", 2)[2]: value for path, value in pins.items() if path.startswith("step/sizes/")}
        sizes = _size_offsets(samples, target - shift_prior, moves, shift_move, tunables, prior_sizes, pinned_sizes)
    model = replace(
        base,
        seconds=seconds,
        pixel_exponent=exponent_values["pixel_exponent"],
        frame_exponent=exponent_values["frame_exponent"],
        shift=shift,
        aspect=terms.get("aspect", base.aspect),
        render_exponent=terms.get("render_exponent", base.render_exponent),
        reference_exponent=terms.get("reference_exponent", base.reference_exponent),
        knobs=knob_terms,
        sizes=sizes,
    )
    return model, variance, [name[len(KNOB_PREFIX) :] for name in moves if name.startswith(KNOB_PREFIX)]


def _size_offsets(samples: Sequence[_Sample], target: np.ndarray, terms: Mapping[str, float], shift: float, tunables: Tunables, prior: Mapping[str, float], pinned: Mapping[str, float]) -> dict[str, float]:
    offset = np.array([sum(terms.get(name, 0.0) * sample.columns.get(name, 0.0) for name in terms) for sample in samples])
    residual = target - offset - shift
    groups: dict[str, list[int]] = defaultdict(list)
    for index, sample in enumerate(samples):
        key = size_key(sample.row.run.workload)
        if key is not None:
            groups[key].append(index)
    result: dict[str, float] = dict(prior)
    for key, indices in groups.items():
        if key in pinned:
            continue
        weight = sum(samples[index].weight for index in indices)
        mean = sum(samples[index].weight * residual[index] for index in indices) / weight
        result[key] = float(_clamp(result.get(key, 0.0) + mean * weight / (weight + tunables.size_offset_shrinkage), -MAX_SIZE_OFFSET, MAX_SIZE_OFFSET))
    return result


def _stage_fits(base: PipelineCalibration, rows: Sequence[Row], weights: Sequence[float], reference: tuple[int, int], tunables: Tunables, pins: Mapping[str, float]) -> tuple[FactoryStage, ...]:
    """The pipeline's stages in the order of its newest run, each with the duration at the reference workload and, where
    executions vary the workload, its own pixel and frame exponents."""
    newest = max(rows, key=lambda row: row.run.order)
    base_by_key = {stage.key: stage for stage in base.stages}
    learned: list[FactoryStage] = []
    for key, stage in newest.keyed.items():
        if key == newest.iterative_key:
            earlier = base_by_key.get(key)
            learned.append(replace(earlier, name=stage.stage, source=stage.source) if earlier is not None else FactoryStage(key, stage.stage, stage.source, 0.0, True))
            continue
        samples = [(row, weight) for row, weight in zip(rows, weights, strict=True) if key in row.keyed and "log_pixels" in row.features]
        earlier = base_by_key.get(key)
        learned.append(_fit_stage(key, stage.stage, stage.source, earlier, samples, reference, tunables, pins))
    present = {stage.key for stage in learned}
    merged = list(learned)
    # A stage of the base the newest run lacks (a color correction this run did not do) stays where the base had it.
    for position, stage in enumerate(base.stages):
        if stage.key in present:
            continue
        before = next((item.key for item in reversed(base.stages[:position]) if item.key in {entry.key for entry in merged}), None)
        index = next((index + 1 for index, entry in enumerate(merged) if entry.key == before), 0)
        merged.insert(index, stage)
    return tuple(merged)


def _fit_stage(key: str, name: str, source: str, earlier: FactoryStage | None, samples: Sequence[tuple[Row, float]], reference: tuple[int, int], tunables: Tunables, pins: Mapping[str, float]) -> FactoryStage:
    prefix = f"stages/{key}/"
    exponents = {"pixel_exponent": earlier.pixel_exponent if earlier else 0.0, "frame_exponent": earlier.frame_exponent if earlier else 0.0}
    if not samples:
        return earlier if earlier is not None else FactoryStage(key, name, source, 0.0)
    pixel_origin, frame_origin = math.log(reference[0]), math.log(reference[1])
    columns = [{"pixel_exponent": row.features["log_pixels"] - pixel_origin, "frame_exponent": row.features.get("log_frames", 0.0) - frame_origin} for row, _ in samples]
    logs = np.log(np.maximum([row.keyed[key].duration for row, _ in samples], 1e-3))
    members = [_Sample(row, weight, column) for (row, weight), column in zip(samples, columns, strict=True)]
    base_seconds = earlier.seconds if earlier is not None and earlier.seconds > 0 else None
    target = logs - np.array([sum(exponents[axis] * column[axis] for axis in exponents) for column in columns])
    opened = [axis for axis in exponents if f"{prefix}{axis}" not in pins and _independent(members, axis)]
    moves = _ridge(members, target, opened, tunables.prior_weight_cap)
    for axis, move in moves.items():
        exponents[axis] = _clamp(exponents[axis] + _clamp(move, -MAX_EXPONENT_MOVE, MAX_EXPONENT_MOVE), 0.0, MAX_EXPONENT)
    target = logs - np.array([sum(exponents[axis] * column[axis] for axis in exponents) for column in columns])
    weights = np.array([weight for _, weight in samples])
    total = float(weights.sum())
    mean = float((weights * target).sum() / total)
    if base_seconds is not None:
        mean = (mean * total + math.log(base_seconds) * PRIOR_EXECUTIONS) / (total + PRIOR_EXECUTIONS)
    seconds = _clamp(math.exp(mean), 1e-3, 1e6)
    sigma = None
    if len(samples) >= 2:
        residual = target - mean
        sigma = _clamp(math.sqrt(float((weights * residual**2).sum() / total)), MIN_STAGE_SIGMA, MAX_STAGE_SIGMA)
    return FactoryStage(key, name, source, seconds, False, exponents["pixel_exponent"], exponents["frame_exponent"], sigma)


def _observed(rows: Sequence[Row], previous: Mapping[str, tuple[float, ...]]) -> dict[str, tuple[float, ...]]:
    seen: dict[str, set[float]] = defaultdict(set)
    for name, values in previous.items():
        seen[name].update(float(value) for value in values)
    for row in rows:
        for name, value in row.features.items():
            if name not in STRUCTURAL:
                seen[name].add(round(value, 6))
    return {name: tuple(sorted(values)[:MAX_OBSERVED_VALUES]) for name, values in sorted(seen.items())[:MAX_OBSERVED_NAMES]}


def _summaries(rows: Sequence[Row], previous: Mapping[str, Summary]) -> dict[str, Summary]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        groups[signature_id(row.signature)].append(row)
    result = dict(previous)
    for name, members in groups.items():
        stages: dict[str, list[float]] = defaultdict(list)
        for row in members:
            for key, stage in row.keyed.items():
                stages[key].append(stage.duration)
        steps = [math.exp(row.y) for row in members if row.y is not None]
        result[name] = Summary(len(members), max(row.execution for row in members), {key: statistics.median(values) for key, values in stages.items()}, statistics.median(steps) if steps else None, len({row.execution for row in members}))
    ranked = sorted(result.items(), key=lambda item: (-item[1].count, -item[1].last_seen, item[0]))
    return dict(ranked[:MAX_SIGNATURES])


def calibrate(base: PipelineCalibration, previous: PipelineCalibration | None, runs: Sequence[HistoryRun], tunables: Tunables, *, checkpoint: int, stamp: str, pins: Mapping[str, float] | None = None, source: str = KNOBS_PROVISIONAL) -> PipelineCalibration | None:
    """The provisional calibration of one pipeline from its ``runs`` (live and complete), away from ``base`` (the factory) and
    carrying the observed values and summaries of ``previous``; None when no run has a step time to learn from."""
    rows = [read_row(run, tunables) for run in runs if run.fidelity == "live"]
    return calibrate_rows(base, previous, rows, tunables, checkpoint=checkpoint, stamp=stamp, pins=pins, source=source)


def calibrate_rows(base: PipelineCalibration, previous: PipelineCalibration | None, rows: Sequence[Row], tunables: Tunables, *, checkpoint: int, stamp: str, pins: Mapping[str, float] | None = None, source: str = KNOBS_PROVISIONAL) -> PipelineCalibration | None:
    """Fit an already parsed set of live rows, for callers that reuse them across cross-validation folds."""
    pins = pins or {}
    if not rows:
        return None
    weights = _weights(rows, tunables.half_life_runs)
    reference = (base.step.pixels, base.step.frames)
    steps = [(row, weight) for row, weight in zip(rows, weights, strict=True) if row.y is not None and math.isfinite(row.y)]
    if not steps:
        return None
    samples = [_Sample(row, weight, columns(row.run.workload, row.features, reference)) for row, weight in steps]
    step, variance, _opened = fit_step(base.step, samples, tunables, pins)
    executions = len({row.execution for row, _ in steps})
    effective = sum(weight for _, weight in steps)
    stats = backend_stats([row for row, _ in steps], rows, tunables)
    spreads = _measured_spreads(base.spreads, stats)
    spread = max(tunables.factory_spread, math.sqrt(variance + tunables.heuristic_log_spread**2 / (1.0 + effective)))
    stages = _stage_fits(base, rows, weights, reference, tunables, pins)
    carried = previous if previous is not None else base
    return PipelineCalibration(base.pipeline, step, spreads, stages, spread, source, len(rows), executions, checkpoint, stamp, _observed(rows, carried.observed), _summaries(rows, carried.summaries))


def _step_error(step: StepModel, run: HistoryRun, row: Row) -> float | None:
    pixels, frames = run.workload.get("pixels"), run.workload.get("frames")
    if row.y is None or not isinstance(pixels, int | float) or isinstance(pixels, bool) or not isinstance(frames, int | float) or isinstance(frames, bool):
        return None
    try:
        rate = step.rate(float(pixels), float(frames), learned_terms(step, run.workload, feature_values(run.workload)))
        return abs(math.log(rate) - row.y) if math.isfinite(rate) and rate > 0 else None
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def _validation_error(base: PipelineCalibration, runs: Sequence[HistoryRun], tunables: Tunables, *, pins: Mapping[str, float]) -> float:
    """Leave-one-execution-out step-rate error on training evidence only."""
    rows = [(run, read_row(run, tunables)) for run in runs if run.fidelity == "live"]
    by_execution = sorted({run.execution for run, row in rows if row.y is not None})
    errors = []
    for execution in by_execution:
        training = [(run, row) for run, row in rows if run.execution != execution and row.y is not None and math.isfinite(row.y)]
        if not training:
            continue
        weights = _weights([row for _run, row in training], tunables.half_life_runs)
        reference = (base.step.pixels, base.step.frames)
        samples = [_Sample(row, weight, columns(run.workload, row.features, reference)) for (run, row), weight in zip(training, weights, strict=True)]
        step, _variance, _opened = fit_step(base.step, samples, tunables, pins)
        fold = [error for run, row in rows if run.execution == execution if (error := _step_error(step, run, row)) is not None]
        if fold:
            errors.append(statistics.fmean(fold))
    return statistics.fmean(errors) if errors else math.inf


def _clamp_linear(value: float, baseline: float, maximum: float = VALIDATED_MAX_MOVE) -> float:
    return max(baseline - maximum, min(baseline + maximum, value))


def _clamp_positive(value: float, baseline: float, maximum: float = VALIDATED_MAX_MOVE) -> float:
    if baseline <= 0 or value <= 0:
        return baseline
    return baseline * math.exp(max(-maximum, min(maximum, math.log(value / baseline))))


def _clamp_mapping(candidate: Mapping[str, float], baseline: Mapping[str, float]) -> dict[str, float]:
    return {name: _clamp_linear(float(candidate.get(name, value)), float(value)) for name, value in baseline.items()} | {name: _clamp_linear(float(value), 0.0) for name, value in candidate.items() if name not in baseline}


def _bounded_validated(base: PipelineCalibration, candidate: PipelineCalibration) -> PipelineCalibration:
    """Clamp one approved update to a 0.1 movement in log/coefficient space."""
    step = replace(
        candidate.step,
        seconds=_clamp_positive(candidate.step.seconds, base.step.seconds),
        pixel_exponent=_clamp_linear(candidate.step.pixel_exponent, base.step.pixel_exponent),
        frame_exponent=_clamp_linear(candidate.step.frame_exponent, base.step.frame_exponent),
        shift=_clamp_linear(candidate.step.shift, base.step.shift),
        aspect=_clamp_linear(candidate.step.aspect, base.step.aspect),
        render_exponent=_clamp_linear(candidate.step.render_exponent, base.step.render_exponent),
        reference_exponent=_clamp_linear(candidate.step.reference_exponent, base.step.reference_exponent),
        knobs=_clamp_mapping(candidate.step.knobs, base.step.knobs),
        sizes=_clamp_mapping(candidate.step.sizes, base.step.sizes),
    )
    base_stages = {stage.key: stage for stage in base.stages}
    stages = []
    for stage in candidate.stages:
        earlier = base_stages.get(stage.key)
        if earlier is None:
            stages.append(stage)
        else:
            stages.append(replace(stage, seconds=_clamp_positive(stage.seconds, earlier.seconds) if earlier.seconds > 0 else stage.seconds, pixel_exponent=_clamp_linear(stage.pixel_exponent, earlier.pixel_exponent), frame_exponent=_clamp_linear(stage.frame_exponent, earlier.frame_exponent), sigma=_clamp_positive(stage.sigma, earlier.sigma) if stage.sigma is not None and earlier.sigma is not None else earlier.sigma))
    spread = _clamp_positive(candidate.spread, base.spread) if candidate.spread is not None and base.spread is not None else candidate.spread
    return replace(candidate, step=step, stages=tuple(stages), spread=spread)


def calibrate_validated(base: PipelineCalibration, runs: Sequence[HistoryRun], tunables: Tunables, *, checkpoint: int, stamp: str, pins: Mapping[str, float] | None = None, source: str = KNOBS_LEARNED) -> PipelineCalibration | None:
    """Fit a post-checkpoint candidate without consulting its reserved evidence.

    The ridge strength is selected by execution-level cross-validation on the
    training partition and capped by ``prior_weight_cap``.  Evidence spanning
    executable builds halves that cap, so a rebuilt child leans less heavily
    on its old approved prior.  The final movement is bounded per update.
    """
    pins = pins or {}
    if len({run.execution for run in runs}) < MIN_TERM_EXECUTIONS:
        return None
    builds = {(run.environment.get("executable_bytes"), run.environment.get("executable_mtime")) for run in runs if run.environment.get("executable_bytes") is not None and run.environment.get("executable_mtime") is not None}
    cap = tunables.prior_weight_cap * (0.5 if len(builds) > 1 else 1.0)
    penalties = sorted({min(float(value), cap) for value in (*tunables.penalties, cap) if value > 0})
    best = min(penalties, key=lambda value: (_validation_error(base, runs, replace(tunables, prior_weight_cap=value), pins=pins), value))
    candidate = calibrate(base, base, runs, replace(tunables, prior_weight_cap=best), checkpoint=checkpoint, stamp=stamp, pins=pins, source=source)
    return _bounded_validated(base, candidate) if candidate is not None else None


def _measured_spreads(base: Spreads, stats: BackendStats) -> Spreads:
    values = {name: getattr(stats, name) for name in ("tau", "rho", "slope", "slope_sd", "first_ratio", "first_sd", "step_noise", "stage_factor", "other_sd", "pause_share") if name in stats.measured}
    return replace(base, **values)


__all__ = ["CHECKPOINTS", "VALIDATED_MAX_MOVE", "calibrate", "calibrate_rows", "calibrate_validated", "checkpoint_at", "checkpoint_reached", "fit_step"]
