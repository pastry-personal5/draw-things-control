"""The forecaster: when the running job will end, as a pure function of its events and the fitted model.

``Forecaster.observe`` takes each job event with the time it arrived and ``forecast`` says what is expected at a time. A run
is a sum of stages; the expected sequence and each stage's duration come from ``FittedModel.plan``. The iterative stage's
pace is the plan's step time scaled by this run's speed factor ``u``, whose posterior is updated by the normal-normal
conjugate rule after each step of the active stage, so the weight of history falls as steps accumulate. A step over three
times the median of the steps so far (three times the predicted step before three exist) is a pause: it counts toward
elapsed time and not toward the rate. A step still running past its prediction is conditioned on having lasted this long,
so the end moves and the range widens. The job adds the runs still to come, the waits between them (the policy applied to
the run's predicted child time), and the state the runs of an execution share. The range is the 80% interval from a
Student-t quantile, summed in seconds. Nothing here reads a file, a database, or the clock."""

from __future__ import annotations

import math
import statistics
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted, RunStatus, StageFinished, StageStarted, TimingStarted
from framecontrol.jobs.timing.features import Signature, evaluated, evaluated_between, signature
from framecontrol.jobs.timing.fit import FittedModel, FixedStage, IterativeStage, Plan, StagePlan, student_t_90
from framecontrol.jobs.timing.knobs import Tunables
from framecontrol.jobs.timing.model import SOURCE_CHILD
from framecontrol.jobs.timing.parsers import STEP_STAGES

# A step over ``Tunables.pause_factor`` times the median of the steps so far is a pause (the model's tunables decide).
# Medians over fewer steps than this are not trusted: the predicted step stands in.
MIN_MEDIAN_STEPS = 3
# However wide a range comes out, a client is never given an end further than this past the estimate (a month).
MAX_REACH_SECONDS = 30 * 86400.0
# The spread of one step's time that an overrunning step is conditioned with, whatever the history's noise says: a step
# that takes far longer than expected is the case that history rarely contains.
OVERRUN_SIGMA = 0.3
# The spread, in the log, of a stage's duration before any step of it has been read.
WHOLE_STAGE_SIGMA = 0.1
# A run priced from an earlier run of this job alone is this uncertain, as a share of its time.
ANALOG_SPREAD = 0.1
# Units a stage that counts any must have shown before it is projected from its own pace.
MIN_PACE_UNITS = 2
# An unfamiliar stage needs an actual duration allowance even when the known stages have zero observed variation.
DEFAULT_OTHER_STAGE_SECONDS = 20.0


@dataclass(frozen=True)
class Span:
    """When something began and is expected to end, in epoch seconds, with the 80% range and the share already shown."""

    started_at: float
    end_at: float
    low: float
    high: float
    floor: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {"started_at": self.started_at, "end_at": self.end_at, "low": self.low, "high": self.high, "floor": self.floor}


@dataclass(frozen=True)
class Basis:
    """What a forecast rests on: the tier, the history's fidelity, how many runs and executions, whether the newest of them
    was timed on another build, the stages the history had not seen, the ridge penalty, and the rows that could not be read."""

    tier: str
    fidelity: str
    runs: int
    executions: int
    build_changed: bool
    unseen: tuple[str, ...] = ()
    penalty: float | None = None
    skipped: int = 0
    # Where the numbers that are not the history's came from (``factory``, ``provisional`` or ``pinned``), and when the
    # calibration they come from was written.
    knobs: str = "factory"
    knobs_updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "fidelity": self.fidelity, "runs": self.runs, "executions": self.executions, "build_changed": self.build_changed, "unseen": list(self.unseen), "penalty": self.penalty, "skipped": self.skipped, "knobs": self.knobs, "knobs_updated_at": self.knobs_updated_at}


