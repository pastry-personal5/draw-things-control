"""The calibration fit: what opens with how much evidence, what it is clamped to, and what a pin holds."""

from __future__ import annotations

import math
import time
import unittest
from dataclasses import replace
from typing import Any

import pytest

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND
from framecontrol.jobs.timing.calibrate import CHECKPOINTS, MAX_SHIFT, VALIDATED_MAX_MOVE, calibrate, calibrate_rows, calibrate_validated, checkpoint_reached
from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.features import HistoryRun, feature_values
from framecontrol.jobs.timing.fit import read_row
from framecontrol.jobs.timing.gate import compare_replays
from framecontrol.jobs.timing.knobs import KNOBS_LEARNED, KNOBS_PROVISIONAL, PipelineCalibration
from framecontrol.jobs.timing.step_terms import learned_terms
from tests.timing.builders import dt_run, dt_workload, h3_run, h3_workload

BASE = factory_calibration().backends[DRAW_THINGS_BACKEND].pipelines["i2v"]
H3_BASE = factory_calibration().backends[H3_BACKEND].pipelines["r2av"]
REFERENCE = (BASE.step.pixels, BASE.step.frames)
STAMP = "2026-10-18T12:00:00+09:00"
# dt_workload makes a square-ish size from the pixel count.
SIZE = f"{round(math.sqrt(372_736))}x{372_736 // round(math.sqrt(372_736))}"


def truth(pixels: int, frames: int, *, pixel_exponent: float = 1.0, frame_exponent: float = 1.0, scale: float = 1.0, extra: float = 1.0) -> float:
    return BASE.step.seconds * scale * extra * (pixels / REFERENCE[0]) ** pixel_exponent * (frames / REFERENCE[1]) ** frame_exponent


def run_at(execution: int, run: int, pixels: int, frames: int, step: float, **extra: Any) -> HistoryRun:
    workload = dt_workload(pixels=pixels, frames=frames, steps=12, **extra)
    return dt_run(execution, run, step=step, workload=workload)


def fit(runs: list[HistoryRun], *, checkpoint: int = 1, pins: dict[str, float] | None = None, **tunables: float) -> PipelineCalibration:
    result = calibrate(BASE, None, runs, replace(factory_tunables(), **tunables), checkpoint=checkpoint, stamp=STAMP, pins=pins)
    assert result is not None
    return result


