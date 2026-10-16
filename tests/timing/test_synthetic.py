"""The synthetic history the estimator is tested on: its size and shape, and that it holds what the model does not assume."""

from __future__ import annotations

import statistics
import unittest
from collections import Counter
from functools import cache

from framecontrol.jobs.timing.features import FRAMECONTROL, HistoryRun, StepSeries, iterative_stage, step_series
from tests.timing.synthetic import SyntheticHistory, synthetic_history


@cache
def history() -> SyntheticHistory:
    return synthetic_history()


class SizeAndShapeTests(unittest.TestCase):
    def test_it_holds_at_least_2000_runs_over_at_least_20_signatures_of_both_backends(self) -> None:
        runs = history().runs
        self.assertGreaterEqual(len(runs), 2000)
        self.assertGreaterEqual(len({run.signature for run in runs}), 20)
        self.assertEqual({run.backend for run in runs}, {"draw-things-cli", "h3"})

    def test_an_execution_has_one_to_six_runs_numbered_from_one_and_a_cooldown(self) -> None:
        for execution in history().executions:
            self.assertTrue(1 <= len(execution.runs) <= 6)
            self.assertEqual([run.run for run in execution.runs], list(range(1, len(execution.runs) + 1)))
            self.assertTrue(all(run.execution == execution.number for run in execution.runs))
            self.assertIn(execution.cooldown.mode, {"auto", "manual", "off"})

    def test_executions_are_numbered_in_time_order_and_a_signature_is_a_backend_and_a_pipeline(self) -> None:
        self.assertEqual([execution.number for execution in history().executions], list(range(1, len(history().executions) + 1)))
        self.assertEqual({(run.backend, run.pipeline) for run in history().runs}, {("draw-things-cli", "i2v"), ("h3", "i2av"), ("h3", "r2av")})

    def test_the_same_seed_gives_the_same_history_and_another_gives_another(self) -> None:
        self.assertEqual([run.stages for run in synthetic_history().runs[:50]], [run.stages for run in history().runs[:50]])
        self.assertNotEqual([run.stages for run in synthetic_history(7).runs[:50]], [run.stages for run in history().runs[:50]])

    def test_some_signatures_are_rare_so_the_fallback_tiers_are_exercised(self) -> None:
        counts = Counter(run.signature for run in history().runs)
        self.assertGreaterEqual(sum(1 for count in counts.values() if count < 3), 4)
        self.assertGreaterEqual(sum(1 for count in counts.values() if count >= 30), 6)


class RunLayoutTests(unittest.TestCase):
    def test_a_runs_stages_are_contiguous_from_zero_and_the_framecontrol_ones_come_after_the_childs(self) -> None:
        for run in history().runs[:300]:
            stages = run.counted_stages
            self.assertEqual(stages[0].stage, "prepare")
            self.assertEqual(stages[0].start, 0.0)
            for earlier, later in zip(stages, stages[1:], strict=False):
                self.assertAlmostEqual(earlier.end, later.start, places=6)
            first_framecontrol_after = next(index for index, stage in enumerate(stages) if index > 0 and stage.source == FRAMECONTROL)
            self.assertTrue(all(stage.source == FRAMECONTROL for stage in stages[first_framecontrol_after:]))

    def test_the_iterative_stage_has_a_mark_for_every_step_and_the_marks_rise(self) -> None:
        for run in history().runs[:300]:
            stage = iterative_stage(run)
            assert stage is not None
            self.assertEqual(len(stage.marks), (stage.total or 0) + 1)
            self.assertEqual([done for _, done in stage.marks], list(range(stage.total + 1 if stage.total else 0)))
            times = [time for time, _ in stage.marks]
            self.assertEqual(times, sorted(times))

    def test_a_draw_things_run_has_the_stages_the_cli_prints_and_an_h3_run_its_phases(self) -> None:
        names = {run.backend: [stage.stage for stage in run.counted_stages] for run in history().runs[:20]}
        self.assertTrue({"starting", "processing", "sampling", "finishing"} <= set(names["draw-things-cli"]))
        self.assertTrue({"tokenize", "text_encode", "load_core", "denoise", "audio_decode", "video_load", "ffmpeg"} <= set(names["h3"]))


@cache
def series_of(backend: str) -> list[tuple[HistoryRun, StepSeries]]:
    found = []
    for run in history().runs:
        stage = iterative_stage(run)
        if run.backend == backend and stage is not None:
            series = step_series(stage)
            if series is not None:
                found.append((run, series))
    return found


class MisspecificationTests(unittest.TestCase):
    """What the estimator does not assume is in the data, so coverage is not tested against its own model."""

    @staticmethod
    def series(backend: str) -> list[tuple[HistoryRun, StepSeries]]:
        return series_of(backend)

    def test_draw_things_step_time_mostly_rises_and_one_run_in_eight_falls(self) -> None:
        slopes = [series.slope for _, series in self.series("draw-things-cli") if series.slope is not None]
        rising = sum(1 for slope in slopes if slope > 0.05)
        falling = sum(1 for slope in slopes if slope < -0.05)
        self.assertGreater(rising / len(slopes), 0.75)
        self.assertTrue(0.06 < falling / len(slopes) < 0.20)

    def test_an_h3_runs_first_gap_is_shorter_and_its_slope_has_no_consistent_sign(self) -> None:
        series = [item for _, item in self.series("h3")]
        ratios = [item.first_ratio for item in series if item.first_ratio is not None]
        self.assertAlmostEqual(statistics.median(ratios), 0.74, delta=0.08)
        slopes = [item.slope for item in series if item.slope is not None]
        self.assertTrue(0.3 < sum(1 for slope in slopes if slope > 0) / len(slopes) < 0.7)

    def test_a_run_now_and_then_pauses(self) -> None:
        pauses = [item.pauses for _, item in self.series("draw-things-cli") + self.series("h3")]
        self.assertTrue(0.02 < sum(1 for count in pauses if count) / len(pauses) < 0.12)

    def test_a_signature_run_after_the_executable_was_rebuilt_is_slower_with_the_same_signature(self) -> None:
        by_build: dict[int, list[float]] = {}
        busiest = self.busiest("h3")
        for run, series in self.series("h3"):
            if run.signature == busiest:
                by_build.setdefault(run.environment["executable_bytes"], []).append(series.mean_seconds)
        self.assertEqual(len(by_build), 2)
        old, new = sorted(by_build)
        self.assertGreater(statistics.median(by_build[new]) / statistics.median(by_build[old]), 1.03)

    def test_a_runs_speed_factor_has_heavy_tails(self) -> None:
        run_type = self.busiest("draw-things-cli")
        times = [series.mean_seconds for run, series in self.series("draw-things-cli") if run.signature == run_type]
        median = statistics.median(times)
        deviations = sorted(abs(time / median - 1) for time in times)
        self.assertGreater(deviations[-1], 3 * statistics.median(deviations))

    @staticmethod
    def busiest(backend: str) -> object:
        counts = Counter(run.signature for run in history().runs if run.backend == backend)
        return counts.most_common(1)[0][0]


class RunsAreHistoryRuns(unittest.TestCase):
    def test_every_row_is_a_live_complete_style_history_run(self) -> None:
        run: HistoryRun = history().runs[0]
        self.assertEqual(run.fidelity, "live")
        self.assertIn("pixels", run.workload)
        self.assertIn("model", run.environment)
