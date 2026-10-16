"""Milestone 11's acceptance criteria on the synthetic history: how close the forecast is, how honest its range is, and that
it beats the estimator it replaces. The history, the folds, and the seeds are fixed, so a number here is the same every run.

The real history the owner's database holds is measured once, on the owner host, and recorded in
``docs/research/time-estimation.md``; it is too small and moves too much to gate ``make check``."""

from __future__ import annotations

import time
import unittest
from collections import Counter
from functools import cache

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.timing.features import HistoryExecution
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.services.timing_replay import BUILTIN_ESTIMATORS, Cell, ReplayReport, _score, _SignatureIndex, cells_of, replay_folds, replay_leave_one_out
from tests.timing.builders import dt_run
from tests.timing.frozen_estimator import FrozenReplayEstimator
from tests.timing.synthetic import synthetic_history

FOLDS, PER_FOLD, SEED = 10, 40, 1
# The least coverage and the most that "the 80% range covers the truth" allows.
COVERAGE = (0.70, 0.90)
# The CI set runs in at most this much CPU.
CPU_SECONDS = 20.0


@cache
def forecast() -> ReplayReport:
    return replay_folds(synthetic_history().executions, BUILTIN_ESTIMATORS["forecast"], folds=FOLDS, per_fold=PER_FOLD, seed=SEED)


@cache
def frozen() -> ReplayReport:
    return replay_folds(synthetic_history().executions, FrozenReplayEstimator, folds=FOLDS, per_fold=PER_FOLD, seed=SEED)


def by_checkpoint(report: ReplayReport, level: str, *, qualified: bool = False) -> dict[float, Cell]:
    items = [item for item in report.observations if item.level == level and (item.qualified or not qualified)]
    return {cell.checkpoint: cell for cell in cells_of(items, ("checkpoint",)) if cell.checkpoint is not None}


class RunAccuracyTests(unittest.TestCase):
    def test_the_set_is_the_size_the_criteria_describe(self) -> None:
        self.assertGreaterEqual(len(forecast().traces), FOLDS * PER_FOLD * 0.9)
        self.assertTrue(all(trace.qualified for trace in forecast().traces[:50]))

    def test_on_runs_with_history_of_their_signature_the_error_of_the_time_left_is_within_the_targets(self) -> None:
        cells = by_checkpoint(forecast(), "run", qualified=True)
        self.assertLessEqual(cells[0.10].median_ape, 0.15)
        self.assertLessEqual(cells[0.50].median_ape, 0.05)
        self.assertLessEqual(cells[0.90].median_ape, 0.10)

    def test_the_80_percent_range_covers_the_truth_between_70_and_90_percent_of_the_time_from_10_percent_on(self) -> None:
        for checkpoint, cell in by_checkpoint(forecast(), "run", qualified=True).items():
            if checkpoint >= 0.10:
                assert cell.coverage is not None
                self.assertTrue(COVERAGE[0] <= cell.coverage <= COVERAGE[1], f"run at {checkpoint:.0%}: {cell.coverage:.2f}")


class JobAccuracyTests(unittest.TestCase):
    def test_the_error_of_the_jobs_time_left_is_within_the_targets(self) -> None:
        cells = by_checkpoint(forecast(), "job", qualified=True)
        self.assertLessEqual(cells[0.10].median_ape, 0.15)
        self.assertLessEqual(cells[0.50].median_ape, 0.10)
        self.assertLessEqual(cells[0.90].median_ape, 0.10)

    def test_the_range_covers_the_truth_between_70_and_90_percent_of_the_time_at_each_checkpoint(self) -> None:
        for checkpoint, cell in by_checkpoint(forecast(), "job", qualified=True).items():
            assert cell.coverage is not None
            self.assertTrue(COVERAGE[0] <= cell.coverage <= COVERAGE[1], f"job at {checkpoint:.0%}: {cell.coverage:.2f}")


