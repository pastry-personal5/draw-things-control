"""The replay: stored runs fed through an estimator as a live stream, and what it says scored against what happened.

A recorded execution is turned back into the events the executor emitted (``jobs/timing/replay_stream.py``), and an estimator
reads them one at a time and is asked, at checkpoints, how long the run and the job have left. The checkpoints are 5%, 10%,
25%, 50%, 75% and 90% of each run's true duration for the run, and 10%, 25%, 50%, 75% and 90% of the job's (the runs and the
waits between them) for the job. The metrics are the median and 90th-percentile absolute percentage error of the time left,
its signed bias, the coverage of the 80% range, the time to the first number, and the number of times the displayed share moves
back. A fit never includes the execution it is scored on, because runs of one execution share a machine state: leave one
execution out, or, for a set that must be quick, group executions into folds and leave a fold out. The forecast is read the way
a client reads it, from what the publisher last published. The estimators here are the forecast and two simple ones to beat;
the frozen estimator the product used before lives in the tests, which score a real store with it."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobEvent, RunFinished, RunOutput, RunStarted
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun, Signature
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.forecast import Forecaster, JobPlan, PlannedRun
from framecontrol.jobs.timing.parsers import STEP_STAGES
from framecontrol.jobs.timing.replay_stream import replay_job
from framecontrol.services.forecast_publisher import ForecastPublisher
from framecontrol.services.forecast_view import progress, seconds_left
from framecontrol.services.timing_history import load_executions
from framecontrol.state.store import Store, StoreMode

CHECKPOINTS_RUN = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90)
CHECKPOINTS_JOB = (0.10, 0.25, 0.50, 0.75, 0.90)
# A run is judged against its own signature when this much of it is in the history.
QUALIFIED_RUNS, QUALIFIED_EXECUTIONS = 3, 2
# The share moving back by less than this is rounding.
SHARE_TOLERANCE = 1e-9
# A cell with fewer runs than this is listed and not judged.
MIN_JUDGED_RUNS = 8
BOOTSTRAP_RESAMPLES = 1000


@dataclass(frozen=True)
class Interval:
    """Seconds left, and the range the estimator gave for them where it gave one."""

    left: float
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class Reading:
    """What an estimator shows at one moment: the run's and the job's time left, and the share of each done (or None)."""

    run: Interval | None
    job: Interval | None
    run_share: float | None = None
    job_share: float | None = None


class ReplayEstimator(Protocol):
    def observe(self, event: JobEvent, now: float) -> None: ...

    def reading(self, now: float) -> Reading: ...


@dataclass(frozen=True)
class JobContext:
    """What an estimator is built from for one replayed job: the job (its runs described as they will be recorded), the
    history it may learn from (never its own execution), and the model fitted on that history."""

    execution: HistoryExecution
    first_run: int
    training: Sequence[HistoryRun]
    model: FittedModel


EstimatorFactory = Callable[[JobContext], ReplayEstimator]


@dataclass(frozen=True)
class Observation:
    """One estimate at one checkpoint, against the truth."""

    level: str
    backend: str
    pipeline: str | None
    checkpoint: float
    execution: int
    run: int | None
    truth: float
    estimate: float | None
    low: float | None
    high: float | None
    qualified: bool


@dataclass(frozen=True)
class Trace:
    """What happened over one run: when the first number came, and how often the share moved back."""

    backend: str
    pipeline: str | None
    execution: int
    run: int
    qualified: bool
    first_number: float | None
    run_moves_back: int


@dataclass
class ReplayReport:
    observations: list[Observation] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)
    job_moves_back: dict[int, int] = field(default_factory=dict)


# ------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Cell:
    """The metrics of the observations of one group; a key it was not grouped by is None."""

    level: str | None
    backend: str | None
    pipeline: str | None
    checkpoint: float | None
    runs: int
    missing: int
    median_ape: float
    p90_ape: float
    bias: float
    coverage: float | None


def percentile(values: Sequence[float], quantile: float) -> float:
    """The ``quantile`` (0 to 1) of ``values`` by linear interpolation between the nearest ranks."""
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)


def cells_of(observations: Sequence[Observation], keys: Sequence[str]) -> list[Cell]:
    """Group ``observations`` by ``keys`` (any of level, backend, pipeline, checkpoint) and measure each group."""
    groups: dict[tuple[object, ...], list[Observation]] = defaultdict(list)
    for item in observations:
        groups[tuple(getattr(item, key) for key in keys)].append(item)
    cells = []
    for group, items in sorted(groups.items(), key=lambda entry: tuple(str(part) for part in entry[0])):
        given = [item for item in items if item.estimate is not None and item.truth > 0]
        if not given:
            continue
        errors = [(item.estimate - item.truth) / item.truth for item in given if item.estimate is not None]
        ranged = [item for item in given if item.low is not None and item.high is not None]
        covered = sum(1 for item in ranged if item.low is not None and item.high is not None and item.low <= item.truth <= item.high)
        named = dict(zip(keys, group, strict=True))
        level, backend, pipeline, checkpoint = (named.get(key) for key in ("level", "backend", "pipeline", "checkpoint"))
        cells.append(
            Cell(
                level=level if isinstance(level, str) else None,
                backend=backend if isinstance(backend, str) else None,
                pipeline=pipeline if isinstance(pipeline, str) else None,
                checkpoint=checkpoint if isinstance(checkpoint, float) else None,
                runs=len(given),
                missing=len(items) - len(given),
                median_ape=statistics.median(abs(error) for error in errors),
                p90_ape=percentile([abs(error) for error in errors], 0.9),
                bias=statistics.median(errors),
                coverage=covered / len(ranged) if ranged else None,
            )
        )
    return cells


def bootstrap_interval(values: Sequence[float], seed: int = 1, *, resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float]:
    """The 90% bootstrap interval of the median of ``values``."""
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(resamples))
    return medians[int(0.05 * resamples)], medians[int(0.95 * resamples) - 1]


def qualified(run: HistoryRun, training: Sequence[HistoryRun]) -> bool:
    """Whether ``training`` holds at least 3 other runs of ``run``'s signature from at least 2 other executions."""
    same = [item for item in training if item.signature == run.signature and item.fidelity == "live" and item.execution != run.execution]
    return len(same) >= QUALIFIED_RUNS and len({item.execution for item in same}) >= QUALIFIED_EXECUTIONS


