"""The fit: which history a run is priced from, what it predicts, and how sure it is. All of it is closed form with numpy.

A run is a sum of stages. Its iterative stage (steps) dominates, and its step time is modeled as
``log t = f(workload) + u + noise`` with one training row per complete run: the mean step time, the first gap and any
pause left out. ``f`` is the signature's mean (tier 1) or a ridge-regularized least-squares fit (tiers 2 and 3) whose
penalty is chosen from ``PENALTIES`` by leave-one-execution-out error; ``u`` is the run's own speed factor. The other
stages are the same-signature median, or a log-linear fit on pixels and frames. ``FittedModel.plan`` returns what a
forecast needs to price one run, and the basis it rests on. Nothing here reads a file, a database, or the clock."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND
from framecontrol.jobs.timing.factory import H3_R2AV_E0051_E0054, DrawThingsFactoryPoint, H3FactoryPoint, factory_point, heuristic_anchor
from framecontrol.jobs.timing.features import HistoryRun, HistoryStage, Signature, StepSeries, evaluated_count, expected_run, feature_values, iterative_stage, reuse_interval, signature, stage_keys, step_series

TIER_SIGNATURE, TIER_BACKEND, TIER_PIPELINE, TIER_FACTORY, TIER_HEURISTIC, TIER_LIVE = "signature", "backend_fit", "pipeline_fit", "factory", "heuristic", "live_only"
LIVE, LOG, POOLED = "live", "log", "pooled"

# The thresholds are provisional, pinned by tests.
MIN_SIGNATURE_RUNS, MIN_SIGNATURE_EXECUTIONS = 3, 2
MIN_FIT_RUNS, MIN_FIT_EXECUTIONS = 6, 3
# Same-signature stage durations need this many samples before their median stands in for a fit.
MIN_STAGE_SAMPLES = 3
# Signatures with runs from two executions that must exist before the speed factor's spread is read from the data.
MIN_SPREAD_SIGNATURES = 3
# Executions whose within-stage slope must be seen before it is applied (and the first gap's runs before its ratio is).
MIN_SLOPE_EXECUTIONS, MIN_FIRST_GAP_RUNS = 3, 3
DEFAULT_TAU, DEFAULT_RHO = 0.10, 0.5
MIN_TAU = 0.02
DEFAULT_STEP_NOISE = 0.10
# The spread of a run's own within-stage slope about the backend's, when too few runs say, and the least it is taken to be.
DEFAULT_SLOPE_SD, MIN_SLOPE_SD = 0.06, 0.02
MIN_SLOPE_RUNS = 6
# A stage's step time is not taken to change by more than a factor of e**2 across it, whatever a few runs seem to say.
MAX_SLOPE = 2.0
# Nor is a step time taken to be uncertain by more than this in the log from one step to the next.
MAX_STEP_NOISE = 1.0
# The least spread, in the log, the first gap's ratio to the later steps is taken to have from one run to the next.
MIN_FIRST_SD = 0.02
# The spread of a stage duration in the log, when no history says, and the least it is taken to be.
DEFAULT_STAGE_SIGMA, MIN_STAGE_SIGMA = 0.15, 0.03
# Seconds of spread for a stage the history has not seen, when the backend has no stage history at all.
DEFAULT_OTHER_SD = 20.0
# A measured point transferred to another workload is useful immediately, but much less certain than an exact match.
HEURISTIC_LOG_SPREAD, HEURISTIC_STAGE_SPREAD = 0.75, 0.60
# The H3 color-finishing evidence is 38-46 seconds altogether; these rough shares let each actual stage start match
# the plan, while keeping the same 42-second total. They are replaced by sparse stage measurements when available.
H3_COPY_RAW_SECONDS, H3_CORRECT_SECONDS, H3_LAST_FRAME_SECONDS = 8.0, 33.0, 1.0
# The ridge penalties tried, by leave-one-execution-out error.
PENALTIES = (0.01, 0.1, 1.0, 10.0, 100.0)
MIN_VARIANCE = 1e-12

# Student-t quantiles for degrees of freedom 1 to 30: 0.90 (the 80% interval) and 0.975 (the 95% interval).
T90 = (3.0777, 1.8856, 1.6377, 1.5332, 1.4759, 1.4398, 1.4149, 1.3968, 1.3830, 1.3722, 1.3634, 1.3562, 1.3502, 1.3450, 1.3406, 1.3368, 1.3334, 1.3304, 1.3277, 1.3253, 1.3232, 1.3212, 1.3195, 1.3178, 1.3163, 1.3150, 1.3137, 1.3125, 1.3114, 1.3104)
T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)
NORMAL_90, NORMAL_975 = 1.2816, 1.9600


def _variance(values: Sequence[float]) -> float:
    """The sample variance, in plain floats (``statistics`` does exact fractions, which a forecast cannot afford)."""
    centre = sum(values) / len(values)
    return sum((value - centre) ** 2 for value in values) / (len(values) - 1)


def _stdev(values: Sequence[float]) -> float:
    return math.sqrt(_variance(values))


def _pstdev(values: Sequence[float]) -> float:
    centre = sum(values) / len(values)
    return math.sqrt(sum((value - centre) ** 2 for value in values) / len(values))


def _quantile(table: tuple[float, ...], normal: float, df: float) -> float:
    whole = max(1, int(df))
    return table[whole - 1] if whole <= len(table) else normal


def student_t_90(df: float) -> float:
    """The quantile that bounds an 80% interval on both sides, for ``df`` residual degrees of freedom (normal above 30)."""
    return _quantile(T90, NORMAL_90, df)


def student_t_975(df: float) -> float:
    """The quantile that bounds a 95% interval, for ``df`` degrees of freedom (normal above 30)."""
    return _quantile(T975, NORMAL_975, df)


@dataclass(frozen=True)
class FixedStage:
    """A stage priced as one duration: lognormal, with this median (seconds) and spread (in the log)."""

    key: str
    stage: str
    source: str
    median: float
    sigma: float

    @property
    def mean(self) -> float:
        return self.median * math.exp(self.sigma**2 / 2)

    @property
    def sd(self) -> float:
        return self.mean * math.sqrt(math.expm1(self.sigma**2))


@dataclass(frozen=True)
class IterativeStage:
    """The stage that counts steps, priced by the step model; ``steps`` is the count it is expected to show and ``reuse`` the
    reuse interval (H3's ``denoise_reuse`` or ``core_reuse``) under which only some of them are evaluated."""

    key: str
    stage: str
    source: str
    steps: int
    reuse: int = 1


StagePlan = FixedStage | IterativeStage


def _positive_pixels_frames(workload: Mapping[str, Any]) -> tuple[float, int] | None:
    pixels, frames = workload.get("pixels"), workload.get("frames")
    if not isinstance(pixels, int | float) or isinstance(pixels, bool) or not isinstance(frames, int) or isinstance(frames, bool) or frames <= 0:
        return None
    try:
        value = float(pixels)
    except OverflowError:
        return None
    return (value, frames) if math.isfinite(value) and value > 0 else None


def _insert_before(stages: list[StagePlan], before: str, item: FixedStage) -> None:
    index = next((index for index, stage in enumerate(stages) if stage.stage == before), len(stages))
    stages.insert(index, item)


def _insert_after(stages: list[StagePlan], after: str, item: FixedStage) -> None:
    index = next((index + 1 for index, stage in enumerate(stages) if stage.stage == after), len(stages))
    stages.insert(index, item)


def _h3_finishing_stages(stages: list[StagePlan], pixels: float, frames: int, spread: float, correcting: bool) -> None:
    """Fill the i2av-only stages at their real positions when a sparse row or r2av anchor lacks them."""
    anchor = H3_R2AV_E0051_E0054
    scale = pixels / (anchor.width * anchor.height) * frames / anchor.frames
    median_factor = math.exp(-(spread**2) / 2)
    if not any(stage.stage == "last_frame" for stage in stages):
        _insert_before(stages, "promote", FixedStage("last_frame", "last_frame", "framecontrol", H3_LAST_FRAME_SECONDS * scale * median_factor, spread))
    if correcting:
        if not any(stage.stage == "correct" for stage in stages):
            _insert_before(stages, "last_frame", FixedStage("correct", "correct", "framecontrol", H3_CORRECT_SECONDS * scale * median_factor, spread))
        if not any(stage.stage == "copy_raw" for stage in stages):
            next_stage = next((stage.stage for stage in stages if stage.stage in ("convert", "drift", "correct", "last_frame", "promote")), "correct")
            _insert_before(stages, next_stage, FixedStage("copy_raw", "copy_raw", "framecontrol", H3_COPY_RAW_SECONDS * scale * median_factor, spread))


@dataclass(frozen=True)
class Plan:
    """Everything a forecast needs to price one run, and the basis it rests on."""

    tier: str
    fidelity: str
    runs: int
    executions: int
    build_changed: bool
    skipped: int
    # The ridge penalty chosen by leave-one-execution-out error; None for the other tiers.
    penalty: float | None
    # The run's stages in order (empty only when no measured or heuristic plan can be built).
    stages: tuple[StagePlan, ...]
    # The log of the mean step time of this workload before the run's own speed factor, and the variance of that estimate.
    log_step: float | None
    log_step_var: float
    # The speed factor's spread (in the log) and the share of its variance that executions share.
    tau: float
    rho: float
    # Model error beyond the speed factor that a fit leaves (zero for tier 1).
    extra_var: float
    df: float
    # The backend's within-stage trend (the change of log step time across a stage), the first gap's ratio, the step-to-step
    # noise, the factor from steps times the mean step to the stage's time, and the spread of a stage not in the history.
    slope: float
    first_ratio: float | None
    step_noise: float
    stage_factor: float
    other_sd: float
    # How far a run's own slope strays from the backend's, which the steps still to come are uncertain by, and how far its first
    # gap strays from the ratio (in the log), which is how little that gap says of the rate.
    slope_sd: float
    first_sd: float
    # What a run's pauses add, as a share of the steps still to come: the allowance for the ones not yet seen.
    pause_share: float
    # Time between the named stages: the mean and spread of a run's ``unaccounted`` seconds.
    overhead_mean: float
    overhead_sd: float

    @property
    def prior_var(self) -> float:
        """The variance of the log step time about ``log_step``: the speed factor, the estimate's, and the model's."""
        return self.tau**2 + self.log_step_var + self.extra_var

    @property
    def shared_var(self) -> float:
        """The part of it the runs of one execution share: their machine's state, and the error of the estimate."""
        return self.rho * self.tau**2 + self.log_step_var


@dataclass(frozen=True)
class _Row:
    """A history run read once: its signature, features, mean step, and stages by key."""

    run: HistoryRun
    signature: Signature
    features: dict[str, float]
    series: StepSeries | None
    y: float | None
    keyed: dict[str, HistoryStage]
    unaccounted: float
    iterative_key: str | None
    stage_ratio: float | None
    # What the stage's pauses added, as a share of the stage at its normal pace.
    pause_ratio: float | None

    @property
    def execution(self) -> int:
        return self.run.execution


def _read_row(run: HistoryRun) -> _Row:
    stages = run.counted_stages
    keys = stage_keys(run)
    keyed = {key: stage for key, stage in zip(keys, stages, strict=True) if stage.stage != "unaccounted"}
    iterative = iterative_stage(run)
    interval = reuse_interval(run.workload)
    series = step_series(iterative, interval) if iterative is not None else None
    key = next((name for name, stage in keyed.items() if stage is iterative), None)
    y = math.log(series.mean_seconds) if series is not None and series.mean_seconds > 0 else None
    ratio = pause = None
    if series is not None and iterative is not None and iterative.total and series.mean_seconds > 0:
        evaluated_steps = evaluated_count(iterative.total, interval)
        ratio = min(2.0, iterative.duration / (evaluated_steps * series.mean_seconds))
        pause = min(2.0, series.pause_seconds / (evaluated_steps * series.mean_seconds))
    return _Row(run, run.signature, feature_values(run.workload, run.output_frames), series, y, keyed, sum(stage.duration for stage in stages if stage.stage == "unaccounted"), key, ratio, pause)


@dataclass(frozen=True)
class _Ridge:
    """A ridge fit of log step time on the features that vary in its training rows."""

    names: tuple[str, ...]
    constants: Mapping[str, float]
    mean: np.ndarray
    scale: np.ndarray
    beta: np.ndarray
    inverse: np.ndarray
    penalty: float
    cv_mse: float
    df: float
    rows: int
    executions: int

    def compatible(self, features: Mapping[str, float]) -> bool:
        """Whether a run can be priced by it: it holds every feature the fit used and every value a constant had. A knob
        that never varied in the data is not extrapolated, and a text value never seen is not guessed."""
        for name in self.names:
            if "=" not in name and name not in features:
                return False
        for name, value in self.constants.items():
            if "=" in name:
                if features.get(name, 0.0) != value:
                    return False
            elif name not in features or not math.isclose(features[name], value, rel_tol=1e-9, abs_tol=1e-9):
                return False
        known = set(self.names) | set(self.constants)
        return all(name in known for name, value in features.items() if "=" in name and value != 0.0)

    def predict(self, features: Mapping[str, float]) -> tuple[float, float]:
        """(the log mean step time, the variance of that estimate)."""
        values = np.array([features.get(name, 0.0) for name in self.names])
        design = np.concatenate(([1.0], (values - self.mean) / self.scale)) if self.names else np.array([1.0])
        return float(design @ self.beta), float(self.cv_mse * (design @ self.inverse @ design))


def _fit_ridge(rows: Sequence[_Row], features_of: Any = None) -> _Ridge | None:
    """Fit ``rows`` (all with a mean step). ``features_of`` gives a row's features, by default its own."""
    feature_dicts = [features_of(row) if features_of is not None else row.features for row in rows]
    keys = set().union(*feature_dicts)
    names = sorted(key for key in keys if "=" in key or all(key in values for values in feature_dicts))
    matrix = np.array([[values.get(name, 0.0) for name in names] for values in feature_dicts], dtype=float).reshape(len(rows), len(names))
    spread = matrix.std(axis=0) if names else np.array([])
    varying = [index for index in range(len(names)) if spread[index] > 1e-9]
    constants = {names[index]: float(matrix[0, index]) for index in range(len(names)) if index not in varying}
    kept = tuple(names[index] for index in varying)
    mean = matrix[:, varying].mean(axis=0) if varying else np.array([])
    scale = spread[varying] if varying else np.array([])
    design = np.hstack((np.ones((len(rows), 1)), (matrix[:, varying] - mean) / scale if varying else np.zeros((len(rows), 0))))
    y = np.array([row.y for row in rows], dtype=float)
    gram, moment = design.T @ design, design.T @ y
    penalty_matrix = np.diag([0.0] + [1.0] * len(kept))
    groups: dict[int, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[row.execution].append(index)
    parts = [(design[indices], y[indices]) for indices in groups.values()]
    folds = [(part.T @ part, part.T @ target, part, target) for part, target in parts]
    best: tuple[float, float] | None = None
    for penalty in PENALTIES:
        error = 0.0
        for fold_gram, fold_moment, part, target in folds:
            coefficients = np.linalg.solve(gram - fold_gram + penalty * penalty_matrix + 1e-9 * np.eye(len(gram)), moment - fold_moment)
            error += float(np.sum((target - part @ coefficients) ** 2))
        mse = error / len(rows)
        if best is None or mse <= best[0] * (1 + 1e-9):
            best = (mse, penalty)
    assert best is not None
    cv_mse, penalty = best
    inverse = np.linalg.inv(gram + penalty * penalty_matrix + 1e-9 * np.eye(len(gram)))
    beta = inverse @ moment
    effective = float(np.trace(inverse @ gram))
    return _Ridge(kept, constants, mean, scale, beta, inverse, penalty, max(cv_mse, MIN_VARIANCE), max(1.0, len(rows) - effective), len(rows), len(groups))


@dataclass
class _BackendStats:
    """What one backend's live history says that does not depend on the workload."""

    slope: float = 0.0
    slope_sd: float = DEFAULT_SLOPE_SD
    first_ratio: float | None = None
    first_sd: float = 0.0
    step_noise: float = DEFAULT_STEP_NOISE
    stage_factor: float = 1.0
    pause_share: float = 0.0
    tau: float = DEFAULT_TAU
    rho: float = DEFAULT_RHO
    other_sd: float = DEFAULT_OTHER_SD
    stage_sigma: float = DEFAULT_STAGE_SIGMA


def _spreads(rows: Sequence[_Row]) -> tuple[float, float]:
    """The speed factor's spread (``tau``) and the share of its variance that executions share (``rho``), from rows of the
    same signature grouped by execution; their defaults until three signatures have runs from two executions."""
    groups: dict[Signature, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        assert row.y is not None
        groups[row.signature][row.execution].append(row.y)
    usable = [by_execution for by_execution in groups.values() if len(by_execution) >= 2]
    if len(usable) < MIN_SPREAD_SIGNATURES:
        return DEFAULT_TAU, DEFAULT_RHO
    between = within = n0_weighted = 0.0
    df_between = df_within = 0
    for by_execution in usable:
        everything = [value for values in by_execution.values() for value in values]
        grand, total, count = sum(everything) / len(everything), len(everything), len(by_execution)
        means = {execution: sum(values) / len(values) for execution, values in by_execution.items()}
        between += sum(len(by_execution[execution]) * (means[execution] - grand) ** 2 for execution in by_execution)
        within += sum((value - means[execution]) ** 2 for execution, values in by_execution.items() for value in values)
        df_between += count - 1
        df_within += total - count
        # n0 of the one-way layout, weighted by this signature's degrees of freedom between executions.
        n0_weighted += total - sum(len(values) ** 2 for values in by_execution.values()) / total
    mean_between = between / df_between
    if df_within == 0:
        return max(MIN_TAU, math.sqrt(mean_between)), DEFAULT_RHO
    mean_within = within / df_within
    n0 = n0_weighted / df_between
    run_var = mean_within
    execution_var = max(0.0, (mean_between - mean_within) / n0) if n0 > 0 else 0.0
    total_var = run_var + execution_var
    rho = execution_var / total_var if total_var > MIN_VARIANCE else DEFAULT_RHO
    return max(MIN_TAU, math.sqrt(total_var)), min(0.95, max(0.05, rho))


def _slope(rows: Sequence[_Row]) -> float:
    """The backend's within-stage slope when its 95% interval over executions excludes zero; otherwise none."""
    by_execution: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if row.series is not None and row.series.slope is not None:
            by_execution[row.execution].append(row.series.slope)
    means = [sum(values) / len(values) for values in by_execution.values()]
    if len(means) < MIN_SLOPE_EXECUTIONS:
        return 0.0
    average = sum(means) / len(means)
    error = _stdev(means) / math.sqrt(len(means))
    return max(-MAX_SLOPE, min(MAX_SLOPE, average)) if abs(average) > student_t_975(len(means) - 1) * error else 0.0


def _stats(live: Sequence[_Row], all_rows: Sequence[_Row]) -> _BackendStats:
    stats = _BackendStats()
    series = [row.series for row in live if row.series is not None]
    if series:
        stats.step_noise = min(MAX_STEP_NOISE, max(0.01, statistics.median(item.noise for item in series)))
    ratios = [item.first_ratio for item in series if item.first_ratio is not None]
    if len(ratios) >= MIN_FIRST_GAP_RUNS:
        stats.first_ratio = statistics.median(ratios)
        stats.first_sd = max(MIN_FIRST_SD, _stdev([math.log(ratio) for ratio in ratios]))
    slopes = [item.slope for item in series if item.slope is not None]
    if len(slopes) >= MIN_SLOPE_RUNS:
        stats.slope_sd = min(MAX_SLOPE, max(MIN_SLOPE_SD, _stdev(slopes)))
    factors = [row.stage_ratio for row in live if row.stage_ratio is not None]
    if factors:
        stats.stage_factor = sum(factors) / len(factors)
    pauses = [row.pause_ratio for row in live if row.pause_ratio is not None]
    if pauses:
        stats.pause_share = sum(pauses) / len(pauses)
    if live:
        stats.tau, stats.rho = _spreads(live)
        stats.slope = _slope(live)
    durations: dict[tuple[str | None, str], list[float]] = defaultdict(list)
    for row in all_rows:
        for key, stage in row.keyed.items():
            if key != row.iterative_key:
                durations[(row.run.pipeline, key)].append(stage.duration)
    groups = [values for values in durations.values() if len(values) >= 2]
    if groups:
        stats.other_sd = math.sqrt(sum(_variance(values) for values in groups) / len(groups))
        logs = [_stdev([math.log(max(value, 1e-3)) for value in values]) for values in groups]
        stats.stage_sigma = max(MIN_STAGE_SIGMA, statistics.median(logs))
    return stats


class FittedModel:
    """The history, read once, and the plans it supports. ``skipped`` is the number of rows the caller could not read."""

    def __init__(self, history: Sequence[HistoryRun], *, skipped: int = 0) -> None:
        self.skipped = skipped
        self._rows: dict[str, list[_Row]] = defaultdict(list)
        self._runs: dict[str, list[HistoryRun]] = defaultdict(list)
        self._read: dict[int, _Row] = {}
        for run in history:
            try:
                row = _read_row(run)
            except Exception:
                # A row that cannot be read is left out and counted, never allowed to stop a forecast.
                self.skipped += 1
                continue
            self._rows[run.backend].append(row)
            self._runs[run.backend].append(run)
            self._read[id(run)] = row
        self._stats_cache: dict[str, _BackendStats] = {}
        self._fit_cache: dict[tuple[str, str | None, str, bool], _Ridge | None] = {}
        self._stage_cache: dict[tuple[str, str | None, str], tuple[float, float, float, float]] = {}
        self._frames: dict[Signature, int] = {}
        joined: dict[Signature, list[int]] = defaultdict(list)
        for rows in self._rows.values():
            for row in rows:
                if row.run.output_frames is not None:
                    joined[row.signature].append(row.run.output_frames)
        self._frames = {key: int(statistics.median(values)) for key, values in joined.items()}

    def _live(self, backend: str) -> list[_Row]:
        return [row for row in self._rows[backend] if row.run.fidelity == LIVE and row.y is not None]

    def _log(self, backend: str) -> list[_Row]:
        return [row for row in self._rows[backend] if row.run.fidelity != LIVE and row.y is not None]

    def _backend_stats(self, backend: str) -> _BackendStats:
        if backend not in self._stats_cache:
            self._stats_cache[backend] = _stats(self._live(backend), self._rows[backend])
        return self._stats_cache[backend]

    def plan(self, backend: str, pipeline: str | None, workload: Mapping[str, Any], environment: Mapping[str, Any]) -> Plan:
        """Price a run of ``backend`` and ``pipeline`` with this ``workload`` (as ``TimingStarted`` will record it) on the
        first tier whose history qualifies, and never from another backend's."""
        key = signature(backend, pipeline, workload, environment)
        frames = None if isinstance(workload.get("frames"), int | float) else self._frames.get(key)
        features = feature_values(workload, frames)
        stats = self._backend_stats(backend)
        live = self._live(backend)
        same = [row for row in live if row.signature == key]
        if len(same) >= MIN_SIGNATURE_RUNS and len({row.execution for row in same}) >= MIN_SIGNATURE_EXECUTIONS:
            return self._signature_plan(backend, pipeline, key, workload, environment, same, stats)
        for tier, scope in ((TIER_BACKEND, lambda row: row.run.pipeline == pipeline), (TIER_PIPELINE, lambda row: True)):
            for fidelity in (LIVE, POOLED):
                rows = [row for row in (live if fidelity == LIVE else live + self._log(backend)) if scope(row)]
                if fidelity == POOLED and len(rows) == len([row for row in live if scope(row)]):
                    continue
                if len(rows) < MIN_FIT_RUNS or len({row.execution for row in rows}) < MIN_FIT_EXECUTIONS:
                    continue
                indicator = tier == TIER_PIPELINE
                fit = self._ridge(backend, pipeline, tier, fidelity == POOLED, rows, indicator)
                request = {**features, f"pipeline={pipeline}": 1.0} if indicator and pipeline is not None else features
                if fit is None or not fit.compatible(request):
                    continue
                return self._fit_plan(backend, pipeline, key, workload, environment, tier, fidelity, rows, fit, request, stats)
        factory = factory_point(backend, pipeline, workload, environment)
        if factory is not None:
            return self._factory_plan(factory)
        heuristic = self._sparse_plan(backend, pipeline, key, workload, environment, stats) or self._heuristic_plan(backend, pipeline, workload)
        return heuristic if heuristic is not None else self._live_only_plan(stats)

    def _sparse_plan(self, backend: str, pipeline: str | None, key: Signature, workload: Mapping[str, Any], environment: Mapping[str, Any], stats: _BackendStats) -> Plan | None:
        """A small same-model sample is a better early guess than a different model's factory point."""
        dimensions, steps = _positive_pixels_frames(workload), workload.get("steps")
        model = environment.get("model")
        if dimensions is None or not isinstance(steps, int) or isinstance(steps, bool) or steps <= 0 or not isinstance(model, str) or not model:
            return None
        pixels, frames = dimensions
        candidates = [row for row in self._rows[backend] if row.run.pipeline == pipeline and row.run.environment.get("model") == model and row.y is not None and math.isfinite(row.y) and _positive_pixels_frames(row.run.workload) is not None]
        if not candidates:
            return None
        live = [row for row in candidates if row.run.fidelity == LIVE]
        candidates = live or candidates
        build_keys = ("executable_bytes", "executable_mtime")
        if all(environment.get(name) is not None for name in build_keys):
            same_build = [row for row in candidates if all(row.run.environment.get(name) == environment[name] for name in build_keys)]
            candidates = same_build or candidates
        same = [row for row in candidates if row.signature == key]
        if same:
            sample = max(same, key=lambda row: row.run.order)
            by_execution: dict[int, list[float]] = defaultdict(list)
            for row in same:
                assert row.y is not None
                by_execution[row.execution].append(row.y)
            rate = math.exp(statistics.mean(statistics.mean(values) for values in by_execution.values()))
            spread = 0.40
        else:
            sample = min(candidates, key=lambda row: (abs(math.log(pixels / row.run.workload["pixels"])) + abs(math.log(frames / row.run.workload["frames"])), -row.run.execution, -row.run.run))
            assert sample.y is not None
            frame_exponent = 1.6 if backend == H3_BACKEND else 1.0
            rate = math.exp(sample.y) * (pixels / sample.run.workload["pixels"]) * (frames / sample.run.workload["frames"]) ** frame_exponent
            spread = HEURISTIC_LOG_SPREAD
        if not math.isfinite(rate) or rate <= 0:
            return None
        build_changed = self._build_changed([sample], environment)
        if build_changed:
            spread = HEURISTIC_LOG_SPREAD
        stages: list[StagePlan] = []
        duration_scale = pixels / sample.run.workload["pixels"] * frames / sample.run.workload["frames"]
        stage_spread = HEURISTIC_STAGE_SPREAD
        for name, stage in sample.keyed.items():
            if name == sample.iterative_key:
                stages.append(IterativeStage(name, stage.stage, stage.source, steps, reuse_interval(workload)))
            elif stage.stage not in ("correct", "copy_raw") or workload.get("color_corrects"):
                duration = stage.duration * (duration_scale if stage.stage in ("ffmpeg", "finishing", "copy_raw", "correct") else 1.0)
                stages.append(FixedStage(name, stage.stage, stage.source, max(duration, 0.001) * math.exp(-(stage_spread**2) / 2), stage_spread))
        if backend == H3_BACKEND and pipeline == "i2av":
            _h3_finishing_stages(stages, float(pixels), frames, stage_spread, bool(workload.get("color_corrects")))
        elif backend == DRAW_THINGS_BACKEND and workload.get("color_corrects") and not any(stage.stage == "correct" for stage in stages):
            anchor = heuristic_anchor(backend)
            assert anchor is not None
            scale = pixels / (anchor.width * anchor.height) * frames / anchor.frames
            _insert_after(stages, "last_frame", FixedStage("correct", "correct", "framecontrol", 6.847 * scale * math.exp(-(stage_spread**2) / 2), stage_spread))
        if not any(isinstance(stage, IterativeStage) for stage in stages):
            return None
        prior_var = spread**2 + stats.tau**2
        return Plan(
            tier=TIER_HEURISTIC,
            fidelity=sample.run.fidelity,
            runs=len(same) if same else 1,
            executions=len({row.execution for row in same}) if same else 1,
            build_changed=build_changed,
            skipped=self.skipped,
            penalty=None,
            stages=tuple(stages),
            log_step=math.log(rate) - prior_var / 2,
            log_step_var=spread**2,
            tau=stats.tau,
            rho=stats.rho,
            extra_var=0.0,
            df=1.0,
            slope=stats.slope,
            first_ratio=stats.first_ratio,
            step_noise=stats.step_noise,
            stage_factor=stats.stage_factor,
            other_sd=max(stats.other_sd, rate * steps * 0.2),
            slope_sd=stats.slope_sd,
            first_sd=max(stats.first_sd, 0.15) if stats.first_ratio is not None else 0.0,
            pause_share=stats.pause_share,
            overhead_mean=max(0.0, sample.unaccounted),
            overhead_sd=max(stats.other_sd, sample.unaccounted * 0.5),
        )

    def _heuristic_plan(self, backend: str, pipeline: str | None, workload: Mapping[str, Any]) -> Plan | None:
        """Scale a backend's measured rate by pixels and effective frames when history cannot fit this workload.

        These exponents are only a starting guess. A broad predictive range and the run's own steps carry the forecast
        until enough matching history supports a fit.
        """
        anchor = heuristic_anchor(backend)
        dimensions, steps = _positive_pixels_frames(workload), workload.get("steps")
        if anchor is None or pipeline is None or dimensions is None or not isinstance(steps, int) or isinstance(steps, bool) or steps <= 0:
            return None
        pixels, frames = dimensions
        pixel_ratio = pixels / (anchor.width * anchor.height)
        frame_ratio = frames / anchor.frames
        # The H3 evidence at 5, 8, 10 and 15 seconds grows faster than linearly with frames.
        # A still-image job does not become 33 times cheaper than E0053's video merely because it has one output frame.
        frame_exponent = 1.6 if backend == H3_BACKEND else 0.5 if pipeline == "i2i" else 1.0
        rate = anchor.step_seconds * pixel_ratio * frame_ratio**frame_exponent
        if not math.isfinite(rate) or rate <= 0:
            return None
        fixed_spread = HEURISTIC_STAGE_SPREAD
        fixed_median_factor = math.exp(-(fixed_spread**2) / 2)
        stages: list[StagePlan] = []
        references = workload.get("reference_count")
        reference_count = references if isinstance(references, int) and not isinstance(references, bool) else 1
        for stage in anchor.stages:
            if stage.name == "correct" and not workload.get("color_corrects"):
                continue
            if backend == H3_BACKEND and stage.name in ("video_encode", "vision") and stage.key.endswith(("#2", "#3")) and int(stage.key[-1]) > reference_count:
                continue
            if stage.iterative:
                stages.append(IterativeStage(stage.key, stage.name, stage.source, steps, reuse_interval(workload)))
            else:
                duration = stage.seconds
                if stage.name in ("ffmpeg", "finishing", "correct"):
                    duration *= pixel_ratio * frame_ratio
                stages.append(FixedStage(stage.key, stage.name, stage.source, duration * fixed_median_factor, fixed_spread))
        if backend == H3_BACKEND and pipeline == "i2av":
            _h3_finishing_stages(stages, float(pixels), frames, fixed_spread, bool(workload.get("color_corrects")))
        log_spread = 1.0 if pipeline != anchor.pipeline else HEURISTIC_LOG_SPREAD
        prior_var = log_spread**2 + anchor.tau**2
        return Plan(
            tier=TIER_HEURISTIC,
            fidelity="heuristic",
            runs=0,
            executions=0,
            build_changed=False,
            skipped=self.skipped,
            penalty=None,
            stages=tuple(stages),
            log_step=math.log(rate) - prior_var / 2,
            log_step_var=log_spread**2,
            tau=anchor.tau,
            rho=anchor.rho,
            extra_var=0.0,
            df=1.0,
            slope=anchor.slope if pipeline == anchor.pipeline else 0.0,
            first_ratio=anchor.first_ratio if pipeline == anchor.pipeline else None,
            step_noise=max(anchor.step_noise, DEFAULT_STEP_NOISE),
            stage_factor=anchor.stage_factor,
            other_sd=max(anchor.other_sd, rate * steps * 0.2),
            slope_sd=anchor.slope_sd,
            first_sd=max(anchor.first_sd, 0.15),
            pause_share=0.0,
            overhead_mean=0.0,
            overhead_sd=0.0,
        )

    def _factory_plan(self, factory: DrawThingsFactoryPoint | H3FactoryPoint) -> Plan:
        stages: tuple[StagePlan, ...] = tuple(IterativeStage(stage.key, stage.name, stage.source, factory.steps) if stage.iterative else FixedStage(stage.key, stage.name, stage.source, stage.seconds, factory.spread) for stage in factory.stages)
        return Plan(
            tier=TIER_FACTORY,
            fidelity="factory",
            runs=0,
            executions=0,
            build_changed=False,
            skipped=self.skipped,
            penalty=None,
            stages=stages,
            log_step=math.log(factory.step_seconds),
            log_step_var=factory.spread**2,
            tau=factory.tau,
            rho=factory.rho,
            extra_var=0.0,
            df=1.0,  # One or two independent executions cannot estimate a narrower predictive tail.
            slope=factory.slope,
            first_ratio=factory.first_ratio,
            step_noise=factory.step_noise,
            stage_factor=factory.stage_factor,
            other_sd=factory.other_sd,
            slope_sd=factory.slope_sd,
            first_sd=factory.first_sd,
            pause_share=factory.pause_share,
            overhead_mean=0.0,
            overhead_sd=0.0,
        )

    def _ridge(self, backend: str, pipeline: str | None, tier: str, pooled: bool, rows: list[_Row], indicator: bool) -> _Ridge | None:
        cache_key = (backend, pipeline if tier == TIER_BACKEND else None, tier, pooled)
        if cache_key not in self._fit_cache:
            features_of = (lambda row: {**row.features, f"pipeline={row.run.pipeline}": 1.0}) if indicator else None
            self._fit_cache[cache_key] = _fit_ridge(rows, features_of)
        return self._fit_cache[cache_key]

    def _signature_plan(self, backend: str, pipeline: str | None, key: Signature, workload: Mapping[str, Any], environment: Mapping[str, Any], same: list[_Row], stats: _BackendStats) -> Plan:
        by_execution: dict[int, list[float]] = defaultdict(list)
        for row in same:
            assert row.y is not None
            by_execution[row.execution].append(row.y)
        means = [sum(values) / len(values) for values in by_execution.values()]
        log_step = sum(means) / len(means)
        scope = [row for row in self._rows[backend] if row.run.pipeline == pipeline]
        stages, overhead = self._stages(backend, pipeline, key, workload, same, scope)
        return Plan(
            tier=TIER_SIGNATURE,
            fidelity=LIVE,
            runs=len(same),
            executions=len(means),
            build_changed=self._build_changed(same, environment),
            skipped=self.skipped,
            penalty=None,
            stages=stages,
            log_step=log_step,
            log_step_var=stats.tau**2 / len(means),
            tau=stats.tau,
            rho=stats.rho,
            extra_var=0.0,
            df=max(1.0, len(same) - 1.0),
            slope=stats.slope,
            first_ratio=stats.first_ratio,
            step_noise=stats.step_noise,
            stage_factor=stats.stage_factor,
            other_sd=stats.other_sd,
            slope_sd=stats.slope_sd,
            first_sd=stats.first_sd,
            pause_share=stats.pause_share,
            overhead_mean=overhead[0],
            overhead_sd=overhead[1],
        )

    def _fit_plan(self, backend: str, pipeline: str | None, key: Signature, workload: Mapping[str, Any], environment: Mapping[str, Any], tier: str, fidelity: str, rows: list[_Row], fit: _Ridge, features: Mapping[str, float], stats: _BackendStats) -> Plan:
        log_step, variance = fit.predict(features)
        scope = [row for row in self._rows[backend] if tier == TIER_PIPELINE or row.run.pipeline == pipeline]
        stages, overhead = self._stages(backend, pipeline, key, workload, [], scope)
        return Plan(
            tier=tier,
            fidelity=fidelity,
            runs=fit.rows,
            executions=fit.executions,
            build_changed=self._build_changed(rows, environment),
            skipped=self.skipped,
            penalty=fit.penalty,
            stages=stages,
            log_step=log_step,
            log_step_var=variance,
            tau=stats.tau,
            rho=stats.rho,
            extra_var=max(0.0, fit.cv_mse - stats.tau**2),
            df=fit.df,
            slope=stats.slope,
            first_ratio=stats.first_ratio,
            step_noise=stats.step_noise,
            stage_factor=stats.stage_factor,
            other_sd=stats.other_sd,
            slope_sd=stats.slope_sd,
            first_sd=stats.first_sd,
            pause_share=stats.pause_share,
            overhead_mean=overhead[0],
            overhead_sd=overhead[1],
        )

    def _live_only_plan(self, stats: _BackendStats) -> Plan:
        return Plan(
            tier=TIER_LIVE,
            fidelity=LIVE,
            runs=0,
            executions=0,
            build_changed=False,
            skipped=self.skipped,
            penalty=None,
            stages=(),
            log_step=None,
            log_step_var=0.0,
            tau=stats.tau,
            rho=stats.rho,
            extra_var=0.0,
            df=1.0,
            slope=stats.slope,
            first_ratio=stats.first_ratio,
            step_noise=stats.step_noise,
            stage_factor=stats.stage_factor,
            other_sd=stats.other_sd,
            slope_sd=stats.slope_sd,
            first_sd=stats.first_sd,
            pause_share=stats.pause_share,
            overhead_mean=0.0,
            overhead_sd=0.0,
        )

    @staticmethod
    def _build_changed(rows: Sequence[_Row], environment: Mapping[str, Any]) -> bool:
        """Whether the newest run the plan rests on was timed on another build of the executable than the run will be."""
        newest = max(rows, key=lambda row: row.run.order).run.environment
        built = ("executable_bytes", "executable_mtime")
        if not all(name in newest and name in environment for name in built):
            return False
        return any(newest[name] != environment[name] for name in built)

    def _stages(self, backend: str, pipeline: str | None, key: Signature, workload: Mapping[str, Any], same: list[_Row], scope: list[_Row]) -> tuple[tuple[StagePlan, ...], tuple[float, float]]:
        runs = [run for run in self._runs[backend] if run.fidelity == LIVE] or self._runs[backend]
        sequence = expected_run(runs, key, backend, pipeline)
        if sequence is None:
            return (), (0.0, 0.0)
        read = self._read[id(sequence)]
        stats = self._backend_stats(backend)
        planned: list[StagePlan] = []
        features = feature_values(workload, self._frames.get(key))
        for name, stage in read.keyed.items():
            if name == read.iterative_key:
                planned.append(IterativeStage(name, stage.stage, stage.source, self._steps(workload, sequence, stage), reuse_interval(workload)))
                continue
            samples = [row.keyed[name].duration for row in same if name in row.keyed]
            median, sigma = self._duration(backend, pipeline, name, features, samples, scope, stage.duration, stats.stage_sigma)
            planned.append(FixedStage(name, stage.stage, stage.source, median, sigma))
        totals = [row.unaccounted for row in (same if len(same) >= MIN_STAGE_SAMPLES else scope)]
        overhead = (sum(totals) / len(totals), _pstdev(totals)) if totals else (0.0, 0.0)
        return tuple(planned), overhead

    @staticmethod
    def _steps(workload: Mapping[str, Any], sequence: HistoryRun, stage: HistoryStage) -> int:
        """The count the iterative stage shows: the sequence run's, scaled when the workload asks for another number of steps."""
        shown = stage.total or stage.done or 1
        asked, was = workload.get("steps"), sequence.workload.get("steps")
        if isinstance(asked, int) and isinstance(was, int) and was > 0 and asked != was:
            return max(1, round(shown * asked / was))
        return shown

    def _duration(self, backend: str, pipeline: str | None, name: str, features: Mapping[str, float], samples: list[float], scope: list[_Row], fallback: float, sigma_default: float) -> tuple[float, float]:
        """A non-iterative stage's median and log spread: the signature's own median from three samples, else a log-linear
        fit on pixels and frames over the scope, else the scope's median, else the one run that has it."""
        if len(samples) >= MIN_STAGE_SAMPLES:
            logs = [math.log(max(value, 1e-3)) for value in samples]
            return statistics.median(samples), max(MIN_STAGE_SIGMA, _stdev(logs))
        rows = [row for row in scope if name in row.keyed and "log_pixels" in row.features]
        if len(rows) >= 4 and len({row.features["log_pixels"] for row in rows}) >= 2:
            if "log_pixels" in features:
                intercept, pixel, frame, sigma = self._stage_fit(backend, pipeline, name, rows)
                value = intercept + pixel * features["log_pixels"] + frame * features.get("log_frames", 0.0)
                return math.exp(value), max(MIN_STAGE_SIGMA, sigma)
        durations = [row.keyed[name].duration for row in scope if name in row.keyed]
        return (statistics.median(durations) if durations else fallback), sigma_default

    def _stage_fit(self, backend: str, pipeline: str | None, name: str, rows: list[_Row]) -> tuple[float, float, float, float]:
        """The log of the stage's duration fitted on log pixels and log frames, with a small ridge on the slopes: its
        intercept, the two coefficients, and the residual spread."""
        cache_key = (backend, pipeline, name)
        if cache_key not in self._stage_cache:
            design = np.array([[1.0, row.features["log_pixels"], row.features.get("log_frames", 0.0)] for row in rows])
            target = np.log(np.maximum([row.keyed[name].duration for row in rows], 1e-3))
            gram = design.T @ design + np.diag([0.0, 0.1, 0.1]) + 1e-9 * np.eye(3)
            coefficients = np.linalg.solve(gram, design.T @ target)
            residual = target - design @ coefficients
            sigma = float(math.sqrt(float(residual @ residual) / max(1, len(rows) - 3)))
            self._stage_cache[cache_key] = (float(coefficients[0]), float(coefficients[1]), float(coefficients[2]), sigma)
        return self._stage_cache[cache_key]


__all__ = ["PENALTIES", "FittedModel", "FixedStage", "IterativeStage", "Plan", "StagePlan", "student_t_90", "student_t_975"]