@dataclass(frozen=True)
class Forecast:
    """The forecast of the running job. ``stage`` and its units are null in a cooldown or between runs; ``run`` and ``job``
    are null when there is none to give; ``as_of`` is the clock it was made by."""

    as_of: float
    stage: str | None
    stage_done: int | None
    stage_total: int | None
    run: Span | None
    job: Span | None
    basis: Basis

    def as_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "stage": self.stage, "stage_done": self.stage_done, "stage_total": self.stage_total, "run": self.run.as_dict() if self.run else None, "job": self.job.as_dict() if self.job else None, "basis": self.basis.as_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Forecast:
        basis = data["basis"]
        return cls(
            as_of=data["as_of"],
            stage=data["stage"],
            stage_done=data["stage_done"],
            stage_total=data["stage_total"],
            run=Span(**data["run"]) if data["run"] else None,
            job=Span(**data["job"]) if data["job"] else None,
            basis=Basis(basis["tier"], basis["fidelity"], basis["runs"], basis["executions"], basis["build_changed"], tuple(basis["unseen"]), basis["penalty"], basis["skipped"], basis.get("knobs", "factory"), basis.get("knobs_updated_at")),
        )


@dataclass(frozen=True)
class PlannedRun:
    """A run that has not started, described as ``TimingStarted`` will record it."""

    number: int
    backend: str
    pipeline: str | None
    workload: Mapping[str, Any]
    environment: Mapping[str, Any]

    @property
    def signature(self) -> Signature:
        return signature(self.backend, self.pipeline, self.workload, self.environment)


@dataclass(frozen=True)
class JobPlan:
    """The runs an entry will execute (a resumed one only those from its first), and its cooldown policy."""

    runs: tuple[PlannedRun, ...]
    cooldown: CooldownPolicy


def lognormal_left(mean: float, sigma: float, elapsed: float) -> tuple[float, float]:
    """The mean and spread of what is left of a lognormal duration of this ``mean`` and log spread that has already lasted
    ``elapsed`` seconds: the duration conditioned on exceeding it. Before it has lasted any time, the duration itself."""
    if mean <= 0:
        return 0.0, 0.0
    sigma = max(sigma, 1e-6)
    if elapsed <= 0:
        return mean, mean * math.sqrt(math.expm1(sigma**2))
    mu = math.log(mean) - sigma**2 / 2
    d = (math.log(elapsed) - mu) / sigma
    if d > 8.0:
        # Far in the tail the residual grows with the elapsed time, by the hazard of the lognormal.
        left = elapsed * sigma / d
        return left, left
    tail = 0.5 * math.erfc(d / math.sqrt(2))
    first = mean * 0.5 * math.erfc((d - sigma) / math.sqrt(2)) / tail
    second = math.exp(2 * mu + 2 * sigma**2) * 0.5 * math.erfc((d - 2 * sigma) / math.sqrt(2)) / tail
    return max(first - elapsed, 0.05 * elapsed), math.sqrt(max(second - first**2, 0.0))


@dataclass
class _Sum:
    """Seconds with a spread: independent parts add in quadrature, and each coherent group (a run's speed factor, the state an
    execution shares) adds its parts linearly before they are squared."""

    mean: float = 0.0
    var: float = 0.0
    coherent: dict[Hashable, float] = field(default_factory=dict)

    def add(self, mean: float, sd: float = 0.0, group: Hashable = None) -> None:
        self.mean += mean
        if group is None:
            self.var += sd**2
        else:
            self.coherent[group] = self.coherent.get(group, 0.0) + sd

    def merge(self, other: _Sum, scale: float = 1.0) -> None:
        """Add ``other`` with its spread multiplied by ``scale`` (how much a wait that follows it magnifies its error)."""
        self.mean += other.mean
        self.var += other.var * scale**2
        for group, sd in other.coherent.items():
            self.coherent[group] = self.coherent.get(group, 0.0) + sd * scale

    @property
    def sd(self) -> float:
        return math.sqrt(self.var + sum(sd**2 for sd in self.coherent.values()))


def _reuse(stage: _Stage) -> int:
    """The reuse interval of the stage's plan, 1 for a stage that is not planned as steps."""
    return stage.plan.reuse if isinstance(stage.plan, IterativeStage) else 1


def _shape(slope: float, position: float) -> float:
    """The share of a stage's mean step time at ``position`` (0 to 1) when the log step time changes by ``slope`` across
    the stage, normalized so the steps average 1."""
    if abs(slope) < 1e-9:
        return 1.0
    return math.exp(slope * (position - 0.5)) * (slope / 2) / math.sinh(slope / 2)


@dataclass
class _Stage:
    """The stage the run is in; ``plan`` is its entry in the run's expected sequence, none for a stage not in it."""

    key: str
    name: str
    source: str
    start: float
    plan: StagePlan | None
    done: int | None = None
    total: int | None = None
    readings: list[tuple[float, int]] = field(default_factory=list)