class _SignatureIndex:
    """How many runs, from how many executions, the training history holds of each signature: ``qualified`` for many runs."""

    def __init__(self, training: Sequence[HistoryRun]) -> None:
        self._counts: dict[Signature, tuple[int, set[int]]] = {}
        for run in training:
            if run.fidelity == "live":
                count, executions = self._counts.setdefault(run.signature, (0, set()))
                executions.add(run.execution)
                self._counts[run.signature] = (count + 1, executions)

    def qualified(self, run: HistoryRun) -> bool:
        count, executions = self._counts.get(run.signature, (0, set()))
        return count >= QUALIFIED_RUNS and len(executions) >= QUALIFIED_EXECUTIONS


# -------------------------------------------------------------------- replay


def _score(execution: HistoryExecution, training: Sequence[HistoryRun], model: FittedModel, factory: EstimatorFactory, index: _SignatureIndex, report: ReplayReport, backend: str | None, run_checkpoints: Sequence[float] = CHECKPOINTS_RUN) -> None:
    job = replay_job(execution, origin=0.0)
    estimator = factory(JobContext(execution, 1, training, model))
    runs = {run.run: run for run in execution.runs}
    marks: list[tuple[float, str, int | None, float]] = []
    for number, start in job.starts.items():
        end = job.ends[number]
        marks.extend((start + share * (end - start), "run", number, share) for share in run_checkpoints)
    marks.extend((job.started + share * (job.finished - job.started), "job", None, share) for share in CHECKPOINTS_JOB)
    marks.sort(key=lambda mark: mark[0])
    first_numbers: dict[int, float | None] = {number: None for number in runs if number in job.starts}
    moves: dict[int, int] = defaultdict(int)
    last_share: dict[int, float] = {}
    job_moves, job_last = 0, None
    current: int | None = None
    pending = 0

    def record(reading: Reading, when: float, level: str, number: int | None, share: float) -> None:
        run = runs[number] if number is not None else execution.runs[0]
        wanted = backend is None or run.backend == backend
        if not wanted:
            return
        truth = (job.ends[number] if number is not None else job.finished) - when
        interval = reading.run if level == "run" else reading.job
        report.observations.append(Observation(level, run.backend, run.pipeline, share, execution.number, number, truth, interval.left if interval else None, interval.low if interval else None, interval.high if interval else None, index.qualified(run)))

    for when, event in job.events:
        while pending < len(marks) and marks[pending][0] < when:
            at, level, number, share = marks[pending]
            record(estimator.reading(at), at, level, number, share)
            pending += 1
        estimator.observe(event, when)
        if isinstance(event, RunStarted):
            current = event.number
        elif isinstance(event, RunFinished):
            current = None
        reading = estimator.reading(when)
        if current is not None and reading.run is not None and first_numbers.get(current) is None:
            first_numbers[current] = when - job.starts[current]
        if current is not None and reading.run_share is not None:
            if current in last_share and reading.run_share < last_share[current] - SHARE_TOLERANCE:
                moves[current] += 1
            last_share[current] = reading.run_share
        if reading.job_share is not None:
            if job_last is not None and reading.job_share < job_last - SHARE_TOLERANCE:
                job_moves += 1
            job_last = reading.job_share
    for at, level, number, share in marks[pending:]:
        record(estimator.reading(at), at, level, number, share)
    for number, run in runs.items():
        if number in job.starts and (backend is None or run.backend == backend):
            report.traces.append(Trace(run.backend, run.pipeline, execution.number, number, index.qualified(run), first_numbers.get(number), moves[number]))
    report.job_moves_back[execution.number] = job_moves


