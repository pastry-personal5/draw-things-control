"""The fit: which history a run is priced from (the tiers), what it predicts, and how sure it is."""

from __future__ import annotations

import math
import time
import unittest
from dataclasses import replace

import numpy as np

from framecontrol.jobs.timing.features import HistoryRun
from framecontrol.jobs.timing.fit import MIN_FIRST_SD, PENALTIES, FittedModel, FixedStage, IterativeStage, Plan, student_t_90, student_t_975
from tests.timing.builders import BASE_PIXELS, DT, H3, dt_run, dt_workload, h3_run, h3_workload, log_run
from tests.timing.synthetic import synthetic_history

ENVIRONMENT = {"model": "base.ckpt", "executable_bytes": 100, "executable_mtime": 1.0}


def plan_of(history: list[HistoryRun], *, backend: str = DT, pipeline: str = "i2v", workload: dict | None = None, environment: dict | None = None, skipped: int = 0) -> Plan:
    return FittedModel(history, skipped=skipped).plan(backend, pipeline, workload if workload is not None else dt_workload(), environment if environment is not None else ENVIRONMENT)


def spread(count: int, *, pixels: list[int], frames: list[int], per_execution: int = 3, start: int = 1, exponent: float = 1.2) -> list[HistoryRun]:
    """``count`` runs of different sizes and clips, ``per_execution`` to an execution, each step ``50 * pixels^1.2 * frames``."""
    runs = []
    for index in range(count):
        p, f = pixels[index % len(pixels)], frames[(index // len(pixels)) % len(frames)]
        step = 50.0 * (p / BASE_PIXELS) ** exponent * (f / 81)
        runs.append(dt_run(start + index // per_execution, index % per_execution + 1, step=step, workload=dt_workload(pixels=p, frames=f)))
    return runs


def fixed(plan: Plan, name: str) -> FixedStage:
    stage = next(stage for stage in plan.stages if stage.stage == name)
    assert isinstance(stage, FixedStage)
    return stage


def iterative(plan: Plan) -> IterativeStage:
    stage = next(stage for stage in plan.stages if isinstance(stage, IterativeStage))
    return stage


PIXELS = [163_840, 245_760, 372_736, 589_824]
FRAMES = [41, 81, 121]


class FactoryDrawThingsTests(unittest.TestCase):
    WORKLOAD = {"width": 384, "height": 768, "pixels": 294912, "frames": 33, "steps": 25, "mode": "i2v", "guidance_scale": 5, "strength": 1, "shift": 3.99, "color_corrects": True}
    ENVIRONMENT = {"model": "wan_v2.2_a14b_hne_i2v_i8x.ckpt", "executable_bytes": 160477120, "executable_mtime": 1790314797.2408893}

    def test_e0053_factory_prices_only_its_measured_workload(self) -> None:
        model = FittedModel([])
        plan = model.plan(DT, "i2v", self.WORKLOAD, self.ENVIRONMENT)
        self.assertEqual((plan.tier, plan.fidelity, plan.runs, plan.executions), ("factory", "factory", 0, 0))
        self.assertAlmostEqual(math.exp(plan.log_step or 0.0), 13.6710, places=4)
        self.assertEqual(iterative(plan).steps, 25)
        self.assertAlmostEqual(fixed(plan, "correct").median, 6.847, places=3)
        estimated = sum(stage.median for stage in plan.stages if isinstance(stage, FixedStage)) + 25 * math.exp(plan.log_step or 0.0) * plan.stage_factor
        self.assertAlmostEqual(estimated, (353.620 + 369.037) / 2, delta=0.2)
        self.assertEqual(model.plan(DT, "i2v", {**self.WORKLOAD, "frames": 17}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(DT, "i2v", {**self.WORKLOAD, "color_corrects": False}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(DT, "i2v", self.WORKLOAD, {"model": "another.ckpt"}).tier, "heuristic")
        self.assertEqual(model.plan(DT, "i2v", self.WORKLOAD, {**self.ENVIRONMENT, "executable_bytes": 2}).tier, "heuristic")

    def test_cold_start_scales_the_measured_rate_and_omits_unused_correction(self) -> None:
        workload = {**self.WORKLOAD, "frames": 66, "steps": 40, "color_corrects": False}
        plan = FittedModel([]).plan(DT, "i2v", workload, {"model": "another.ckpt"})
        self.assertEqual(plan.tier, "heuristic")
        self.assertEqual(iterative(plan).steps, 40)
        self.assertNotIn("correct", [stage.stage for stage in plan.stages])
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step + plan.prior_var / 2), 2 * 13.6710, places=4)

    def test_still_image_cold_start_does_not_divide_the_video_rate_by_33(self) -> None:
        workload = {**self.WORKLOAD, "frames": 1, "mode": "i2i", "color_corrects": False}
        plan = FittedModel([]).plan(DT, "i2i", workload, {"model": "image.ckpt"})
        self.assertEqual(plan.tier, "heuristic")
        self.assertEqual(plan.log_step_var, 1.0)
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step + plan.prior_var / 2), 13.6710 / math.sqrt(33), places=4)


class FactoryH3Tests(unittest.TestCase):
    WORKLOAD = {"pipeline": "r2av", "width": 832, "height": 448, "pixels": 372736, "frames": 362, "steps": 30, "seconds": 362 / 24, "references": {"image": 3}, "reference_count": 3, "render_width": None, "render_height": None, "denoise_reuse": 1, "dit_layers": 50, "core_reuse": 1, "token_reduction": False, "ssd_streaming": False, "int8_row_fc2": False, "reference_rope": False, "reference_image_size": "match", "knobs": {"seconds": 15, "requested_frames": 360, "effective_frames": 362, "effective_seconds": 362 / 24, "reference_pair": "three-subjects"}}
    ENVIRONMENT = {"model": "MiniMax-H3", "executable_bytes": 558688, "executable_mtime": 1791736910.7589593}

    def test_e0051_and_e0054_factory_uses_effective_h3_workload(self) -> None:
        model = FittedModel([])
        plan = model.plan(H3, "r2av", self.WORKLOAD, self.ENVIRONMENT)
        self.assertEqual((plan.tier, plan.fidelity, plan.runs, plan.executions), ("factory", "factory", 0, 0))
        self.assertAlmostEqual(math.exp(plan.log_step or 0.0), 138.6683, places=4)
        self.assertEqual(iterative(plan).steps, 30)
        self.assertAlmostEqual(fixed(plan, "ffmpeg").median, 189.977, places=3)
        self.assertAlmostEqual(plan.first_ratio or 0.0, 0.798, places=3)
        estimated = sum(stage.median for stage in plan.stages if isinstance(stage, FixedStage)) + 30 * math.exp(plan.log_step or 0.0) * plan.stage_factor
        self.assertAlmostEqual(estimated, (4421.824 + 4291.886) / 2, delta=0.2)
        unrelated = FittedModel([h3_run(1, 1)]).plan(H3, "r2av", self.WORKLOAD, self.ENVIRONMENT)
        self.assertEqual((unrelated.tier, unrelated.tau, unrelated.rho, unrelated.step_noise, unrelated.first_sd, unrelated.pause_share), (plan.tier, plan.tau, plan.rho, plan.step_noise, plan.first_sd, plan.pause_share))
        self.assertEqual(model.plan(H3, "r2av", {**self.WORKLOAD, "frames": 360}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(H3, "r2av", {**self.WORKLOAD, "knobs": {**self.WORKLOAD["knobs"], "seconds": 10}}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(H3, "r2av", {**self.WORKLOAD, "dit_layers": 36}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(H3, "r2av", {**self.WORKLOAD, "compatibility": {"bf16_attention_output": True}}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(H3, "r2av", {**self.WORKLOAD, "color_corrects": True}, self.ENVIRONMENT).tier, "heuristic")
        self.assertEqual(model.plan(H3, "r2av", self.WORKLOAD, {**self.ENVIRONMENT, "executable_bytes": 2}).tier, "heuristic")

    def test_h3_cold_start_uses_effective_frames_and_prices_i2av_correction(self) -> None:
        workload = {**self.WORKLOAD, "pipeline": "i2av", "frames": 242, "seconds": 242 / 24, "references": {}, "reference_count": 0, "color_corrects": True}
        plan = FittedModel([]).plan(H3, "i2av", workload, {"model": "another-h3"})
        self.assertEqual(plan.tier, "heuristic")
        self.assertEqual(iterative(plan).steps, 30)
        self.assertEqual([stage.stage for stage in plan.stages[-7:]], ["verify", "copy_raw", "convert", "drift", "correct", "last_frame", "promote"])
        self.assertAlmostEqual(sum(fixed(plan, name).mean for name in ("copy_raw", "correct", "last_frame")), 42.0 * 242 / 362, places=4)
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step + plan.prior_var / 2), 138.6683 * (242 / 362) ** 1.6, places=4)


class SparseHeuristicTests(unittest.TestCase):
    def test_one_same_model_run_supplies_its_rate_and_stage_order(self) -> None:
        history = [dt_run(1, 1, step=50.0)]
        plan = plan_of(history)
        self.assertEqual((plan.tier, plan.fidelity, plan.runs), ("heuristic", "live", 1))
        self.assertEqual([stage.stage for stage in plan.stages], ["prepare", "starting", "processing", "sampling", "finishing", "verify"])
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step + plan.prior_var / 2), 50.0, places=4)

    def test_a_matching_executable_build_wins_over_a_newer_different_build(self) -> None:
        old = dt_run(1, 1, step=40.0, environment=ENVIRONMENT)
        rebuilt = dt_run(2, 1, step=90.0, environment={**ENVIRONMENT, "executable_bytes": 200})
        plan = plan_of([old, rebuilt])
        self.assertEqual((plan.tier, plan.runs, plan.build_changed), ("heuristic", 1, False))
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step + plan.prior_var / 2), 40.0, places=4)
        changed = plan_of([old], environment={**ENVIRONMENT, "executable_bytes": 300})
        self.assertTrue(changed.build_changed)
        self.assertGreater(changed.log_step_var, plan.log_step_var)

    def test_sparse_h3_stages_keep_color_finishing_in_order_even_when_history_lacks_it(self) -> None:
        base = h3_workload()
        sample = h3_run(1, 1, workload={**base, "pipeline": "i2av"})
        workload = {**base, "pipeline": "i2av", "color_corrects": True}
        plan = FittedModel([sample]).plan(H3, "i2av", workload, sample.environment)
        self.assertEqual(plan.tier, "heuristic")
        self.assertEqual([stage.stage for stage in plan.stages[-4:]], ["verify", "copy_raw", "correct", "last_frame"])

    def test_nonfinite_sparse_dimensions_are_ignored(self) -> None:
        malformed = dt_run(1, 1, workload={**dt_workload(), "pixels": float("inf")})
        plan = plan_of([malformed])
        self.assertEqual((plan.tier, plan.runs), ("heuristic", 0))

    def test_rows_without_a_named_model_do_not_count_as_same_model_history(self) -> None:
        anonymous = dt_run(1, 1, step=100.0, environment={})
        plan = FittedModel([anonymous]).plan(DT, "i2v", anonymous.workload, {})
        self.assertEqual((plan.tier, plan.runs, plan.fidelity), ("heuristic", 0, "heuristic"))