class AgainstTheFrozenEstimatorTests(unittest.TestCase):
    def test_pooled_the_median_error_is_lower_at_every_checkpoint_where_the_frozen_estimator_gives_a_number(self) -> None:
        for level in ("run", "job"):
            new, old = by_checkpoint(forecast(), level), by_checkpoint(frozen(), level)
            for checkpoint, cell in old.items():
                self.assertLess(new[checkpoint].median_ape, cell.median_ape, f"{level} at {checkpoint:.0%}")

    def test_in_no_backend_pipeline_and_checkpoint_cell_is_it_more_than_two_points_worse(self) -> None:
        keys = ("level", "backend", "pipeline", "checkpoint")
        new = {(cell.level, cell.backend, cell.pipeline, cell.checkpoint): cell for cell in cells_of(forecast().observations, keys)}
        old = {(cell.level, cell.backend, cell.pipeline, cell.checkpoint): cell for cell in cells_of(frozen().observations, keys)}
        self.assertTrue(old)
        for key, cell in old.items():
            self.assertLessEqual(new[key].median_ape, cell.median_ape + 0.02, str(key))

    def test_the_displayed_share_never_moves_back_within_a_run_or_a_job(self) -> None:
        self.assertEqual(sum(trace.run_moves_back for trace in forecast().traces), 0)
        self.assertEqual(sum(forecast().job_moves_back.values()), 0)
        # The estimator it replaces moved it back all the time.
        self.assertGreater(sum(trace.run_moves_back for trace in frozen().traces), 100)

    def test_with_history_of_the_signature_the_first_number_comes_before_the_first_step_and_no_later_than_the_old_ones(self) -> None:
        old = {(trace.execution, trace.run): trace.first_number for trace in frozen().traces}
        for trace in forecast().traces:
            self.assertEqual(trace.first_number, 0.0)
            former = old[(trace.execution, trace.run)]
            self.assertTrue(former is None or 0.0 <= former)


class ThinHistoryTests(unittest.TestCase):
    def test_with_under_3_runs_of_the_signature_and_under_6_of_the_backend_the_run_is_priced_at_its_start_from_them(self) -> None:
        executions = [HistoryExecution(number, CooldownPolicy("off"), (dt_run(number, 1), dt_run(number, 2))) for number in (1, 2)]
        report = replay_leave_one_out(executions, BUILTIN_ESTIMATORS["forecast"])
        self.assertTrue(report.traces)
        self.assertTrue(all(trace.first_number == 0.0 for trace in report.traces))
        self.assertLessEqual(by_checkpoint(report, "run")[0.05].median_ape, 0.05)
        self.assertEqual(sum(trace.run_moves_back for trace in report.traces), 0)

    def test_the_signatures_with_too_little_history_are_priced_by_the_fits_and_are_still_close(self) -> None:
        history = synthetic_history()
        counts = Counter(run.signature for run in history.runs)
        rare = [execution for execution in history.executions if counts[execution.runs[0].signature] < 3]
        self.assertGreaterEqual(len(rare), 4)
        report = ReplayReport()
        for execution in rare:
            training = [run for other in history.executions if other.number != execution.number for run in other.runs]
            _score(execution, training, FittedModel(training), BUILTIN_ESTIMATORS["forecast"], _SignatureIndex(training), report, None)
        self.assertFalse(any(item.qualified for item in report.observations))
        cells = by_checkpoint(report, "run")
        self.assertLessEqual(cells[0.10].median_ape, 0.15)
        self.assertLessEqual(cells[0.50].median_ape, 0.10)
        self.assertTrue(all(trace.first_number == 0.0 for trace in report.traces))


class CostTests(unittest.TestCase):
    def test_the_ci_set_runs_in_at_most_20_seconds_of_cpu(self) -> None:
        started = time.process_time()
        replay_folds(synthetic_history().executions, BUILTIN_ESTIMATORS["forecast"], folds=FOLDS, per_fold=PER_FOLD, seed=SEED + 1)
        self.assertLess(time.process_time() - started, CPU_SECONDS)