class CheckpointTests(unittest.TestCase):
    def test_the_checkpoints_are_the_powers_of_two_to_32(self) -> None:
        self.assertEqual(CHECKPOINTS, (1, 2, 4, 8, 16, 32))
        self.assertEqual([checkpoint_reached(count) for count in (0, 1, 3, 4, 31, 32, 99)], [0, 1, 2, 4, 16, 32, 32])

    def test_a_validated_fit_selects_on_training_and_bounds_one_update(self) -> None:
        runs = [run_at(execution, 1, REFERENCE[0], REFERENCE[1], BASE.step.seconds * 5) for execution in range(1, 9)]
        result = calibrate_validated(BASE, runs, factory_tunables(), checkpoint=32, stamp=STAMP)
        assert result is not None
        self.assertEqual(result.source, KNOBS_LEARNED)
        self.assertLessEqual(abs(result.step.shift - BASE.step.shift), VALIDATED_MAX_MOVE + 1e-12)
        self.assertLessEqual(abs(math.log(result.step.seconds / BASE.step.seconds)), VALIDATED_MAX_MOVE + 1e-12)

    def test_prepared_rows_produce_the_same_calibration_as_runs(self) -> None:
        runs = [run_at(execution, 1, REFERENCE[0] + execution * 1024, REFERENCE[1] + execution, BASE.step.seconds + execution) for execution in range(1, 9)]
        tunables = factory_tunables()

        direct = calibrate(BASE, BASE, runs, tunables, checkpoint=8, stamp=STAMP)
        prepared = calibrate_rows(BASE, BASE, [read_row(run, tunables) for run in runs], tunables, checkpoint=8, stamp=STAMP)

        self.assertEqual(prepared, direct)

    def test_a_validated_fit_is_shrunk_toward_the_approved_model_not_zero(self) -> None:
        approved = replace(
            BASE,
            step=replace(
                BASE.step,
                shift=0.5,
                aspect=0.8,
                render_exponent=0.5,
                reference_exponent=0.3,
                knobs={"guidance_scale": 0.2},
                sizes={"512x768": 0.4, "768x512": -0.3},
            ),
        )
        runs = []
        for execution in range(1, 13):
            width = 512 if execution % 2 else 768
            height = 768 if execution % 3 else 512
            workload = dt_workload(pixels=width * height, frames=17 + 16 * (execution % 3), steps=25, width=width, height=height, guidance=5 + execution % 2)
            step = approved.step.rate(workload["pixels"], workload["frames"], learned_terms(approved.step, workload, feature_values(workload)))
            runs.append(dt_run(execution, 1, step=step, workload=workload))

        result = calibrate_validated(approved, runs, factory_tunables(), checkpoint=32, stamp=STAMP)

        assert result is not None
        for name in ("pixel_exponent", "frame_exponent", "shift", "aspect", "render_exponent", "reference_exponent"):
            self.assertAlmostEqual(getattr(result.step, name), getattr(approved.step, name), places=7, msg=name)
        self.assertEqual(set(result.step.knobs), set(approved.step.knobs))
        for name, value in approved.step.knobs.items():
            self.assertAlmostEqual(result.step.knobs[name], value, places=7, msg=name)
        for name, value in approved.step.sizes.items():
            self.assertAlmostEqual(result.step.sizes[name], value, places=7, msg=name)
        self.assertTrue(all(abs(value) < 1e-7 for name, value in result.step.sizes.items() if name not in approved.step.sizes))

    @pytest.mark.timing_performance
    def test_a_300_run_validated_fit_and_full_holdout_replay_fit_the_cpu_budget(self) -> None:
        runs = [h3_run(execution, 1, step=130.0 + execution % 7, workload=h3_workload(pixels=832 * 448, frames=362, steps=30, references=3)) for execution in range(1, 301)]
        factory = factory_calibration()
        backend = factory.backends[H3_BACKEND]
        started = time.process_time()
        fitted = calibrate_validated(H3_BASE, runs[:-8], factory_tunables(), checkpoint=32, stamp=STAMP)
        assert fitted is not None
        candidate = replace(factory, backends={**factory.backends, H3_BACKEND: replace(backend, pipelines={**backend.pipelines, "r2av": fitted})})
        comparison = compare_replays(runs[-8:], factory_tunables(), factory, factory_tunables(), candidate)
        elapsed = time.process_time() - started
        self.assertEqual((len(comparison.execution_ids), comparison.candidate.observations), (8, 48))
        self.assertLess(elapsed, 3.0, f"300-run fit and held-out replay took {elapsed:.3f} CPU seconds")


class OneExecutionTests(unittest.TestCase):
    def test_a_single_run_moves_only_the_local_point(self) -> None:
        result = fit([run_at(1, 1, REFERENCE[0], REFERENCE[1], BASE.step.seconds * 2)])
        self.assertEqual((result.source, result.runs, result.executions, result.checkpoint, result.updated_at), (KNOBS_PROVISIONAL, 1, 1, 1, STAMP))
        step = result.step
        # The factory counts as one more execution, so one run that took twice the time moves halfway to it in the log.
        self.assertAlmostEqual(step.shift, 0.5 * math.log(2), places=6)
        self.assertEqual((step.pixel_exponent, step.frame_exponent, step.aspect, step.render_exponent, step.reference_exponent), (1.0, 1.0, 0.0, 0.0, 0.0))
        self.assertEqual((dict(step.knobs), dict(step.sizes)), ({}, {}))

    def test_many_runs_of_one_execution_are_one_independent_measurement(self) -> None:
        one = fit([run_at(1, 1, REFERENCE[0], REFERENCE[1], BASE.step.seconds * 2)])
        many = fit([run_at(1, number, REFERENCE[0], REFERENCE[1], BASE.step.seconds * 2) for number in range(1, 9)], checkpoint=8)
        self.assertAlmostEqual(many.step.shift, one.step.shift, delta=0.02)
        self.assertEqual(many.executions, 1)

    def test_a_run_ten_times_slower_moves_nothing_beyond_the_clamp(self) -> None:
        result = fit([run_at(1, 1, REFERENCE[0], REFERENCE[1], BASE.step.seconds * 10)])
        self.assertLessEqual(abs(result.step.shift), MAX_SHIFT)
        wild = fit([run_at(1, 1, REFERENCE[0], REFERENCE[1], BASE.step.seconds * 1e6)])
        self.assertEqual(wild.step.shift, MAX_SHIFT)
        self.assertEqual((wild.step.pixel_exponent, wild.step.frame_exponent), (1.0, 1.0))

    def test_a_run_with_no_steps_teaches_nothing(self) -> None:
        run = dt_run(1, 1, extra_stages=())
        stripped = replace(run, stages=tuple(stage for stage in run.stages if stage.unit != "steps"))
        self.assertIsNone(calibrate(BASE, None, [stripped], factory_tunables(), checkpoint=1, stamp=STAMP))

    def test_a_log_import_is_not_evidence(self) -> None:
        self.assertIsNone(calibrate(BASE, None, [replace(run_at(1, 1, REFERENCE[0], REFERENCE[1], 10.0), fidelity="log")], factory_tunables(), checkpoint=1, stamp=STAMP))