class StudentTTests(unittest.TestCase):
    @staticmethod
    def integral(df: int, upper: float) -> float:
        """P(T <= upper) by Simpson's rule on the density, independent of the table."""
        constant = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
        steps, low = 20000, -2000.0 if df == 1 else -200.0
        width = (upper - low) / steps
        total = 0.0
        for index in range(steps + 1):
            x = low + index * width
            weight = 1 if index in (0, steps) else 4 if index % 2 else 2
            total += weight * constant * (1 + x * x / df) ** (-(df + 1) / 2)
        return total * width / 3

    def test_the_table_is_the_80_percent_interval_quantile_for_every_degree_of_freedom_to_30(self) -> None:
        for df in range(1, 31):
            self.assertAlmostEqual(self.integral(df, student_t_90(df)), 0.90, delta=0.002 if df > 1 else 0.004, msg=f"df {df}")

    def test_the_95_percent_table_is_checked_the_same_way(self) -> None:
        for df in (2, 3, 5, 10, 20, 30):
            self.assertAlmostEqual(self.integral(df, student_t_975(df)), 0.975, delta=0.002, msg=f"df {df}")
        self.assertAlmostEqual(student_t_975(31), 1.96, places=2)

    def test_above_30_it_is_the_normal_quantile_and_it_falls_with_the_degrees_of_freedom(self) -> None:
        self.assertAlmostEqual(student_t_90(31), 1.2816, places=3)
        self.assertAlmostEqual(student_t_90(10_000), 1.2816, places=3)
        values = [student_t_90(df) for df in range(1, 40)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_a_fraction_of_a_degree_of_freedom_rounds_down_and_less_than_one_is_one(self) -> None:
        self.assertEqual(student_t_90(4.9), student_t_90(4))
        self.assertEqual(student_t_90(0.2), student_t_90(1))


class TierOneTests(unittest.TestCase):
    def history(self) -> list[HistoryRun]:
        return [dt_run(1, 1, step=48.0), dt_run(2, 1, step=50.0), dt_run(2, 2, step=52.0)]

    def test_three_runs_of_the_signature_from_two_executions_price_the_run_from_them(self) -> None:
        plan = plan_of(self.history())
        self.assertEqual((plan.tier, plan.fidelity, plan.runs, plan.executions), ("signature", "live", 3, 2))
        self.assertIsNotNone(plan.log_step)
        assert plan.log_step is not None
        # The mean of the executions' means in the log: 48 s, then 50 s and 52 s.
        self.assertAlmostEqual(plan.log_step, (math.log(48.0) + (math.log(50.0) + math.log(52.0)) / 2) / 2, places=9)
        self.assertIsNone(plan.penalty)

    def test_the_sequence_is_the_newest_runs_and_its_other_stages_are_the_medians_of_the_signature(self) -> None:
        history = [dt_run(1, 1, finishing=10.0), dt_run(2, 1, finishing=12.0), dt_run(3, 1, finishing=11.0)]
        plan = plan_of(history)
        self.assertEqual([stage.stage for stage in plan.stages], ["prepare", "starting", "processing", "sampling", "finishing", "verify"])
        self.assertAlmostEqual(fixed(plan, "finishing").median, 11.0, places=6)
        self.assertEqual((iterative(plan).stage, iterative(plan).steps), ("sampling", 10))

    def test_runs_from_one_execution_are_not_enough(self) -> None:
        history = [dt_run(1, 1), dt_run(1, 2), dt_run(1, 3)]
        self.assertEqual(plan_of(history).tier, "heuristic")

    def test_two_runs_are_not_enough(self) -> None:
        self.assertEqual(plan_of([dt_run(1, 1), dt_run(2, 1)]).tier, "heuristic")

    def test_a_log_row_never_has_the_signature_of_a_live_one(self) -> None:
        history = [log_run(1, 1), log_run(2, 1), log_run(3, 1)]
        plan = plan_of(history, workload=dt_workload(frames=81, steps=10))
        self.assertNotEqual(plan.tier, "signature")

    def test_the_run_is_flagged_when_the_newest_run_was_timed_on_another_build(self) -> None:
        history = self.history()
        self.assertFalse(plan_of(history).build_changed)
        self.assertTrue(plan_of(history, environment={**ENVIRONMENT, "executable_bytes": 999}).build_changed)

    def test_a_planned_run_with_no_frame_count_is_priced_with_the_joined_frames_of_its_signature(self) -> None:
        workload = dt_workload(frames=None)
        history = [dt_run(index, 1, workload=workload, output_frames=81) for index in (1, 2, 3)]
        plan = plan_of(history, workload=workload)
        self.assertEqual(plan.tier, "signature")


class TierTwoTests(unittest.TestCase):
    def test_a_fit_within_the_backend_and_pipeline_recovers_how_the_step_time_grows(self) -> None:
        history = spread(12, pixels=PIXELS, frames=FRAMES)
        plan = plan_of(history, workload=dt_workload(pixels=450_000, frames=100))
        self.assertEqual((plan.tier, plan.fidelity, plan.runs, plan.executions), ("backend_fit", "live", 12, 4))
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step), 50.0 * (450_000 / BASE_PIXELS) ** 1.2 * (100 / 81), delta=0.06 * 50.0 * (450_000 / BASE_PIXELS) ** 1.2 * (100 / 81))
        self.assertIn(plan.penalty, PENALTIES)

    def test_it_needs_six_runs_from_three_executions(self) -> None:
        self.assertEqual(plan_of(spread(5, pixels=PIXELS, frames=FRAMES, per_execution=1), workload=dt_workload(pixels=450_000)).tier, "heuristic")
        self.assertEqual(plan_of(spread(6, pixels=PIXELS, frames=FRAMES, per_execution=3), workload=dt_workload(pixels=450_000)).tier, "heuristic")
        self.assertEqual(plan_of(spread(6, pixels=PIXELS, frames=FRAMES, per_execution=2), workload=dt_workload(pixels=450_000)).tier, "backend_fit")

    def test_a_knob_that_never_varies_is_not_extrapolated(self) -> None:
        history = spread(12, pixels=PIXELS, frames=FRAMES)
        self.assertEqual(plan_of(history, workload=dt_workload(pixels=450_000, guidance=7.5)).tier, "backend_fit")
        self.assertEqual(plan_of(history, workload=dt_workload(pixels=450_000, guidance=3.5)).tier, "heuristic")

    def test_a_knob_that_varies_is_part_of_the_fit(self) -> None:
        history = []
        for index, run in enumerate(spread(12, pixels=PIXELS, frames=FRAMES)):
            guidance = 7.5 if index % 2 == 0 else 3.5
            history.append(dt_run(run.execution, run.run, step=50.0 * (run.workload["pixels"] / BASE_PIXELS) ** 1.2 * (run.workload["frames"] / 81) * (guidance / 7.5) ** 0.5, workload={**run.workload, "guidance_scale": guidance}))
        plan = plan_of(history, workload=dt_workload(pixels=450_000, frames=100, guidance=5.0))
        self.assertEqual(plan.tier, "backend_fit")
        assert plan.log_step is not None
        expected = 50.0 * (450_000 / BASE_PIXELS) ** 1.2 * (100 / 81) * (5.0 / 7.5) ** 0.5
        self.assertAlmostEqual(math.exp(plan.log_step), expected, delta=0.06 * expected)

    def test_live_rows_that_are_too_few_are_joined_by_log_rows_over_the_features_both_hold(self) -> None:
        live = spread(4, pixels=PIXELS, frames=[81], per_execution=2)
        logs = [log_run(10 + index, 1, step=50.0 * (p / BASE_PIXELS) ** 1.2, pixels=p, frames=81) for index, p in enumerate(PIXELS * 2)]
        plan = plan_of(live + logs, workload=dt_workload(pixels=450_000, frames=81))
        self.assertEqual((plan.tier, plan.fidelity, plan.runs), ("backend_fit", "pooled", 12))

    def test_enough_live_rows_are_fitted_without_the_log_ones(self) -> None:
        live = spread(12, pixels=PIXELS, frames=FRAMES)
        logs = [log_run(20 + index, 1, step=999.0) for index in range(4)]
        plan = plan_of(live + logs, workload=dt_workload(pixels=450_000, frames=100))
        self.assertEqual((plan.fidelity, plan.runs), ("live", 12))