def score_execution(execution: HistoryExecution, training: Sequence[HistoryRun], model: FittedModel, factory: EstimatorFactory, report: ReplayReport, backend: str | None = None, run_checkpoints: Sequence[float] = CHECKPOINTS_RUN) -> None:
    """Score one execution against ``model``, adding its observations to ``report`` (for a replay that builds its own models,
    such as the knob learning curve, where each execution is judged by what was learned before it)."""
    _score(execution, training, model, factory, _SignatureIndex(training), report, backend, run_checkpoints)


def replay_leave_one_out(executions: Sequence[HistoryExecution], factory: EstimatorFactory, *, backend: str | None = None) -> ReplayReport:
    """Score every execution (those with a run of ``backend``, if given) against a history of all the others."""
    report = ReplayReport()
    for execution in executions:
        if backend is not None and not any(run.backend == backend for run in execution.runs):
            continue
        training = [run for other in executions if other.number != execution.number for run in other.runs]
        _score(execution, training, FittedModel(training), factory, _SignatureIndex(training), report, backend)
    return report


def replay_folds(executions: Sequence[HistoryExecution], factory: EstimatorFactory, *, folds: int = 10, per_fold: int = 40, seed: int = 1, backend: str | None = None) -> ReplayReport:
    """Group the executions into ``folds`` and score each against a history without its fold. In a fold, executions are taken
    in a seeded order until they hold ``per_fold`` runs, so the set is quick and the same every time."""
    rng = random.Random(seed)
    order = list(executions)
    rng.shuffle(order)
    groups = [order[index::folds] for index in range(folds)]
    report = ReplayReport()
    for group in groups:
        left_out = {execution.number for execution in group}
        training = [run for other in executions if other.number not in left_out for run in other.runs]
        model, index = FittedModel(training), _SignatureIndex(training)
        scored = 0
        for execution in group:
            if scored >= per_fold:
                break
            if backend is not None and not any(run.backend == backend for run in execution.runs):
                continue
            _score(execution, training, model, factory, index, report, backend)
            scored += len(execution.runs)
    return report


# --------------------------------------------------------------- estimators


class ForecastEstimator:
    """The product's forecast, read as a client reads it: from the last one published, with its own clock."""

    def __init__(self, context: JobContext) -> None:
        plan = JobPlan(tuple(PlannedRun(run.run, run.backend, run.pipeline, dict(run.workload), dict(run.environment)) for run in context.execution.runs if run.run >= context.first_run), context.execution.cooldown)
        self._publisher = ForecastPublisher(Forecaster(context.model, plan), lambda forecast: None, clock=lambda: 0.0)

    def observe(self, event: JobEvent, now: float) -> None:
        self._publisher.observe(event, now)

    def reading(self, now: float) -> Reading:
        forecast = self._publisher.latest
        if forecast is None:
            return Reading(None, None)
        run, job = forecast.run, forecast.job
        return Reading(
            Interval(seconds_left(run.as_dict(), now), max(0.0, run.low - now), max(0.0, run.high - now)) if run else None,
            Interval(seconds_left(job.as_dict(), now), max(0.0, job.low - now), max(0.0, job.high - now)) if job else None,
            progress(run.as_dict(), now) if run else None,
            progress(job.as_dict(), now) if job else None,
        )


