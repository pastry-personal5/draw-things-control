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

from framecontrol.jobs.timing.factory import DrawThingsFactoryPoint, H3FactoryPoint, factory_point
from framecontrol.jobs.timing.features import HistoryRun, HistoryStage, Signature, StepSeries, expected_run, feature_values, iterative_stage, signature, stage_keys, step_series

TIER_SIGNATURE, TIER_BACKEND, TIER_PIPELINE, TIER_FACTORY, TIER_LIVE = "signature", "backend_fit", "pipeline_fit", "factory", "live_only"
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
# The least spread, in the log, the first gap's ratio to the later steps is taken to have from one run to the next.
MIN_FIRST_SD = 0.02
# The spread of a stage duration in the log, when no history says, and the least it is taken to be.
DEFAULT_STAGE_SIGMA, MIN_STAGE_SIGMA = 0.15, 0.03
# Seconds of spread for a stage the history has not seen, when the backend has no stage history at all.
DEFAULT_OTHER_SD = 20.0
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
    """The stage that counts steps, priced by the step model; ``steps`` is the count it is expected to show."""

    key: str
    stage: str
    source: str
    steps: int


StagePlan = FixedStage | IterativeStage


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
    # The run's stages in order (none for tier 4: with no history that qualifies, nothing after the steps is invented).
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
    series = step_series(iterative) if iterative is not None else None
    key = next((name for name, stage in keyed.items() if stage is iterative), None)
    y = math.log(series.mean_seconds) if series is not None and series.mean_seconds > 0 else None
    ratio = pause = None
    if series is not None and iterative is not None and iterative.total and series.mean_seconds > 0:
        ratio = min(2.0, iterative.duration / (iterative.total * series.mean_seconds))
        pause = min(2.0, series.pause_seconds / (iterative.total * series.mean_seconds))
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
    return average if abs(average) > student_t_975(len(means) - 1) * error else 0.0


def _stats(live: Sequence[_Row], all_rows: Sequence[_Row]) -> _BackendStats:
    stats = _BackendStats()
    series = [row.series for row in live if row.series is not None]
    if series:
        stats.step_noise = max(0.01, statistics.median(item.noise for item in series))
    ratios = [item.first_ratio for item in series if item.first_ratio is not None]
    if len(ratios) >= MIN_FIRST_GAP_RUNS:
        stats.first_ratio = statistics.median(ratios)
        stats.first_sd = max(MIN_FIRST_SD, _stdev([math.log(ratio) for ratio in ratios]))
    slopes = [item.slope for item in series if item.slope is not None]
    if len(slopes) >= MIN_SLOPE_RUNS:
        stats.slope_sd = max(MIN_SLOPE_SD, _stdev(slopes))
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
        return self._factory_plan(factory) if factory is not None else self._live_only_plan(stats)

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
                planned.append(IterativeStage(name, stage.stage, stage.source, self._steps(workload, sequence, stage)))
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