class TierThreeTests(unittest.TestCase):
    def test_pipelines_of_one_backend_are_fitted_together_with_an_indicator(self) -> None:
        i2v = spread(4, pixels=PIXELS, frames=[81], per_execution=2)
        others = [replace(run, pipeline="i2i", workload={**run.workload, "mode": "i2i"}, execution=run.execution + 10) for run in spread(4, pixels=PIXELS, frames=[81], per_execution=2)]
        plan = plan_of(i2v + others, workload=dt_workload(pixels=450_000, frames=81))
        self.assertEqual((plan.tier, plan.runs), ("pipeline_fit", 8))

    def test_a_pipeline_with_enough_history_of_its_own_stays_in_tier_two(self) -> None:
        history = spread(12, pixels=PIXELS, frames=FRAMES) + [replace(run, pipeline="i2i", workload={**run.workload, "mode": "i2i"}, execution=run.execution + 10) for run in spread(6, pixels=PIXELS, frames=FRAMES, per_execution=2)]
        self.assertEqual(plan_of(history, workload=dt_workload(pixels=450_000, frames=100)).tier, "backend_fit")


class OtherBackendTests(unittest.TestCase):
    def test_the_other_backends_history_never_qualifies(self) -> None:
        history = [h3_run(execution, run) for execution in range(1, 8) for run in (1, 2)]
        plan = plan_of(history)
        self.assertEqual((plan.tier, plan.runs, plan.executions), ("heuristic", 0, 0))
        self.assertEqual(iterative(plan).steps, 10)

    def test_an_h3_run_is_priced_from_h3_history(self) -> None:
        history = [h3_run(1, 1, step=58.0), h3_run(2, 1, step=60.0), h3_run(2, 2, step=62.0)] + [dt_run(5, 1, step=1.0), dt_run(6, 1, step=1.0), dt_run(7, 1, step=1.0)]
        plan = plan_of(history, backend=H3, pipeline="r2av", workload=h3_workload(), environment={"model": "h3-model", "executable_bytes": 200, "executable_mtime": 2.0})
        self.assertEqual(plan.tier, "signature")
        assert plan.log_step is not None
        self.assertAlmostEqual(math.exp(plan.log_step), 60.0, delta=0.6)
        self.assertEqual((iterative(plan).stage, iterative(plan).steps), ("denoise", 30))