class _Tracker:
    """The part of reading a job's events the simple estimators share: which run is going, when, and the cooldown."""

    def __init__(self, context: JobContext) -> None:
        self.policy = context.execution.cooldown
        self.numbers = [run.run for run in context.execution.runs if run.run >= context.first_run]
        self.current: int | None = None
        self.origin = 0.0
        self.started = 0.0
        self.last = 0
        self.wait_until: float | None = None
        self.finished_full: list[float] = []
        self.finished_child: list[float] = []
        self.signature: Signature | None = None

    def track(self, event: JobEvent, now: float) -> None:
        if isinstance(event, RunStarted):
            self.current, self.origin, self.wait_until = event.number, now, None
            if not self.last:
                self.started = now
        elif isinstance(event, RunFinished):
            self.last, self.current = event.number, None
            self.finished_full.append(now - self.origin)
            self.finished_child.append(event.seconds or 0.0)
        elif isinstance(event, CooldownStarted):
            self.wait_until = now + event.seconds
        elif isinstance(event, CooldownEnded):
            self.wait_until = None

    def later(self) -> list[int]:
        done = self.current if self.current is not None else self.last
        return [number for number in self.numbers if number > done]


class LastSignatureEstimator:
    """ "The last run of the same signature": a run takes what the newest run of its signature (else of its backend and
    pipeline) took, and a wait is the policy's for that run's child time."""

    def __init__(self, context: JobContext) -> None:
        self._tracker = _Tracker(context)
        newest: dict[object, HistoryRun] = {}
        for run in sorted(context.training, key=lambda item: item.order):
            newest[run.signature] = run
            newest[(run.backend, run.pipeline)] = run
        self._references = {run.run: newest.get(run.signature) or newest.get((run.backend, run.pipeline)) for run in context.execution.runs}

    def observe(self, event: JobEvent, now: float) -> None:
        self._tracker.track(event, now)

    def reading(self, now: float) -> Reading:
        tracker = self._tracker
        later = tracker.later()
        references = [self._references.get(number) for number in later]
        run = job = None
        total = 0.0
        if None not in references:
            for position, reference in enumerate(references):
                assert reference is not None
                total += reference.seconds
                if position < len(references) - 1:
                    total += tracker.policy.wait_after(reference.child_seconds).seconds
        if tracker.current is not None:
            current = self._references.get(tracker.current)
            if current is not None:
                left = max(0.0, current.seconds - (now - tracker.origin))
                run = Interval(left)
                if None not in references:
                    job = Interval(left + (tracker.policy.wait_after(current.child_seconds).seconds if later else 0.0) + total)
        elif None not in references and later:
            wait = max(tracker.wait_until - now, 0.0) if tracker.wait_until is not None else 0.0
            job = Interval(wait + total)
        return Reading(run, job)


class ConstantPaceEstimator:
    """A constant pace: once the step counter has two readings, the steps left at the pace since the first, and a later run
    at the time this job's finished runs took (else this run's projected time)."""

    def __init__(self, context: JobContext) -> None:
        self._tracker = _Tracker(context)
        self._readings: list[tuple[float, int, int]] = []

    def observe(self, event: JobEvent, now: float) -> None:
        self._tracker.track(event, now)
        if isinstance(event, RunStarted):
            self._readings = []
        elif isinstance(event, RunOutput) and event.stage in STEP_STAGES and event.done is not None and event.total:
            if not self._readings or event.done > self._readings[-1][1]:
                self._readings.append((now, event.done, event.total))

    def reading(self, now: float) -> Reading:
        tracker = self._tracker
        later = tracker.later()
        if tracker.current is None:
            if not later or not tracker.finished_full:
                return Reading(None, None)
            wait = max(tracker.wait_until - now, 0.0) if tracker.wait_until is not None else 0.0
            full = sum(tracker.finished_full) / len(tracker.finished_full)
            child = sum(tracker.finished_child) / len(tracker.finished_child)
            return Reading(None, Interval(wait + len(later) * full + (len(later) - 1) * tracker.policy.wait_after(child).seconds))
        if len(self._readings) < 2:
            return Reading(None, None)
        (first_at, first_done, total), (last_at, last_done, _) = self._readings[0], self._readings[-1]
        pace = (last_at - first_at) / (last_done - first_done)
        left = max(0.0, (total - last_done) * pace - (now - last_at))
        full = sum(tracker.finished_full) / len(tracker.finished_full) if tracker.finished_full else (now - tracker.origin) + left
        child = sum(tracker.finished_child) / len(tracker.finished_child) if tracker.finished_child else full
        job = left + len(later) * full + len(later) * tracker.policy.wait_after(child).seconds
        elapsed = now - tracker.started
        return Reading(Interval(left), Interval(job), (now - tracker.origin) / ((now - tracker.origin) + left) if left >= 0 else None, elapsed / (elapsed + job) if elapsed + job > 0 else None)