class IndependentTermTests(unittest.TestCase):
    PIXELS = (163_840, 245_760, 372_736, 589_824)
    FRAMES = (17, 33, 49, 81)

    def varied(self, executions: int, *, pixel_exponent: float = 1.4, frame_exponent: float = 0.7) -> list[HistoryRun]:
        runs = []
        for execution in range(1, executions + 1):
            pixels, frames = self.PIXELS[execution % len(self.PIXELS)], self.FRAMES[(execution // 2) % len(self.FRAMES)]
            runs.append(run_at(execution, 1, pixels, frames, truth(pixels, frames, pixel_exponent=pixel_exponent, frame_exponent=frame_exponent)))
        return runs

    def test_two_executions_open_no_slope_even_when_their_sizes_differ(self) -> None:
        result = fit(self.varied(2), checkpoint=2)
        self.assertEqual((result.step.pixel_exponent, result.step.frame_exponent), (1.0, 1.0))

    def test_three_independent_executions_that_vary_open_the_exponents_toward_the_truth(self) -> None:
        result = fit(self.varied(8), checkpoint=8)
        self.assertGreater(result.step.pixel_exponent, 1.0)
        self.assertLess(result.step.pixel_exponent, 1.4 + 0.05)
        self.assertLess(result.step.frame_exponent, 1.0)
        self.assertGreater(result.step.frame_exponent, 0.7 - 0.05)

    def test_more_evidence_moves_the_exponents_closer(self) -> None:
        few, many = fit(self.varied(4), checkpoint=4), fit(self.varied(16), checkpoint=16)
        self.assertLess(abs(many.step.frame_exponent - 0.7), abs(few.step.frame_exponent - 0.7))

    def test_the_prior_weight_cap_sets_how_far_toward_the_data_it_goes(self) -> None:
        runs = self.varied(8)
        stiff, loose = fit(runs, checkpoint=8, prior_weight_cap=100.0), fit(runs, checkpoint=8, prior_weight_cap=1.0)
        self.assertLess(abs(loose.step.frame_exponent - 0.7), abs(stiff.step.frame_exponent - 0.7))

    def test_a_pinned_exponent_is_held_and_the_rest_is_fitted_around_it(self) -> None:
        runs = self.varied(8)
        result = fit(runs, checkpoint=8, pins={"step/frame_exponent": 0.9})
        self.assertEqual(result.step.frame_exponent, 0.9)
        self.assertNotEqual(result.step.pixel_exponent, 1.0)

    def test_a_pinned_step_time_is_the_base_the_shift_is_fitted_from(self) -> None:
        runs = self.varied(3, pixel_exponent=1.0, frame_exponent=1.0)
        result = fit(runs, checkpoint=2, pins={"step/seconds": BASE.step.seconds * 2})
        self.assertEqual(result.step.seconds, BASE.step.seconds * 2)
        self.assertLess(result.step.shift, 0.0)

    def test_a_size_used_often_gets_a_shrunk_offset_and_a_new_size_follows_the_curve(self) -> None:
        runs = [run_at(execution, 1, 372_736, 33, truth(372_736, 33, extra=1.5)) for execution in range(1, 7)] + [run_at(7, 1, 589_824, 33, truth(589_824, 33))]
        result = fit(runs, checkpoint=4)
        offset = result.step.sizes[SIZE]
        self.assertGreater(offset, 0.0)
        self.assertNotIn("0x0", result.step.sizes)
        # The shift and the offset together carry the size's 1.5x (log 0.405).
        self.assertAlmostEqual(result.step.shift + offset, math.log(1.5), delta=0.1)

    def test_the_offset_is_shrunk_by_n_over_n_plus_k(self) -> None:
        runs = [run_at(execution, 1, 372_736, 33, truth(372_736, 33)) for execution in range(1, 4)] + [run_at(execution, 1, 372_736, 33, truth(372_736, 33)) for execution in range(4, 7)]
        result = fit(runs, checkpoint=4, size_offset_shrinkage=3.0)
        loose = fit(runs, checkpoint=4, size_offset_shrinkage=0.5)
        self.assertLessEqual(abs(result.step.sizes[SIZE]), abs(loose.step.sizes[SIZE]) + 1e-9)

    def test_a_knob_effect_opens_only_where_it_varies_in_three_executions(self) -> None:
        def with_knob(executions: int) -> list[HistoryRun]:
            runs = []
            for execution in range(1, executions + 1):
                guidance = 5.0 if execution % 2 else 9.0
                runs.append(run_at(execution, 1, 294_912, 33, truth(294_912, 33, extra=1.0 + 0.1 * (guidance - 5.0)), guidance=guidance))
            return runs

        self.assertEqual(dict(fit(with_knob(2), checkpoint=2).step.knobs), {})
        opened = fit(with_knob(6), checkpoint=4)
        self.assertIn("guidance_scale", opened.step.knobs)
        self.assertGreater(opened.step.knobs["guidance_scale"], 0.0)
        self.assertEqual(opened.observed["guidance_scale"], (5.0, 9.0))

    def test_older_evidence_fades(self) -> None:
        old = [run_at(execution, 1, 294_912, 33, truth(294_912, 33, scale=3.0)) for execution in range(1, 6)]
        new = [run_at(execution, 1, 294_912, 33, truth(294_912, 33)) for execution in range(6, 11)]
        slow, fast = fit(old + new, checkpoint=8, half_life_runs=1000.0), fit(old + new, checkpoint=8, half_life_runs=2.0)
        self.assertLess(abs(fast.step.shift), abs(slow.step.shift))


class StageTests(unittest.TestCase):
    def test_a_stage_is_the_measured_median_blended_with_the_factory_and_keeps_the_runs_order(self) -> None:
        result = fit([run_at(execution, 1, 294_912, 33, 13.0) for execution in range(1, 5)], checkpoint=4)
        keys = [stage.key for stage in result.stages]
        self.assertLess(keys.index("starting"), keys.index("sampling"))
        self.assertLess(keys.index("sampling"), keys.index("finishing"))
        starting = next(stage for stage in result.stages if stage.key == "starting")
        # dt_run's starting stage takes 7 s; the factory said 3.14 and counts as one execution among five.
        self.assertGreater(starting.seconds, 3.14)
        self.assertLess(starting.seconds, 7.0)
        self.assertTrue(next(stage for stage in result.stages if stage.key == "sampling").iterative)

    def test_a_stage_of_the_factory_the_run_lacked_stays(self) -> None:
        result = fit([run_at(1, 1, 294_912, 33, 13.0)])
        self.assertIn("correct", [stage.key for stage in result.stages])

    def test_h3_stages_are_fitted_from_its_own_names(self) -> None:
        runs = [h3_run(execution, 1, step=140.0, workload=h3_workload(pixels=832 * 448, frames=362, steps=30, references=3)) for execution in range(1, 4)]
        result = calibrate(H3_BASE, None, runs, factory_tunables(), checkpoint=2, stamp=STAMP)
        assert result is not None
        self.assertIn("denoise", [stage.key for stage in result.stages])
        self.assertAlmostEqual(result.step.shift, 0.0, delta=0.1)


class SummaryTests(unittest.TestCase):
    def test_summaries_are_bounded_and_keep_a_signatures_medians(self) -> None:
        runs = [run_at(execution, 1, 294_912, 33, 13.0) for execution in range(1, 6)] + [run_at(6, 1, 589_824, 33, 26.0)]
        result = fit(runs, checkpoint=4)
        counts = sorted((summary.count for summary in result.summaries.values()), reverse=True)
        self.assertEqual(counts, [5, 1])
        biggest = max(result.summaries.values(), key=lambda summary: summary.count)
        self.assertEqual(biggest.executions, 5)
        self.assertAlmostEqual(biggest.step_seconds or 0.0, 13.0, delta=0.5)
        self.assertIn("sampling", biggest.stages)
