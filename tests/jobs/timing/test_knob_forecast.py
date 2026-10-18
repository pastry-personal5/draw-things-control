"""The forecast on top of the knobs: what a run with no history is priced from, where the numbers came from, and what an unseen
knob does to the range. The refactor onto the tunables and calibration seam changed no number."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from typing import Any

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND
from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.timing.factory import factory_calibration
from framecontrol.jobs.timing.features import HistoryExecution, HistoryStage, step_series
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.forecast import Basis, Forecast, Forecaster, JobPlan, PlannedRun
from framecontrol.jobs.timing.knobs import PipelineCalibration, Tunables
from framecontrol.jobs.timing.replay_stream import replay_job
from tests.timing.builders import dt_run, dt_workload, h3_run, h3_workload
from tests.timing.synthetic import synthetic_history

E0053 = {"width": 384, "height": 768, "pixels": 294912, "frames": 33, "steps": 25, "mode": "i2v", "guidance_scale": 5, "strength": 1, "shift": 3.99, "color_corrects": True}
E0053_ENVIRONMENT = {"model": "wan_v2.2_a14b_hne_i2v_i8x.ckpt", "executable_bytes": 160477120, "executable_mtime": 1790314797.2408893}
OTHER = {"model": "other.ckpt", "executable_bytes": 1, "executable_mtime": 2.0}


def learned(backend: str, pipeline: str, **step: Any) -> FittedModel:
    calibration = factory_calibration()
    anchor = calibration.backends[backend]
    base = anchor.baseline_pipeline(pipeline)
    updated = replace(base, step=replace(base.step, **step), source="provisional", runs=8, executions=4, checkpoint=8, updated_at="2026-10-18T12:00:00+09:00", observed={"guidance_scale": (5.0,)})
    return FittedModel([], calibration=replace(calibration, backends={**calibration.backends, backend: replace(anchor, pipelines={**anchor.pipelines, pipeline: updated})}))


class NoFilesTests(unittest.TestCase):
    def test_a_measured_workload_is_priced_narrowly_from_its_factory_point(self) -> None:
        plan = FittedModel([]).plan(DRAW_THINGS_BACKEND, "i2v", E0053, E0053_ENVIRONMENT)
        self.assertEqual((plan.tier, plan.knobs, plan.knobs_updated_at), ("factory", "factory", None))
        self.assertEqual(plan.log_step_var, 0.25**2)

    def test_the_same_workload_on_another_build_is_a_broad_heuristic_from_the_same_numbers(self) -> None:
        exact = FittedModel([]).plan(DRAW_THINGS_BACKEND, "i2v", E0053, E0053_ENVIRONMENT)
        broad = FittedModel([]).plan(DRAW_THINGS_BACKEND, "i2v", E0053, OTHER)
        self.assertEqual((broad.tier, broad.knobs), ("heuristic", "factory"))
        self.assertAlmostEqual(math.exp(broad.log_step or 0.0 + 0.0) / math.exp(exact.log_step or 0.0), math.exp(-(0.75**2 + 0.1**2) / 2), places=6)
        self.assertGreater(broad.log_step_var, exact.log_step_var)

    def test_every_supported_pipeline_with_a_priceable_workload_gets_a_number(self) -> None:
        cases = [(DRAW_THINGS_BACKEND, "i2v", dt_workload(pixels=294_912, frames=33, steps=20)), (DRAW_THINGS_BACKEND, "t2v", dt_workload(pixels=294_912, frames=33, steps=20)), (DRAW_THINGS_BACKEND, "i2i", dt_workload(pixels=294_912, frames=1, steps=20)), (H3_BACKEND, "r2av", h3_workload(frames=362)), (H3_BACKEND, "i2av", h3_workload(frames=362, pipeline="i2av"))]
        for backend, pipeline, workload in cases:
            with self.subTest(backend=backend, pipeline=pipeline):
                plan = FittedModel([]).plan(backend, pipeline, workload, OTHER)
                self.assertEqual(plan.tier, "heuristic")
                self.assertIsNotNone(plan.log_step)
                self.assertTrue(any(stage.__class__.__name__ == "IterativeStage" for stage in plan.stages))

    def test_a_workload_missing_what_it_needs_to_be_priced_stays_live_only(self) -> None:
        for workload in ({"steps": 20}, {"pixels": 100, "frames": 3}, {**dt_workload(), "steps": 0}, {**dt_workload(), "frames": None}):
            with self.subTest(workload=workload):
                self.assertEqual(FittedModel([]).plan(DRAW_THINGS_BACKEND, "i2v", workload, OTHER).tier, "live_only")
        self.assertEqual(FittedModel([]).plan("some-future-backend", "i2v", dt_workload(), OTHER).tier, "live_only")

    def test_a_pipeline_the_factory_borrows_widens_the_range(self) -> None:
        same = FittedModel([]).plan(H3_BACKEND, "r2av", h3_workload(frames=362), OTHER)
        borrowed = FittedModel([]).plan(H3_BACKEND, "i2av", h3_workload(frames=362, pipeline="i2av"), OTHER)
        self.assertGreater(borrowed.log_step_var, same.log_step_var)
        self.assertEqual(borrowed.slope, 0.0)


class ProvisionalTests(unittest.TestCase):
    def test_a_learned_step_time_prices_the_run_and_names_its_source_and_time(self) -> None:
        model = learned(H3_BACKEND, "r2av", shift=0.5)
        plan = model.plan(H3_BACKEND, "r2av", h3_workload(frames=362), OTHER)
        factory = FittedModel([]).plan(H3_BACKEND, "r2av", h3_workload(frames=362), OTHER)
        self.assertEqual((plan.knobs, plan.knobs_updated_at, plan.tier), ("provisional", "2026-10-18T12:00:00+09:00", "heuristic"))
        self.assertAlmostEqual((plan.log_step or 0.0) - (factory.log_step or 0.0), 0.5, places=6)

    def test_the_size_offset_prices_a_seen_size_and_a_new_size_follows_the_curve(self) -> None:
        width, height = 610, 611
        model = learned(DRAW_THINGS_BACKEND, "i2v", sizes={f"{width}x{height}": 0.4})

        def plan(owner: FittedModel, pixels: int):
            return owner.plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(pixels=pixels, frames=33, guidance=5.0), OTHER)

        seen, flat = plan(model, width * height), plan(FittedModel([]), width * height)
        new, flat_new = plan(model, 500 * 501), plan(FittedModel([]), 500 * 501)
        self.assertAlmostEqual((seen.log_step or 0.0) - (flat.log_step or 0.0), 0.4, places=6)
        self.assertEqual(new.log_step, flat_new.log_step)

    def test_an_unseen_knob_value_widens_the_range_and_is_named(self) -> None:
        model = learned(DRAW_THINGS_BACKEND, "i2v", knobs={"guidance_scale": 0.1})
        seen = model.plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(pixels=294_912, frames=33, guidance=5.0), OTHER)
        unseen = model.plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(pixels=294_912, frames=33, guidance=9.0), OTHER)
        self.assertEqual((seen.unseen_knobs, unseen.unseen_knobs), ((), ("guidance_scale",)))
        self.assertAlmostEqual(math.sqrt(unseen.log_step_var) / math.sqrt(seen.log_step_var), 1.5, places=6)
        self.assertAlmostEqual((unseen.log_step or 0.0) - (seen.log_step or 0.0), 0.1 * 4.0, delta=0.5)

    def test_the_inflation_is_the_tunable(self) -> None:
        calibration = learned(DRAW_THINGS_BACKEND, "i2v").calibration
        model = FittedModel([], tunables=Tunables(unseen_knob_inflation=3.0), calibration=calibration)
        seen = model.plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(pixels=294_912, frames=33, guidance=5.0), OTHER)
        unseen = model.plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(pixels=294_912, frames=33, guidance=9.0), OTHER)
        self.assertAlmostEqual(math.sqrt(unseen.log_step_var) / math.sqrt(seen.log_step_var), 3.0, places=6)

    def test_an_edited_tunable_is_named_a_pin_on_the_forecast(self) -> None:
        self.assertEqual(FittedModel([], tunables=Tunables(pause_factor=4.0)).plan(DRAW_THINGS_BACKEND, "i2v", E0053, E0053_ENVIRONMENT).knobs, "pinned")
        self.assertEqual(FittedModel([], tunables=Tunables(pause_factor=4.0)).plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(), OTHER).knobs, "pinned")

    def test_the_basis_of_a_forecast_carries_the_knobs_and_an_old_snapshot_still_reads(self) -> None:
        model = learned(H3_BACKEND, "r2av", shift=0.2)
        run = h3_run(9, 1, workload=h3_workload(frames=362))
        execution = HistoryExecution(9, CooldownPolicy("off"), (replace(run, environment=OTHER),))
        plan = JobPlan((PlannedRun(1, run.backend, run.pipeline, dict(run.workload), OTHER),), execution.cooldown)
        forecaster = Forecaster(model, plan)
        for at, event in replay_job(execution, origin=100.0).events[:2]:
            forecaster.observe(event, at)
        forecast = forecaster.forecast(100.5)
        assert forecast is not None
        self.assertEqual((forecast.basis.knobs, forecast.basis.knobs_updated_at), ("provisional", "2026-10-18T12:00:00+09:00"))
        self.assertEqual(Forecast.from_dict(forecast.as_dict()), forecast)
        old = forecast.as_dict()
        del old["basis"]["knobs"], old["basis"]["knobs_updated_at"]
        self.assertEqual(Forecast.from_dict(old).basis, replace(forecast.basis, knobs="factory", knobs_updated_at=None))

    def test_unseen_knobs_join_the_unseen_stage_names_in_the_basis(self) -> None:
        basis = Forecaster._basis(replace(FittedModel([]).plan(DRAW_THINGS_BACKEND, "i2v", dt_workload(), OTHER), unseen_knobs=("guidance_scale",)), ("other",))  # noqa: SLF001
        self.assertEqual(basis.unseen, ("other", "guidance_scale"))
        self.assertIsInstance(basis, Basis)


class SeamTests(unittest.TestCase):
    """Moving the constants behind ``Tunables`` and the factory behind ``Calibration`` changed nothing."""

    def test_the_default_model_is_the_factory_pair(self) -> None:
        history = [run for execution in synthetic_history(20261016, executions=40).executions for run in execution.runs]
        implicit = FittedModel(history)
        explicit = FittedModel(history, tunables=Tunables(), calibration=factory_calibration())
        for run in history[::7]:
            self.assertEqual(implicit.plan(run.backend, run.pipeline, dict(run.workload), dict(run.environment)), explicit.plan(run.backend, run.pipeline, dict(run.workload), dict(run.environment)))

    def test_the_old_constants_are_the_factory_values(self) -> None:
        tunables = Tunables()
        self.assertEqual((tunables.pause_factor, tunables.slope_min_gaps, tunables.penalties, tunables.heuristic_log_spread, tunables.heuristic_stage_spread, tunables.tau, tunables.rho), (3.0, 6, (0.01, 0.1, 1.0, 10.0, 100.0), 0.75, 0.60, 0.10, 0.5))

    def test_the_pause_factor_decides_what_a_pause_is(self) -> None:
        gaps = [10.0] * 8 + [25.0] + [10.0] * 3
        marks = [(0.0, 0)]
        for index, gap in enumerate(gaps, start=1):
            marks.append((marks[-1][0] + gap, index))
        stage = HistoryStage("sampling", "child", 0.0, marks[-1][0], len(gaps), len(gaps), "steps", tuple(marks))
        default, strict = step_series(stage), step_series(stage, pause_factor=2.0)
        assert default is not None and strict is not None
        self.assertEqual((default.pauses, strict.pauses), (0, 1))

    def test_slope_min_gaps_decides_when_a_slope_is_fitted(self) -> None:
        marks = [(float(index) * 10, index) for index in range(9)]
        stage = HistoryStage("sampling", "child", 0.0, 80.0, 8, 8, "steps", tuple(marks))
        self.assertIsNotNone(step_series(stage, slope_min_gaps=6).slope)  # type: ignore[union-attr]
        self.assertIsNone(step_series(stage, slope_min_gaps=20).slope)  # type: ignore[union-attr]

    def test_a_penalty_grid_and_tier_counts_from_the_tunables_change_which_tier_prices(self) -> None:
        history = [dt_run(execution, 1) for execution in range(1, 4)]
        workload = dt_workload()
        env = history[0].environment
        self.assertEqual(FittedModel(history).plan(DRAW_THINGS_BACKEND, "i2v", workload, env).tier, "signature")
        strict = FittedModel(history, tunables=Tunables(min_signature_runs=5, min_signature_executions=4))
        self.assertNotEqual(strict.plan(DRAW_THINGS_BACKEND, "i2v", workload, env).tier, "signature")


class PipelineCalibrationShapeTests(unittest.TestCase):
    def test_a_foreign_pipeline_borrows_the_anchor_with_its_own_exponent_and_is_not_learned(self) -> None:
        backend = factory_calibration().backends[DRAW_THINGS_BACKEND]
        borrowed = backend.pipeline("i2i")
        self.assertIsInstance(borrowed, PipelineCalibration)
        self.assertEqual((borrowed.step.frame_exponent, borrowed.learned, backend.has("i2i")), (0.5, False, False))