class SpreadTests(unittest.TestCase):
    @staticmethod
    def grouped(signatures: int) -> list[HistoryRun]:
        """Runs of ``signatures`` signatures, each from three executions of two runs: the execution shifts the log step time
        by -0.06, 0, or 0.06 and the run by -0.03 or 0.03."""
        runs = []
        for signature in range(signatures):
            base = 50.0 * (1 + signature)
            for execution, shift in enumerate((-0.06, 0.0, 0.06)):
                for number, own in enumerate((-0.03, 0.03)):
                    runs.append(dt_run(signature * 10 + execution + 1, number + 1, step=base * math.exp(shift + own), workload=dt_workload(pixels=BASE_PIXELS - 10_000 * signature)))
        return runs

    def test_until_three_signatures_have_runs_from_two_executions_the_speed_factor_has_its_prior(self) -> None:
        plan = plan_of(self.grouped(2))
        self.assertEqual((plan.tau, plan.rho), (0.10, 0.5))

    def test_from_then_on_it_is_read_from_the_runs_grouped_by_execution(self) -> None:
        plan = plan_of(self.grouped(4))
        self.assertAlmostEqual(plan.tau, math.sqrt(0.0045), places=3)
        self.assertAlmostEqual(plan.rho, 0.6, places=2)


class WithinStageTests(unittest.TestCase):
    @staticmethod
    def sloped(slope: float, executions: int, *, alternate: bool = False) -> list[HistoryRun]:
        runs = []
        for execution in range(1, executions + 1):
            sign = -1.0 if alternate and execution % 2 == 0 else 1.0
            gaps = [50.0 * math.exp(sign * slope * ((index + 0.5) / 10 - 0.5)) for index in range(10)]
            runs.extend(dt_run(execution, number, gaps=gaps) for number in (1, 2))
        return runs

    def test_a_trend_that_every_execution_shows_is_applied(self) -> None:
        self.assertAlmostEqual(plan_of(self.sloped(0.2, 4)).slope, 0.2, places=2)

    def test_a_trend_whose_sign_changes_from_one_execution_to_the_next_is_not(self) -> None:
        self.assertEqual(plan_of(self.sloped(0.2, 4, alternate=True)).slope, 0.0)

    def test_no_trend_is_applied_from_fewer_than_three_executions(self) -> None:
        self.assertEqual(plan_of(self.sloped(0.2, 2)).slope, 0.0)

    def test_the_first_gap_is_its_own_coefficient_once_three_runs_show_it(self) -> None:
        def runs(count: int) -> list[HistoryRun]:
            return [h3_run(index + 1, 1, gaps=[44.4] + [60.0] * 29) for index in range(count)]

        environment = {"model": "h3-model", "executable_bytes": 200, "executable_mtime": 2.0}
        self.assertAlmostEqual(plan_of(runs(3), backend=H3, pipeline="r2av", workload=h3_workload(), environment=environment).first_ratio or 0.0, 0.74, places=2)
        self.assertIsNone(plan_of(runs(2), backend=H3, pipeline="r2av", workload=h3_workload(), environment=environment).first_ratio)