@dataclass
class _Run:
    """What is known of the run in progress."""

    number: int
    origin: float
    plan: Plan
    signature: Signature
    backend: str
    # The prior of the execution's shared state (mean and variance in the log), of the run's own factor, and the posterior
    # of the run's speed factor.
    prior_mean: float
    shared_var: float
    own_var: float
    u_mean: float
    u_var: float
    position: int = -1
    seen: dict[str, int] = field(default_factory=dict)
    stage: _Stage | None = None
    unseen: list[str] = field(default_factory=list)
    gaps: list[tuple[float, int, float]] = field(default_factory=list)
    used: int = 0
    steps_total: int | None = None
    child_done: float = 0.0
    pace_level: float = 0.0
    pace_weight: int = 0


@dataclass
class _Between:
    """The run that finished last, until the next one starts; ``wait_until`` is when its cooldown ends."""

    number: int
    backend: str
    wait_until: float | None


@dataclass
class _Estimate:
    """What is left of the run in progress: the seconds, the part of them the speed factor moves, and the child's share."""

    total: _Sum
    exposure: float
    child_mean: float
    u_sd: float


class Forecaster:
    """The forecast of one entry's job. Build it when the job is parsed, feed it every job event, and ask it for a forecast."""

    def __init__(self, model: FittedModel, plan: JobPlan) -> None:
        self._model = model
        self._job = plan
        self._planned = {run.number: run for run in plan.runs}
        self._plans: dict[int, Plan] = {run.number: model.plan(run.backend, run.pipeline, run.workload, run.environment) for run in plan.runs}
        self._started_at: float | None = None
        self._run: _Run | None = None
        self._between: _Between | None = None
        self._parking = False
        self._cancelled = False
        self._finished = False
        # The state the runs of this execution share, per backend, after the runs so far: its mean and variance in the log.
        self._carry: dict[str, tuple[float, float]] = {}
        # What the runs of this job have taken, for a later run of the same kind that has no history of its own.
        self._actual: dict[Signature, tuple[float, float]] = {}

    # ------------------------------------------------------------------ events

    @property
    def tunables(self) -> Tunables:
        return self._model.tunables

    def set_parking(self, parking: bool) -> None:
        self._parking = parking

    def cancel(self) -> None:
        self._cancelled = True

    def observe(self, event: JobEvent, now: float) -> None:
        if self._cancelled or self._finished:
            return
        if isinstance(event, JobStarted):
            self._started_at = now
            if self._planned:
                first = min(self._planned)
                planned = self._planned[first]
                self._run = self._start(first, now, planned.backend, planned.signature, self._plans[first])
        elif isinstance(event, RunStarted):
            planned = self._planned.get(event.number)
            self._between = None
            self._run = self._start(event.number, now, planned.backend, planned.signature, self._plans[event.number]) if planned is not None else None
        elif isinstance(event, TimingStarted):
            self._timing_started(event)
        elif isinstance(event, StageStarted):
            self._stage_started(event)
        elif isinstance(event, StageFinished):
            self._stage_finished(event)
        elif isinstance(event, RunOutput):
            self._output(event)
        elif isinstance(event, RunFinished):
            self._run_finished(event, now)
        elif isinstance(event, CooldownStarted):
            if self._between is not None:
                self._between.wait_until = now + event.seconds
        elif isinstance(event, CooldownEnded):
            if self._between is not None:
                self._between.wait_until = None
        elif isinstance(event, JobFinished):
            self._finished = True

    def _start(self, number: int, origin: float, backend: str, run_signature: Signature, plan: Plan) -> _Run:
        carried = self._carry.get(backend)
        own = max(plan.prior_var - plan.shared_var, 0.0)
        mean, shared = carried if carried is not None else (0.0, plan.shared_var)
        return _Run(number, origin, plan, run_signature, backend, mean, shared, own, mean, shared + own)

    def _timing_started(self, event: TimingStarted) -> None:
        run = self._run
        if run is None or run.number != event.number:
            return
        actual = signature(event.backend, event.pipeline, event.workload, event.environment)
        if actual != run.signature:
            # The run is not the one that was described, so it is priced as what it is.
            self._run = self._start(event.number, run.origin, event.backend, actual, self._model.plan(event.backend, event.pipeline, event.workload, event.environment))

    def _stage_started(self, event: StageStarted) -> None:
        run = self._run
        if run is None or run.number != event.number or event.start < 0:
            return
        count = run.seen.get(event.stage, 0) + 1
        run.seen[event.stage] = count
        key = event.stage if count == 1 else f"{event.stage}#{count}"
        index = next((i for i, item in enumerate(run.plan.stages) if item.key == key), None)
        matched = run.plan.stages[index] if index is not None and index > run.position else None
        if matched is not None and index is not None:
            run.position = index
        elif run.plan.stages:
            name = event.phase if event.stage == "other" and event.phase else event.stage
            if name not in run.unseen:
                run.unseen.append(name)
        run.stage = _Stage(key, event.stage, event.source, event.start, matched)

    def _stage_finished(self, event: StageFinished) -> None:
        run = self._run
        if run is not None and run.number == event.number and event.start >= 0 and event.source == SOURCE_CHILD:
            run.child_done += max(0.0, event.end - event.start)

    def _output(self, event: RunOutput) -> None:
        run = self._run
        if run is None or run.number != event.number or event.stage is None:
            return
        stage = run.stage
        if stage is None or stage.name != event.stage or event.done is None:
            return
        stage.done, stage.total = event.done, event.total
        if event.stage in STEP_STAGES and event.total:
            self._read_step(run, stage, event.offset, event.done, event.total)

    def _read_step(self, run: _Run, stage: _Stage, offset: float, done: int, total: int) -> None:
        """A reading of the step counter: the gap since the last one is a step time, which updates the run's speed factor."""
        run.steps_total = total
        if stage.readings:
            last_offset, last_done = stage.readings[-1]
            if done <= last_done or offset <= last_offset:
                return
            steps = done - last_done
            count = evaluated_between(last_done, done, total, _reuse(stage))
            if count < 1:
                # Reused steps arrive with the evaluated step before them and take no time of their own: the next
                # evaluated step's clock runs from this reading.
                stage.readings[-1] = (offset, done)
                return
            self._observe_gap(run, (offset - last_offset) / count, count, (last_done + steps / 2) / total, first=len(stage.readings) == 1)
        stage.readings.append((offset, done))

    def _observe_gap(self, run: _Run, seconds: float, count: int, position: float, *, first: bool) -> None:
        plan = run.plan
        shape = math.log(_shape(plan.slope, position))
        if first:
            # The first gap is its own coefficient: with none fitted, a planned run's says nothing of the rate.
            if plan.first_ratio is not None:
                seconds /= plan.first_ratio
            elif plan.log_step is not None:
                return
        elif self._is_pause(run, seconds, shape):
            return
        if plan.log_step is not None:
            observed = math.log(seconds) - (plan.log_step + shape)
            # The first gap is noisier than a step by as much as its ratio differs from one run to the next.
            noise = plan.step_noise**2 / count + (plan.first_sd**2 if first else 0.0)
            precision = 1.0 / run.u_var + 1.0 / noise
            run.u_mean = (run.u_mean / run.u_var + observed / noise) / precision
            run.u_var = 1.0 / precision
        else:
            run.pace_level = (run.pace_level * run.pace_weight + (math.log(seconds) - shape) * count) / (run.pace_weight + count)
            run.pace_weight += count
        if not first:
            run.gaps.append((seconds, count, position))
        run.used += 1

    def _is_pause(self, run: _Run, seconds: float, shape: float) -> bool:
        times = [gap[0] for gap in run.gaps]
        if len(times) >= MIN_MEDIAN_STEPS:
            return seconds > self._model.tunables.pause_factor * statistics.median(times)
        if run.plan.log_step is None:
            return False
        # The prior's prediction, not the posterior's: a step is judged against what was expected before the data it
        # would update, or one wild early reading would make every normal step after it look like a pause.
        return seconds > self._model.tunables.pause_factor * math.exp(run.plan.log_step + run.prior_mean + shape)

    def _run_finished(self, event: RunFinished, now: float) -> None:
        run = self._run
        if run is None or run.number != event.number:
            return
        self._carry[run.backend] = self._carried(run)
        child = event.seconds if event.seconds is not None else run.child_done
        if event.status == RunStatus.SUCCEEDED:
            self._actual[run.signature] = (now - run.origin, child)
        wait_until = None
        if event.status == RunStatus.SUCCEEDED and any(number > event.number for number in self._planned):
            seconds = self._job.cooldown.wait_after(child).seconds
            wait_until = now + seconds if seconds > 0 else None
        self._between = _Between(event.number, run.backend, wait_until)
        self._run = None

    @staticmethod
    def _carried(run: _Run) -> tuple[float, float]:
        """The state the execution's runs share, after this run: its prior, moved toward the factor the run showed by the
        share of the run's variance the execution owns, and the variance that leaves."""
        total = run.shared_var + run.own_var
        weight = run.shared_var / total if total > 0 else 0.0
        mean = run.prior_mean + weight * (run.u_mean - run.prior_mean)
        return mean, weight**2 * run.u_var + (run.shared_var * run.own_var / total if total > 0 else 0.0)

    # ---------------------------------------------------------------- forecast

    def forecast(self, now: float) -> Forecast | None:
        """What is expected at ``now``; None once cancelled or finished, or before the job starts."""
        if self._cancelled or self._finished or self._started_at is None:
            return None
        if self._run is not None:
            return self._in_run(self._run, now)
        if self._between is not None:
            return self._between_runs(self._between, now)
        return None

    def _in_run(self, run: _Run, now: float) -> Forecast:
        stage = run.stage
        estimate = self._estimate(run, now)
        basis = self._basis(run.plan, tuple(run.unseen))
        span = job = None
        if estimate is not None:
            degrees = run.plan.df + run.used
            span = self._span(run.origin, now, estimate.total, degrees)
            job = span if self._parking else self._job_span(run, estimate, now, degrees)
        return Forecast(now, stage.name if stage else None, stage.done if stage else None, stage.total if stage else None, span, job, basis)

    def _between_runs(self, between: _Between, now: float) -> Forecast:
        later = [number for number in self._planned if number > between.number]
        plan = self._plans[later[0] if later else between.number]
        job = None
        if later and not self._parking and self._started_at is not None:
            future = self._future(later, None)
            if future is not None:
                total = _Sum(max(between.wait_until - now, 0.0) if between.wait_until is not None else 0.0)
                total.merge(future)
                job = self._span(self._started_at, now, total, plan.df)
        return Forecast(now, None, None, None, None, job, self._basis(plan, ()))

    def _job_span(self, run: _Run, current: _Estimate, now: float, degrees: float) -> Span | None:
        assert self._started_at is not None
        later = [number for number in self._planned if number > run.number]
        total = _Sum()
        total.merge(current.total, self._wait_slope(current.child_mean) if later else 1.0)
        if later:
            total.mean += self._job.cooldown.wait_after(current.child_mean).seconds
            future = self._future(later, (run, current))
            if future is None:
                return None
            total.merge(future)
        return self._span(self._started_at, now, total, degrees)

    def _wait_slope(self, child_mean: float) -> float:
        """How much a run's spread is magnified in the time to the next run: one, plus the policy's share of the child's time
        while that share is between the bounds."""
        policy = self._job.cooldown
        if policy.mode != "auto" or policy.wait_after(child_mean).bound is not None:
            return 1.0
        return 1.0 + policy.ratio

    @staticmethod
    def _span(started: float, now: float, total: _Sum, degrees: float) -> Span:
        end = now + total.mean
        reach = min(MAX_REACH_SECONDS, student_t_90(degrees) * total.sd)
        return Span(started, end, max(now, end - reach), end + reach)

    @staticmethod
    def _basis(plan: Plan, unseen: tuple[str, ...]) -> Basis:
        return Basis(plan.tier, plan.fidelity, plan.runs, plan.executions, plan.build_changed, (*unseen, *(name for name in plan.unseen_knobs if name not in unseen)), plan.penalty, plan.skipped, plan.knobs, plan.knobs_updated_at)

    # ------------------------------------------------------------- estimating

    def _estimate(self, run: _Run, now: float) -> _Estimate | None:
        """The run in progress: what is left of the stage it is in and of every stage to come."""
        plan = run.plan
        if plan.log_step is None or not plan.stages:
            return self._live_only(run, now)
        total = _Sum()
        child_mean = run.child_done
        exposure = 0.0
        stage = run.stage
        if stage is not None:
            elapsed = now - (run.origin + stage.start)
            if stage.source == SOURCE_CHILD:
                child_mean += max(elapsed, 0.0)
            active = stage.plan
            if isinstance(active, IterativeStage):
                mean, sd = self._iterative_left(run, stage, active, now)
                total.add(mean, sd)
                exposure += mean
                child_mean += mean if stage.source == SOURCE_CHILD else 0.0
            elif isinstance(active, FixedStage):
                mean, sd = self._fixed_left(stage, active, elapsed)
                total.add(mean, sd)
                child_mean += mean if stage.source == SOURCE_CHILD else 0.0
            else:
                # An unseen stage still needs time to finish. Treat its pooled spread as a rough duration and widen the
                # range; otherwise a stage after the planned tail would claim the run is already done.
                mean, sd = lognormal_left(max(DEFAULT_OTHER_STAGE_SECONDS, min(plan.other_sd, 60.0)), 0.75, elapsed)
                total.add(mean, math.hypot(sd, plan.other_sd))
                child_mean += mean if stage.source == SOURCE_CHILD else 0.0
        for item in plan.stages[run.position + 1 :]:
            if isinstance(item, FixedStage):
                total.add(item.mean, item.sd)
                child_mean += item.mean if item.source == SOURCE_CHILD else 0.0
            else:
                mean, sd = self._iterative_whole(plan, item, run.u_mean, run.u_var)
                total.add(mean, sd)
                exposure += mean
                child_mean += mean if item.source == SOURCE_CHILD else 0.0
        total.add(plan.overhead_mean, plan.overhead_sd)
        u_sd = math.sqrt(run.u_var)
        total.add(0.0, u_sd * exposure, ("u", run.number))
        return _Estimate(total, exposure, child_mean, u_sd)

    def _live_only(self, run: _Run, now: float) -> _Estimate | None:
        """With no history that qualifies, a number only once the steps have two readings: the pace they show, with the
        spread of a stage not in the history added for everything that comes after."""
        stage = run.stage
        if stage is None or stage.name not in STEP_STAGES or not stage.readings or run.pace_weight == 0 or run.steps_total is None:
            return None
        plan = run.plan
        n = stage.total or run.steps_total
        level = math.exp(run.pace_level + plan.step_noise**2 / run.pace_weight / 2)
        mean, sd = self._remaining_steps(run, stage, n, level, now)
        total = _Sum()
        total.add(mean, sd)
        total.add(0.0, plan.other_sd)
        return _Estimate(total, 0.0, 0.0, 0.0)

    def _step_level(self, plan: Plan, u_mean: float, u_var: float) -> float:
        """The mean step time of a plan's stage for a run with this speed factor, before the stage's shape."""
        assert plan.log_step is not None
        return math.exp(plan.log_step + u_mean + u_var / 2)

    def _iterative_whole(self, plan: Plan, item: IterativeStage, u_mean: float, u_var: float) -> tuple[float, float]:
        """A stage of steps that has not started: its steps, and what the first gap and the pauses add."""
        level = self._step_level(plan, u_mean, u_var)
        steps = [level * _shape(plan.slope, (index + 0.5) / item.steps) for index in range(item.steps) if evaluated(index, item.steps, item.reuse)]
        mean = plan.stage_factor * sum(steps)
        return mean, math.sqrt(sum((step * plan.step_noise) ** 2 for step in steps))

    def _iterative_left(self, run: _Run, stage: _Stage, item: IterativeStage, now: float) -> tuple[float, float]:
        plan = run.plan
        if not stage.readings:
            whole, spread = self._iterative_whole(plan, item, run.u_mean, run.u_var)
            left, sd = lognormal_left(whole, WHOLE_STAGE_SIGMA, now - (run.origin + stage.start))
            return left, math.hypot(sd, spread)
        return self._remaining_steps(run, stage, stage.total or item.steps, self._step_level(plan, run.u_mean, run.u_var), now)

    def _remaining_steps(self, run: _Run, stage: _Stage, n: int, level: float, now: float) -> tuple[float, float]:
        """The seconds left in a stage of ``n`` steps whose latest reading is at hand: the step in progress, conditioned on
        having lasted this long, and each step to come at its place in the stage's shape."""
        plan = run.plan
        offset, done = stage.readings[-1]
        if done >= n:
            return 0.0, 0.0
        interval = _reuse(stage)
        # Reused steps finish at once, so the step the clock is running on is the next evaluated one.
        upcoming = next((index for index in range(done, n) if evaluated(index, n, interval)), n - 1)
        elapsed = max(0.0, now - (run.origin + offset))
        current = level * _shape(plan.slope, (upcoming + 0.5) / n)
        overrun = min(1.0, max(0.0, (elapsed - current) / current)) if current > 0 else 1.0
        sigma = plan.step_noise + (OVERRUN_SIGMA - plan.step_noise) * overrun if OVERRUN_SIGMA > plan.step_noise else plan.step_noise
        mean, sd = lognormal_left(current, sigma, elapsed)
        var = sd**2
        centre = sum(position * count for _, count, position in run.gaps) / sum(count for _, count, _ in run.gaps) if run.gaps else 0.5
        lean = current * ((upcoming + 0.5) / n - centre)
        for index in range(upcoming + 1, n):
            if not evaluated(index, n, interval):
                continue
            position = (index + 0.5) / n
            step = level * _shape(plan.slope, position)
            # The pauses runs make add to the steps still to come, as many as they have added to the ones before them.
            mean += step * (1.0 + plan.pause_share)
            var += (step * plan.step_noise) ** 2
            lean += step * (position - centre)
        var += (plan.slope_sd * lean) ** 2
        return mean, math.sqrt(var)

    @staticmethod
    def _fixed_left(stage: _Stage, item: FixedStage, elapsed: float) -> tuple[float, float]:
        """What is left of a stage that is not made of steps: from its counter's pace once it has shown some, else its
        duration conditioned on having lasted this long."""
        if stage.done is not None and stage.total:
            share = (stage.total - stage.done) / stage.total
            if stage.done >= MIN_PACE_UNITS and elapsed > 0:
                return (stage.total - stage.done) * elapsed / stage.done, item.sd * share
            return item.mean * share, item.sd * share
        return lognormal_left(item.mean, item.sigma, elapsed)

    # --------------------------------------------------------------- the rest

    def _future(self, numbers: list[int], active: tuple[_Run, _Estimate] | None) -> _Sum | None:
        """The runs still to come and the waits between them (not the wait after the run in progress); None when a run
        has no history and no run of this job to be priced from."""
        total = _Sum()
        for index, number in enumerate(numbers):
            built = self._future_run(number, active)
            if built is None:
                return None
            run_sum, child_mean = built
            last = index == len(numbers) - 1
            total.merge(run_sum, 1.0 if last else self._wait_slope(child_mean))
            if not last:
                total.mean += self._job.cooldown.wait_after(child_mean).seconds
        return total

    def _future_run(self, number: int, active: tuple[_Run, _Estimate] | None) -> tuple[_Sum, float] | None:
        planned, plan = self._planned[number], self._plans[number]
        total = _Sum()
        if plan.log_step is None or not plan.stages:
            actual = self._actual.get(planned.signature)
            if actual is None:
                return None
            total.add(actual[0], ANALOG_SPREAD * actual[0])
            return total, actual[1]
        backend = planned.backend
        own = max(plan.prior_var - plan.shared_var, 0.0)
        groups: list[tuple[Hashable, float]]
        if active is not None and active[0].backend == backend and active[0].plan.log_step is not None:
            current = active[0]
            variance = current.shared_var + current.own_var
            weight = current.shared_var / variance if variance > 0 else 0.0
            eps = math.sqrt(current.shared_var * current.own_var / variance) if variance > 0 else 0.0
            mean = current.prior_mean + weight * (current.u_mean - current.prior_mean)
            shared = weight**2 * current.u_var + eps**2
            groups = [(("u", current.number), weight * active[1].u_sd), (("eps", backend), eps)]
        elif backend in self._carry:
            mean, shared = self._carry[backend]
            groups = [(("eps", backend), math.sqrt(shared))]
        else:
            mean, shared = 0.0, plan.shared_var
            groups = [(("eps", backend), math.sqrt(shared))]
        exposure = child_mean = 0.0
        for item in plan.stages:
            if isinstance(item, FixedStage):
                total.add(item.mean, item.sd)
                child_mean += item.mean if item.source == SOURCE_CHILD else 0.0
            else:
                stage_mean, stage_sd = self._iterative_whole(plan, item, mean, shared + own)
                total.add(stage_mean, stage_sd)
                exposure += stage_mean
                child_mean += stage_mean if item.source == SOURCE_CHILD else 0.0
        total.add(plan.overhead_mean, plan.overhead_sd)
        for group, sd in groups:
            total.add(0.0, sd * exposure, group)
        total.add(0.0, math.sqrt(own) * exposure)
        return total, child_mean
