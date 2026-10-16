"""The replay: it feeds stored runs through an estimator as a live stream and scores what it says against what happened."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import JobEvent, RunFinished, RunStarted
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun
from framecontrol.jobs.timing.replay_stream import replay_job
from framecontrol.services.timing_replay import (
    BUILTIN_ESTIMATORS,
    CHECKPOINTS_JOB,
    CHECKPOINTS_RUN,
    Interval,
    JobContext,
    Reading,
    ReplayEstimator,
    cells_of,
    percentile,
    qualified,
    replay_folds,
    replay_leave_one_out,
)
from tests.timing.builders import dt_run, dt_workload, h3_run


def execution(number: int, runs: int = 2, *, step: float = 50.0, policy: CooldownPolicy | None = None) -> HistoryExecution:
    return HistoryExecution(number, policy or CooldownPolicy("manual", seconds=100.0), tuple(dt_run(number, index + 1, step=step) for index in range(runs)))


def history(count: int = 8, **changes: float) -> list[HistoryExecution]:
    return [execution(number, **changes) for number in range(1, count + 1)]  # type: ignore[arg-type]


@dataclass
class Oracle:
    """An estimator that knows how every run and the job ended, to show the harness scores a perfect one as perfect."""

    context: JobContext
    scale: float = 1.0
    wait: float = 0.0
    spread: float = 5.0
    hide_run_share: bool = False

    def __post_init__(self) -> None:
        self.truth = replay_job(self.context.execution, first_run=self.context.first_run, origin=0.0)
        self.current: int | None = None
        self.started_after: dict[int, float] = {}

    def observe(self, event: JobEvent, now: float) -> None:
        if isinstance(event, RunStarted):
            self.current = event.number
        elif isinstance(event, RunFinished):
            self.current = None

    def reading(self, now: float) -> Reading:
        run = None
        if self.current is not None and now - self.truth.starts[self.current] >= self.wait:
            left = (self.truth.ends[self.current] - now) * self.scale
            run = Interval(left, max(0.0, left - self.spread), left + self.spread)
        job_left = (self.truth.finished - now) * self.scale
        share = None if self.hide_run_share else (now - self.truth.started) / (self.truth.finished - self.truth.started)
        return Reading(run, Interval(job_left, max(0.0, job_left - self.spread), job_left + self.spread), share, share)


class CheckpointTests(unittest.TestCase):
    def report(self, **options):
        return replay_leave_one_out(history(), lambda context: Oracle(context, **options))

    def test_a_perfect_estimator_has_no_error_and_covers_every_truth(self) -> None:
        report = self.report()
        for cell in cells_of(report.observations, ("level", "checkpoint")):
            self.assertAlmostEqual(cell.median_ape, 0.0, places=6)
            self.assertAlmostEqual(cell.bias, 0.0, places=6)
            self.assertEqual(cell.coverage, 1.0)

    def test_runs_are_scored_at_the_run_checkpoints_and_the_job_at_its_own(self) -> None:
        report = self.report()
        run = {cell.checkpoint for cell in cells_of(report.observations, ("level", "checkpoint")) if cell.level == "run"}
        job = {cell.checkpoint for cell in cells_of(report.observations, ("level", "checkpoint")) if cell.level == "job"}
        self.assertEqual((run, job), (set(CHECKPOINTS_RUN), set(CHECKPOINTS_JOB)))
        self.assertEqual(CHECKPOINTS_RUN, (0.05, 0.10, 0.25, 0.50, 0.75, 0.90))
        self.assertEqual(CHECKPOINTS_JOB, (0.10, 0.25, 0.50, 0.75, 0.90))

    def test_the_truth_of_a_run_checkpoint_is_the_time_to_the_runs_own_end(self) -> None:
        report = self.report()
        run_seconds = dt_run(1, 1).seconds
        first = [item for item in report.observations if item.level == "run" and item.execution == 1 and item.run == 1]
        self.assertEqual([round(item.truth / run_seconds, 6) for item in first], [round(1 - p, 6) for p in CHECKPOINTS_RUN])

    def test_a_biased_estimator_shows_its_bias_and_error(self) -> None:
        report = self.report(scale=1.1)
        for cell in cells_of(report.observations, ("level", "checkpoint")):
            self.assertAlmostEqual(cell.median_ape, 0.1, places=6)
            self.assertAlmostEqual(cell.bias, 0.1, places=6)

    def test_coverage_is_the_share_of_truths_inside_the_range(self) -> None:
        report = self.report(spread=0.0, scale=1.05)
        self.assertTrue(all(cell.coverage == 0.0 for cell in cells_of(report.observations, ("level",))))

    def test_cells_can_be_grouped_by_backend_and_pipeline(self) -> None:
        mixed = [execution(number) for number in range(1, 5)] + [HistoryExecution(number, CooldownPolicy("off"), (h3_run(number, 1),)) for number in range(5, 9)]
        report = replay_leave_one_out(mixed, lambda context: Oracle(context))
        groups = {(cell.backend, cell.pipeline) for cell in cells_of(report.observations, ("backend", "pipeline"))}
        self.assertEqual(groups, {("draw-things-cli", "i2v"), ("h3-metal-cli", "r2av")})

    def test_the_percentile_interpolates(self) -> None:
        self.assertEqual(percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)
        self.assertEqual(percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.9), 4.6)
        self.assertEqual(percentile([7.0], 0.9), 7.0)


class TraceTests(unittest.TestCase):
    def test_the_time_to_the_first_number_is_seconds_after_the_run_started(self) -> None:
        report = replay_leave_one_out(history(count=4), lambda context: Oracle(context, wait=30.0))
        self.assertTrue(report.traces)
        for trace in report.traces:
            self.assertIsNotNone(trace.first_number)
            assert trace.first_number is not None
            self.assertGreaterEqual(trace.first_number, 30.0)
            self.assertLess(trace.first_number, 80.0)

    def test_a_run_that_never_gets_a_number_has_none(self) -> None:
        report = replay_leave_one_out(history(count=4), lambda context: Oracle(context, wait=1e9))
        self.assertTrue(all(trace.first_number is None for trace in report.traces))

    def test_a_share_that_falls_is_counted(self) -> None:
        class Falling(Oracle):
            def reading(self, now: float) -> Reading:
                base = super().reading(now)
                share = 1.0 - (base.run_share or 0.0)
                return Reading(base.run, base.job, share, share)

        report = replay_leave_one_out(history(count=4), lambda context: Falling(context))
        self.assertTrue(all(trace.run_moves_back > 0 for trace in report.traces))
        self.assertTrue(all(count > 0 for count in report.job_moves_back.values()))
        steady = replay_leave_one_out(history(count=4), lambda context: Oracle(context))
        self.assertTrue(all(trace.run_moves_back == 0 for trace in steady.traces))
        self.assertTrue(all(count == 0 for count in steady.job_moves_back.values()))


class CrossValidationTests(unittest.TestCase):
    def test_a_job_is_never_replayed_with_its_own_execution_in_the_history(self) -> None:
        seen: list[tuple[int, set[int]]] = []

        def factory(context: JobContext) -> ReplayEstimator:
            seen.append((context.execution.number, {run.execution for run in context.training}))
            return Oracle(context)

        replay_leave_one_out(history(count=5), factory)
        self.assertEqual(sorted(number for number, _ in seen), [1, 2, 3, 4, 5])
        for number, trained in seen:
            self.assertNotIn(number, trained)
            self.assertEqual(trained, {1, 2, 3, 4, 5} - {number})

    def test_folds_leave_each_scored_execution_out_of_its_folds_history(self) -> None:
        seen: list[tuple[int, set[int]]] = []

        def factory(context: JobContext) -> ReplayEstimator:
            seen.append((context.execution.number, {run.execution for run in context.training}))
            return Oracle(context)

        report = replay_folds(history(count=20), factory, folds=4, per_fold=4, seed=3)
        scored = {number for number, _ in seen}
        self.assertTrue(scored)
        self.assertTrue(report.observations)
        for number, trained in seen:
            self.assertNotIn(number, trained)
            # Every execution of the scored one's fold is out, since they share a machine state.
            self.assertGreaterEqual(len(trained), 20 - 7)

    def test_each_fold_scores_at_least_the_asked_number_of_runs_chosen_by_the_seed(self) -> None:
        first = replay_folds(history(count=20), lambda context: Oracle(context), folds=4, per_fold=4, seed=3)
        again = replay_folds(history(count=20), lambda context: Oracle(context), folds=4, per_fold=4, seed=3)
        other = replay_folds(history(count=20), lambda context: Oracle(context), folds=4, per_fold=4, seed=4)
        runs = {(trace.execution, trace.run) for trace in first.traces}
        self.assertEqual(runs, {(trace.execution, trace.run) for trace in again.traces})
        self.assertNotEqual(runs, {(trace.execution, trace.run) for trace in other.traces})
        self.assertGreaterEqual(len(runs), 4 * 4)

    def test_only_a_backend_asked_for_is_scored(self) -> None:
        mixed = [execution(number) for number in range(1, 5)] + [HistoryExecution(number, CooldownPolicy("off"), (h3_run(number, 1),)) for number in range(5, 9)]
        report = replay_leave_one_out(mixed, lambda context: Oracle(context), backend="h3-metal-cli")
        self.assertEqual({item.backend for item in report.observations}, {"h3-metal-cli"})


class QualifiedTests(unittest.TestCase):
    def test_a_run_is_qualified_with_three_other_runs_of_its_signature_from_two_executions(self) -> None:
        training: list[HistoryRun] = [run for number in (1, 2) for run in execution(number).runs]
        self.assertTrue(qualified(dt_run(9, 1), training))
        self.assertFalse(qualified(dt_run(9, 1), training[:2]))
        self.assertFalse(qualified(dt_run(9, 1), [run for run in training if run.execution == 1]))
        other = dt_run(9, 1, workload=dt_workload(steps=11))
        self.assertFalse(qualified(other, training))

    def test_the_observations_carry_whether_their_run_was(self) -> None:
        report = replay_leave_one_out(history(count=6), lambda context: Oracle(context))
        self.assertTrue(all(item.qualified for item in report.observations))
        thin = replay_leave_one_out(history(count=2), lambda context: Oracle(context))
        self.assertFalse(any(item.qualified for item in thin.observations))


class BuiltInEstimatorTests(unittest.TestCase):
    def play(self, name: str, executions: list[HistoryExecution], target: HistoryExecution, seconds: float):
        training = [run for item in executions for run in item.runs]
        from framecontrol.jobs.timing.fit import FittedModel

        context = JobContext(target, 1, training, FittedModel(training))
        estimator = BUILTIN_ESTIMATORS[name](context)
        job = replay_job(target, origin=0.0)
        for when, event in job.events:
            if when <= seconds:
                estimator.observe(event, when)
        return estimator.reading(seconds), job

    def test_the_names_are_the_commands(self) -> None:
        self.assertEqual(sorted(BUILTIN_ESTIMATORS), ["constant-pace", "forecast", "last-signature"])

    def test_last_signature_says_what_the_last_run_of_its_signature_took_less_the_time_gone(self) -> None:
        reading, _ = self.play("last-signature", history(count=4), execution(9), 100.0)
        assert reading.run is not None
        self.assertAlmostEqual(reading.run.left, dt_run(1, 1).seconds - 100.0, places=6)

    def test_constant_pace_waits_for_two_readings_of_the_steps(self) -> None:
        early, _ = self.play("constant-pace", history(count=4), execution(9), 14.0)
        self.assertIsNone(early.run)
        later, _ = self.play("constant-pace", history(count=4), execution(9), 14.0 + 50.0 + 10.0)
        assert later.run is not None
        self.assertAlmostEqual(later.run.left, 9 * 50.0 - 10.0, delta=1.0)

    def test_the_forecast_is_read_the_way_a_client_reads_the_published_one(self) -> None:
        reading, job = self.play("forecast", history(count=4), execution(9), 300.0)
        assert reading.run is not None and reading.job is not None and reading.run_share is not None
        assert reading.run.low is not None and reading.run.high is not None
        self.assertAlmostEqual(reading.run.left, job.ends[1] - 300.0, delta=0.03 * 527.0)
        self.assertLessEqual(reading.run.low, reading.run.left)
        self.assertGreaterEqual(reading.run.high, reading.run.left)
        self.assertAlmostEqual(reading.job.left, job.finished - 300.0, delta=0.03 * job.finished)
        self.assertGreater(reading.run_share, 0.4)