class RobustnessTests(unittest.TestCase):
    def test_a_row_with_marks_that_do_not_rise_is_left_out_of_the_pace_and_nothing_raises(self) -> None:
        broken = replace(dt_run(9, 1), stages=tuple(replace(stage, marks=((5.0, 0), (3.0, 1))) if stage.stage == "sampling" else stage for stage in dt_run(9, 1).stages))
        plan = plan_of([dt_run(1, 1), dt_run(2, 1), dt_run(3, 1), broken], skipped=2)
        self.assertEqual((plan.tier, plan.runs, plan.skipped), ("signature", 3, 2))

    def test_an_empty_history_gives_a_broad_backend_heuristic(self) -> None:
        plan = plan_of([])
        self.assertEqual((plan.tier, plan.runs, plan.executions), ("heuristic", 0, 0))
        self.assertEqual(iterative(plan).steps, 10)
        self.assertGreater(plan.log_step_var, 0.5)
        self.assertIsNotNone(plan.log_step)

    def test_a_row_with_odd_workload_values_does_not_raise(self) -> None:
        odd = replace(dt_run(9, 1), workload={"pixels": float("nan"), "frames": "many", "knobs": "none"})
        self.assertIsNotNone(plan_of([odd, dt_run(1, 1)]))


class SpeedTests(unittest.TestCase):
    def test_the_fit_of_500_runs_takes_at_most_200_milliseconds(self) -> None:
        history = synthetic_history().runs[:500]
        target = history[-1]
        best = math.inf
        for _ in range(5):
            started = time.process_time()
            model = FittedModel(history)
            model.plan(target.backend, target.pipeline, target.workload, target.environment)
            for run in history[-40:]:
                model.plan(run.backend, run.pipeline, run.workload, run.environment)
            best = min(best, time.process_time() - started)
        self.assertLess(best, 0.2)

    def test_the_planned_step_is_close_to_the_next_runs_on_the_synthetic_history(self) -> None:
        runs = synthetic_history().runs
        history, held = runs[:1500], [run for run in runs[1500:1700] if run.backend == DT]
        model = FittedModel(history)
        errors = []
        for run in held:
            plan = model.plan(run.backend, run.pipeline, run.workload, run.environment)
            if plan.log_step is None:
                continue
            sampling = next(stage for stage in run.counted_stages if stage.unit == "steps")
            assert sampling.total is not None
            errors.append(abs(math.exp(plan.log_step) * sampling.total / sampling.duration - 1))
        self.assertGreater(len(errors), 20)
        self.assertLess(float(np.median(errors)), 0.12)


