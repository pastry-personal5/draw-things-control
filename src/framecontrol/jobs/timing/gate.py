"""Pure M15 held-out partitioning and deterministic paired replay gate.

The updater supplies one paired error observation per execution. Keeping the
statistical decision here makes it impossible for a
candidate to accidentally partition its own training evidence differently
from the baseline it is compared with.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.forecast import Forecaster, JobPlan, PlannedRun
from framecontrol.jobs.timing.knobs import Calibration, Tunables
from framecontrol.jobs.timing.replay_stream import ReplayJob, replay_job

MARGIN = 0.01
BOOTSTRAP_SAMPLES = 10_000
REPLAY_CHECKPOINTS = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90)


@dataclass(frozen=True)
class Partition:
    training: tuple[Mapping[str, int], ...]
    holdout: tuple[Mapping[str, int], ...]
    reason: str | None = None


@dataclass(frozen=True)
class GateResult:
    verdict: str
    interval: tuple[float, float] | None
    differences: tuple[float, ...]
    reason: str


@dataclass(frozen=True)
class ReplayScore:
    """Full remaining-time replay metrics, reduced to one paired value per execution."""

    errors: Mapping[int, float]
    coverage_distance: Mapping[int, float]
    observations: int
    valid: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class ReplayComparison:
    """A candidate minus a baseline on exactly the same held-out executions."""

    execution_ids: tuple[int, ...]
    error_differences: tuple[float, ...]
    coverage_differences: tuple[float, ...]
    baseline: ReplayScore
    candidate: ReplayScore

    @property
    def valid(self) -> bool:
        return self.baseline.valid and self.candidate.valid


@dataclass(frozen=True)
class ReplayCase:
    execution: HistoryExecution
    stream: ReplayJob
    marks: tuple[tuple[float, int], ...]


def partition(entries: Iterable[Mapping[str, int]], checkpoint_execution: int | None) -> Partition:
    """Split entries by execution, reserving the newest post-checkpoint ones.

    Multiple runs in an execution stay together.  The returned entries retain
    chronological order and no holdout item can be used by a caller's fit.
    """
    if checkpoint_execution is None:
        return Partition((), (), "insufficient_evidence")
    ordered = sorted(entries, key=lambda item: (item["e"], item.get("r", 0)))
    executions = sorted({item["e"] for item in ordered})
    eligible = [execution for execution in executions if execution > checkpoint_execution]
    hold_count = min(8, max(3, math.ceil(len(executions) * 0.2)))
    if len(eligible) < hold_count:
        return Partition((), (), "insufficient_evidence")
    hold_ids = frozenset(eligible[-hold_count:])
    holdout = tuple(item for item in ordered if item["e"] in hold_ids)
    training = tuple(item for item in ordered if item["e"] not in hold_ids)
    if len({item["e"] for item in training}) < 3:
        return Partition(training, holdout, "insufficient_evidence")
    return Partition(training, holdout)


def paired_bootstrap(differences: Sequence[float], execution_ids: Sequence[int], *, samples: int = BOOTSTRAP_SAMPLES, confidence: float = 0.90) -> tuple[float, float]:
    """A deterministic paired-bootstrap interval for median differences."""
    if len(differences) != len(execution_ids) or not differences or samples < 1 or not 0 < confidence < 1 or len(set(execution_ids)) != len(execution_ids) or not all(math.isfinite(value) for value in differences):
        raise ValueError("paired differences must be finite and have one value per distinct execution, with at least one sample")
    pairs = sorted(zip(execution_ids, differences, strict=True))
    seed_bytes = hashlib.sha256(",".join(str(execution) for execution, _difference in pairs).encode()).digest()
    generator = np.random.default_rng(int.from_bytes(seed_bytes[:8], "big"))
    values = np.asarray([difference for _execution, difference in pairs], dtype=float)
    draws = generator.integers(0, len(values), size=(samples, len(values)))
    medians = np.median(values[draws], axis=1)
    tail = (1.0 - confidence) / 2.0
    return (float(np.quantile(medians, tail)), float(np.quantile(medians, 1.0 - tail)))


def decide(differences: Sequence[float], execution_ids: Sequence[int], *, valid: bool = True, margin: float = MARGIN, confidence: float = 0.90) -> GateResult:
    """Return promote/reject/inconclusive for candidate-minus-baseline error.

    Negative values improve error.  A holdout smaller than five executions is
    always inconclusive, so a small unanimous sample cannot turn repeated
    looks at noise into an approval.
    """
    if not valid:
        return GateResult("reject", None, tuple(differences), "invalid_candidate")
    if len(differences) != len(execution_ids) or len(differences) < 3 or not all(math.isfinite(value) for value in differences):
        return GateResult("inconclusive", None, tuple(differences), "insufficient_evidence")
    interval = paired_bootstrap(differences, execution_ids, confidence=confidence)
    if len(differences) < 5:
        return GateResult("inconclusive", interval, tuple(differences), "fewer_than_five_holdouts")
    if interval[0] > 0:
        return GateResult("reject", interval, tuple(differences), "clear_regression")
    if interval[1] <= margin and float(np.median(differences)) < 0:
        return GateResult("promote", interval, tuple(differences), "no_worse_than_margin")
    return GateResult("inconclusive", interval, tuple(differences), "uncertain")


def _executions(runs: Sequence[HistoryRun]) -> tuple[HistoryExecution, ...]:
    grouped: dict[int, list[HistoryRun]] = defaultdict(list)
    for run in runs:
        grouped[run.execution].append(run)
    off = CooldownPolicy("off")
    return tuple(HistoryExecution(number, off, tuple(sorted(members, key=lambda item: item.run))) for number, members in sorted(grouped.items()))


def _plan(execution: HistoryExecution) -> JobPlan:
    return JobPlan(tuple(PlannedRun(run.run, run.backend, run.pipeline, dict(run.workload), dict(run.environment)) for run in execution.runs), execution.cooldown)


def prepare_replay(runs: Sequence[HistoryRun], *, checkpoints: Sequence[float] = REPLAY_CHECKPOINTS) -> tuple[ReplayCase, ...]:
    """Build the model-independent event streams a set of candidates shares."""
    cases = []
    for execution in _executions(runs):
        stream = replay_job(execution, origin=0.0)
        marks = tuple(sorted((stream.starts[run.run] + checkpoint * (stream.ends[run.run] - stream.starts[run.run]), run.run) for run in execution.runs for checkpoint in checkpoints))
        cases.append(ReplayCase(execution, stream, marks))
    return tuple(cases)


def replay(runs: Sequence[HistoryRun], tunables: Tunables, calibration: Calibration, *, checkpoints: Sequence[float] = REPLAY_CHECKPOINTS, cases: Sequence[ReplayCase] | None = None) -> ReplayScore:
    """Replay ``runs`` through the complete forecast and score remaining time.

    The ring does not retain a job's cooldown policy, and the gate judges each
    run's remaining time rather than a fabricated job total.  Runs of the same
    execution are still replayed together so the execution carry is preserved.
    """
    errors: dict[int, list[float]] = defaultdict(list)
    covered: dict[int, list[float]] = defaultdict(list)
    observations = 0
    prepared = tuple(cases) if cases is not None else prepare_replay(runs, checkpoints=checkpoints)
    for case in prepared:
        execution, stream, marks = case.execution, case.stream, case.marks
        model = FittedModel([], tunables=tunables, calibration=calibration)
        forecaster = Forecaster(model, _plan(execution))
        event_index = 0
        for at, number in marks:
            while event_index < len(stream.events) and stream.events[event_index][0] <= at:
                when, event = stream.events[event_index]
                forecaster.observe(event, when)
                event_index += 1
            forecast = forecaster.forecast(at)
            span = forecast.run if forecast is not None else None
            truth = stream.ends[number] - at
            if span is None or truth <= 0 or not all(math.isfinite(value) for value in (span.end_at, span.low, span.high)):
                return ReplayScore({}, {}, observations, False, f"no finite forecast for E{execution.number:04d} run {number}")
            estimate = max(0.0, span.end_at - at)
            errors[execution.number].append(abs(estimate - truth) / truth)
            covered[execution.number].append(1.0 if span.low <= stream.ends[number] <= span.high else 0.0)
            observations += 1
    if not errors:
        return ReplayScore({}, {}, 0, False, "no replay observations")
    return ReplayScore(
        {execution: statistics.fmean(values) for execution, values in errors.items()},
        {execution: abs(statistics.fmean(values) - 0.8) for execution, values in covered.items()},
        observations,
    )


def compare_replays(runs: Sequence[HistoryRun], baseline_tunables: Tunables, baseline_calibration: Calibration, candidate_tunables: Tunables, candidate_calibration: Calibration) -> ReplayComparison:
    """Replay two models on one reserved set and return execution-paired differences."""
    cases = prepare_replay(runs)
    baseline = replay(runs, baseline_tunables, baseline_calibration, cases=cases)
    candidate = replay(runs, candidate_tunables, candidate_calibration, cases=cases)
    execution_ids = tuple(sorted(set(baseline.errors) & set(candidate.errors) & set(baseline.coverage_distance) & set(candidate.coverage_distance)))
    return ReplayComparison(
        execution_ids,
        tuple(candidate.errors[item] - baseline.errors[item] for item in execution_ids),
        tuple(candidate.coverage_distance[item] - baseline.coverage_distance[item] for item in execution_ids),
        baseline,
        candidate,
    )


def coverage_regressed(comparison: ReplayComparison, *, margin: float = MARGIN) -> bool:
    """Whether the candidate's distance from 80% coverage is clearly worse."""
    return decide(comparison.coverage_differences, comparison.execution_ids, valid=comparison.valid, margin=margin).verdict == "reject"


__all__ = ["BOOTSTRAP_SAMPLES", "GateResult", "MARGIN", "Partition", "REPLAY_CHECKPOINTS", "ReplayCase", "ReplayComparison", "ReplayScore", "compare_replays", "coverage_regressed", "decide", "paired_bootstrap", "partition", "prepare_replay", "replay"]