BUILTIN_ESTIMATORS: Mapping[str, EstimatorFactory] = {"constant-pace": ConstantPaceEstimator, "last-signature": LastSignatureEstimator, "forecast": ForecastEstimator}


ESTIMATOR_NAMES = tuple(BUILTIN_ESTIMATORS)


def _percent(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "-"


def _intervals(observations: Sequence[Observation], keys: Sequence[str]) -> dict[tuple[object, ...], tuple[float, float]]:
    """The 90% bootstrap interval of the median error of each group of at least ``MIN_JUDGED_RUNS`` estimates."""
    groups: dict[tuple[object, ...], list[float]] = defaultdict(list)
    for item in observations:
        if item.estimate is not None and item.truth > 0:
            groups[tuple(getattr(item, key) for key in keys)].append(abs(item.estimate - item.truth) / item.truth)
    return {group: bootstrap_interval(errors) for group, errors in groups.items() if len(errors) >= MIN_JUDGED_RUNS}


def report_lines(report: ReplayReport, estimator: str, executions: int, runs: int) -> list[str]:
    """The metrics a replay measured, as the command prints them: for each backend and pipeline, the run and the job at each
    checkpoint, with the interval of the median where there are enough runs to judge, and the first number and the share."""
    lines = [f"Replay with {estimator}: {executions} execution{'s' if executions != 1 else ''}, {runs} run{'s' if runs != 1 else ''}, each scored with its own execution left out"]
    keys = ("level", "backend", "pipeline", "checkpoint")
    intervals = _intervals(report.observations, keys)
    for level in ("run", "job"):
        groups = sorted({(cell.backend, cell.pipeline) for cell in cells_of([item for item in report.observations if item.level == level], ("backend", "pipeline"))}, key=lambda group: (str(group[0]), str(group[1])))
        for backend, pipeline in groups:
            lines += ["", f"{level} {backend} {pipeline}", f"  {'checkpoint':>10}  {'runs':>5}  {'median':>8}  {'90% interval':>16}  {'p90':>8}  {'bias':>8}  {'coverage':>8}"]
            for cell in cells_of([item for item in report.observations if item.level == level and item.backend == backend and item.pipeline == pipeline], keys):
                interval = intervals.get((level, backend, pipeline, cell.checkpoint))
                span = f"{_percent(interval[0])} to {_percent(interval[1])}" if interval is not None else ""
                judged = "" if interval is not None else f"  not judged (fewer than {MIN_JUDGED_RUNS} runs)"
                coverage = f"{cell.coverage:.2f}" if cell.coverage is not None else "-"
                lines.append(f"  {f'{cell.checkpoint * 100:.0f}%' if cell.checkpoint is not None else '':>10}  {cell.runs:>5}  {_percent(cell.median_ape):>8}  {span:>16}  {_percent(cell.p90_ape):>8}  {_percent(cell.bias):>8}  {coverage:>8}{judged}")
    firsts = sorted(trace.first_number for trace in report.traces if trace.first_number is not None)
    middle = f", median {firsts[len(firsts) // 2]:.1f} s" if firsts else ""
    lines += ["", f"time to the first number: {len(firsts)}/{len(report.traces)} runs{middle}", f"share moved back: {sum(trace.run_moves_back for trace in report.traces)} times in runs, {sum(report.job_moves_back.values())} in jobs"]
    return lines


def replay_database(database: Path, estimator: str, *, backend: str | None = None) -> list[str]:
    """Replay the runs of the state database at ``database`` through ``estimator``, leaving out the execution each is scored
    on, and return the lines to print. The store is only read (opened as the history browses it), and needs no server."""
    if not database.exists():
        return ["No timing history to replay: there is no state database yet"]
    store = Store.open(database, mode=StoreMode.BROWSE)
    try:
        executions, skipped = load_executions(store)
    finally:
        store.close()
    scored = [execution for execution in executions if backend is None or any(run.backend == backend for run in execution.runs)]
    if not scored:
        return [f"No complete {backend} runs to replay" if backend is not None else "No timing history to replay: no run has complete timing yet"]
    report = replay_leave_one_out(executions, BUILTIN_ESTIMATORS[estimator], backend=backend)
    lines = report_lines(report, estimator, len(scored), sum(len(execution.runs) for execution in scored))
    if skipped:
        lines.append(f"{skipped} timing row{'s' if skipped != 1 else ''} could not be read and {'were' if skipped != 1 else 'was'} left out")
    return lines