class FirstGapSpreadTests(unittest.TestCase):
    ENVIRONMENT = {"model": "h3-model", "executable_bytes": 200, "executable_mtime": 2.0}

    def plan(self, ratios: list[float]) -> Plan:
        history = [h3_run(index + 1, 1, gaps=[60.0 * ratio] + [60.0] * 29) for index, ratio in enumerate(ratios)]
        return plan_of(history, backend=H3, pipeline="r2av", workload=h3_workload(), environment=self.ENVIRONMENT)

    def test_the_first_gap_has_the_spread_its_ratio_shows_from_run_to_run(self) -> None:
        plan = self.plan([0.60, 0.74, 0.88, 0.60, 0.74, 0.88])
        expected = float(np.std(np.log([0.60, 0.74, 0.88, 0.60, 0.74, 0.88]), ddof=1))
        self.assertAlmostEqual(plan.first_sd, expected, places=6)
        self.assertGreater(plan.first_sd, 0.1)

    def test_a_ratio_that_never_varies_has_the_least_spread(self) -> None:
        self.assertEqual(self.plan([0.74] * 6).first_sd, MIN_FIRST_SD)

    def test_with_no_first_ratio_known_there_is_no_spread_to_speak_of(self) -> None:
        self.assertEqual(self.plan([0.74, 0.74]).first_sd, 0.0)


class PauseShareTests(unittest.TestCase):
    def test_the_share_of_a_stage_that_pauses_added_is_the_mean_over_the_runs(self) -> None:
        history = [dt_run(index + 1, 1, gaps=[50.0] * 10 + ([550.0] if index % 2 == 0 else [50.0]) + [50.0] * 9) for index in range(4)]
        plan = plan_of(history, workload=dt_workload(steps=20))
        # Half the runs paused for 500 s beyond a step in a stage of 20 steps of 50 s.
        self.assertAlmostEqual(plan.pause_share, 0.5 * 500.0 / (20 * 50.0), places=6)

    def test_a_backend_whose_runs_never_paused_has_none(self) -> None:
        self.assertEqual(plan_of([dt_run(index + 1, 1) for index in range(4)]).pause_share, 0.0)
