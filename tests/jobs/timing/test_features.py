"""History rows, the signature that groups runs, and the per-step statistics and features a forecast fits on."""

from __future__ import annotations

import math
import unittest
from typing import Any

from framecontrol.jobs.timing.features import HistoryRun, HistoryStage, evaluated_count, expected_stages, feature_values, reuse_interval, signature, stage_keys, step_series


def marks(start: float, gaps: list[float], first_done: int = 0) -> tuple[tuple[float, int], ...]:
    """Marks at ``start`` and after each gap, one step done per gap."""
    time, result = start, [(start, first_done)]
    for index, gap in enumerate(gaps):
        time += gap
        result.append((round(time, 6), first_done + index + 1))
    return tuple(result)


def stage(name: str, start: float, end: float, *, source: str = "child", done: int | None = None, total: int | None = None, unit: str | None = None, mark: tuple[tuple[float, int], ...] = (), uncertain: bool = False) -> HistoryStage:
    return HistoryStage(name, source, start, end, done, total, unit, mark, uncertain)


def iterative(gaps: list[float], *, start: float = 10.0, uncertain: bool = False) -> HistoryStage:
    series = marks(start, gaps)
    return stage("sampling", start, series[-1][0], done=len(gaps), total=len(gaps), unit="steps", mark=series, uncertain=uncertain)


def history_run(**changes: Any) -> HistoryRun:
    values: dict[str, Any] = {"execution": 1, "run": 1, "fidelity": "live", "backend": "draw-things-cli", "pipeline": "i2v", "workload": {"width": 832, "height": 448, "pixels": 372736, "frames": 81, "steps": 40, "mode": "i2v"}, "environment": {"model": "base.ckpt"}, "stages": ()}
    values.update(changes)
    return HistoryRun(**values)


class SignatureTests(unittest.TestCase):
    WORKLOAD = {"width": 832, "height": 448, "pixels": 372736, "frames": 243, "steps": 30, "seconds": 10.0, "references": {"image": 2}, "reference_count": 2, "knobs": {"frames": 243, "seed_note": "x", "timeout_seconds": 3600, "reference_pair": "faces", "seconds": 10.0}}

    def test_the_same_workload_in_any_key_order_has_one_signature(self) -> None:
        reordered = dict(reversed(list(self.WORKLOAD.items())))
        self.assertEqual(signature("h3-metal-cli", "r2av", self.WORKLOAD, {"model": "m"}), signature("h3-metal-cli", "r2av", reordered, {"model": "m"}))

    def test_seconds_the_timeout_and_the_reference_pair_name_do_not_split_a_signature(self) -> None:
        other = {**self.WORKLOAD, "seconds": 8.0, "knobs": {**self.WORKLOAD["knobs"], "timeout_seconds": 60, "reference_pair": "other", "seconds": 8.0}}
        self.assertEqual(signature("h3-metal-cli", "r2av", self.WORKLOAD, {"model": "m"}), signature("h3-metal-cli", "r2av", other, {"model": "m"}))

    def test_backend_pipeline_model_and_every_other_workload_value_split_it(self) -> None:
        base = signature("h3-metal-cli", "r2av", self.WORKLOAD, {"model": "m"})
        self.assertNotEqual(base, signature("draw-things-cli", "r2av", self.WORKLOAD, {"model": "m"}))
        self.assertNotEqual(base, signature("h3-metal-cli", "i2av", self.WORKLOAD, {"model": "m"}))
        self.assertNotEqual(base, signature("h3-metal-cli", "r2av", self.WORKLOAD, {"model": "n"}))
        for change in ({"steps": 31}, {"pixels": 1}, {"frames": 121}, {"references": {"image": 1, "video": 1}}, {"knobs": {"frames": 121}}):
            self.assertNotEqual(base, signature("h3-metal-cli", "r2av", {**self.WORKLOAD, **change}, {"model": "m"}), change)

    def test_floats_are_rounded_to_six_places_and_the_executable_is_not_part_of_it(self) -> None:
        one = signature("draw-things-cli", "i2v", {"guidance_scale": 7.5}, {"model": "m", "executable_bytes": 10, "executable_mtime": 1.0})
        two = signature("draw-things-cli", "i2v", {"guidance_scale": 7.5000000001}, {"model": "m", "executable_bytes": 99, "executable_mtime": 2.0, "load_start": [1.0, 1.0, 1.0]})
        self.assertEqual(one, two)
        self.assertNotEqual(one, signature("draw-things-cli", "i2v", {"guidance_scale": 7.6}, {"model": "m"}))

    def test_a_run_with_no_frames_has_the_signature_it_will_have_once_its_frames_are_joined(self) -> None:
        planned = history_run(workload={"width": 832, "height": 448, "pixels": 372736, "frames": None, "steps": 40, "mode": "i2v"})
        loaded = history_run(workload=planned.workload, output_frames=81)
        self.assertEqual(planned.signature, loaded.signature)


class StepSeriesTests(unittest.TestCase):
    def test_a_steady_stage_has_its_mean_step_and_no_trend(self) -> None:
        series = step_series(iterative([50.0] * 20))
        assert series is not None
        self.assertAlmostEqual(series.mean_seconds, 50.0)
        self.assertAlmostEqual(series.first_ratio or 0.0, 1.0)
        self.assertAlmostEqual(series.slope or 0.0, 0.0, places=9)
        self.assertEqual(series.pauses, 0)

    def test_a_pause_counts_toward_the_stage_but_not_toward_the_pace(self) -> None:
        gaps = [50.0] * 10 + [900.0] + [50.0] * 9
        series = step_series(iterative(gaps))
        assert series is not None
        self.assertAlmostEqual(series.mean_seconds, 50.0)
        self.assertEqual(series.pauses, 1)

    def test_the_seconds_a_pause_added_are_what_it_took_beyond_a_step(self) -> None:
        series = step_series(iterative([50.0] * 10 + [900.0] + [50.0] * 9))
        assert series is not None
        self.assertAlmostEqual(series.pause_seconds, 850.0)
        steady = step_series(iterative([50.0] * 20))
        assert steady is not None
        self.assertEqual(steady.pause_seconds, 0.0)

    def test_the_first_gap_is_set_aside_and_compared_with_the_rest(self) -> None:
        series = step_series(iterative([37.0] + [50.0] * 19))
        assert series is not None
        self.assertAlmostEqual(series.mean_seconds, 50.0)
        self.assertAlmostEqual(series.first_ratio or 0.0, 0.74)

    def test_an_estimated_first_mark_gives_no_first_gap_ratio(self) -> None:
        series = step_series(iterative([37.0] + [50.0] * 19, uncertain=True))
        assert series is not None
        self.assertIsNone(series.first_ratio)

    def test_a_rising_step_time_has_a_positive_slope_and_its_noise_is_what_is_left(self) -> None:
        gaps = [40.0 * math.exp(0.2 * ((index + 0.5) / 30 - 0.5)) for index in range(30)]
        series = step_series(iterative(gaps))
        assert series is not None
        self.assertAlmostEqual(series.slope or 0.0, 0.2, places=3)
        self.assertLess(series.noise, 1e-6)

    def test_a_stage_with_too_few_gaps_has_no_slope(self) -> None:
        series = step_series(iterative([50.0, 51.0, 52.0, 53.0]))
        assert series is not None
        self.assertIsNone(series.slope)

    def test_a_stage_with_no_marks_has_no_series(self) -> None:
        self.assertIsNone(step_series(stage("tokenize", 0.0, 1.0, done=1, total=1)))

    def test_a_bulk_reading_is_a_gap_of_several_steps(self) -> None:
        series = step_series(stage("sampling", 0.0, 100.0, done=10, total=10, unit="steps", mark=((0.0, 0), (20.0, 2), (40.0, 4), (100.0, 10))))
        assert series is not None
        self.assertAlmostEqual(series.mean_seconds, 10.0)


class FeatureTests(unittest.TestCase):
    def test_pixels_frames_and_the_knobs_a_workload_holds(self) -> None:
        values = feature_values({"width": 832, "height": 448, "pixels": 372736, "frames": 81, "steps": 40, "mode": "i2v", "guidance_scale": 7.5, "strength": 1.0, "shift": 5.0})
        self.assertAlmostEqual(values["log_pixels"], math.log(372736))
        self.assertAlmostEqual(values["log_frames"], math.log(81))
        self.assertEqual((values["guidance_scale"], values["strength"], values["shift"]), (7.5, 1.0, 5.0))
        self.assertNotIn("steps", values)
        self.assertNotIn("mode", values)

    def test_h3_references_and_render_size_and_flags(self) -> None:
        values = feature_values({"pipeline": "r2av", "width": 832, "height": 448, "pixels": 372736, "frames": 243, "steps": 30, "seconds": 10.0, "references": {"image": 2}, "reference_count": 2, "render_width": 416, "render_height": 224, "ssd_streaming": True, "dit_layers": 36})
        self.assertAlmostEqual(values["log_refs"], math.log1p(2))
        self.assertAlmostEqual(values["log_render_pixels"], math.log(416 * 224))
        self.assertEqual((values["ssd_streaming"], values["dit_layers"]), (1.0, 36.0))
        self.assertNotIn("render_width", values)
        self.assertNotIn("references", values)

    def test_a_missing_frame_count_is_the_joined_one_then_one(self) -> None:
        self.assertAlmostEqual(feature_values({"width": 8, "height": 8, "pixels": 64, "frames": None})["log_frames"], 0.0)
        self.assertAlmostEqual(feature_values({"width": 8, "height": 8, "pixels": 64, "frames": None}, output_frames=81)["log_frames"], math.log(81))

    def test_a_text_knob_is_an_indicator_and_the_run_knobs_are_prefixed(self) -> None:
        values = feature_values({"width": 8, "height": 8, "pixels": 64, "frames": 9, "sampler": "euler", "knobs": {"frames": 9, "token_reduction": 0.5, "timeout_seconds": 60}})
        self.assertEqual(values["sampler=euler"], 1.0)
        self.assertEqual(values["knob.token_reduction"], 0.5)
        self.assertNotIn("knob.timeout_seconds", values)
        self.assertNotIn("knob.frames", values)


class ExpectedStagesTests(unittest.TestCase):
    def test_a_repeated_stage_is_keyed_by_its_occurrence(self) -> None:
        run = history_run(stages=(stage("prepare", 0.0, 1.0, source="framecontrol"), stage("sampling", 1.0, 100.0, done=40, total=40, unit="steps"), stage("sampling", 100.0, 150.0, done=8, total=8, unit="steps"), stage("verify", 150.0, 152.0, source="framecontrol")))
        self.assertEqual(stage_keys(run), ["prepare", "sampling", "sampling#2", "verify"])

    def test_work_done_before_the_run_started_is_not_part_of_the_run(self) -> None:
        run = history_run(stages=(stage("prepare_input", -2.0, -1.0, source="framecontrol"), stage("prepare", 0.0, 1.0, source="framecontrol"), stage("sampling", 1.0, 9.0, done=1, total=1, unit="steps")))
        self.assertEqual(stage_keys(run), ["prepare", "sampling"])

    def test_the_sequence_is_the_newest_complete_run_of_the_signature_else_of_the_backend_and_pipeline(self) -> None:
        old = history_run(execution=1, stages=(stage("prepare", 0.0, 1.0, source="framecontrol"), stage("sampling", 1.0, 9.0, done=1, total=1, unit="steps")))
        new = history_run(execution=2, stages=(stage("prepare", 0.0, 1.0, source="framecontrol"), stage("sampling", 1.0, 9.0, done=1, total=1, unit="steps"), stage("correct", 9.0, 40.0, source="framecontrol")))
        other = history_run(execution=3, workload={**old.workload, "steps": 8}, stages=(stage("prepare", 0.0, 1.0, source="framecontrol"),))
        self.assertEqual([item.stage for item in expected_stages([old, new, other], old.signature, "draw-things-cli", "i2v")], ["prepare", "sampling", "correct"])
        self.assertEqual([item.stage for item in expected_stages([old, other], (("none", None),), "draw-things-cli", "i2v")], ["prepare"])
        self.assertEqual(expected_stages([old, new], (("none", None),), "h3-metal-cli", "r2av"), [])


class ReuseTests(unittest.TestCase):
    """H3 under ``denoise_reuse`` or ``core_reuse`` evaluates the first step, the last, and every interval-th, and the readings
    of the reused steps arrive in the same burst as the evaluated one before them."""

    @staticmethod
    def bursts(interval: int, steps: int = 30, evaluated_seconds: float = 20.0) -> list[float]:
        return [evaluated_seconds if index in (0, steps - 1) or index % interval == 0 else 0.02 for index in range(steps)]

    def test_the_interval_is_the_reuse_knob_that_is_set(self) -> None:
        self.assertEqual(reuse_interval({"denoise_reuse": 2, "core_reuse": 1}), 2)
        self.assertEqual(reuse_interval({"denoise_reuse": 1, "core_reuse": 4}), 4)
        self.assertEqual(reuse_interval({}), 1)

    def test_the_evaluated_steps_are_the_first_the_last_and_every_interval(self) -> None:
        self.assertEqual(evaluated_count(30, 1), 30)
        self.assertEqual(evaluated_count(30, 2), 16)
        self.assertEqual(evaluated_count(30, 4), 9)

    def test_a_burst_stream_gives_the_time_of_an_evaluated_step_and_no_pauses(self) -> None:
        for interval, evaluated_steps in ((2, 16), (4, 9)):
            with self.subTest(interval=interval):
                series = step_series(iterative(self.bursts(interval)), interval)
                assert series is not None
                self.assertAlmostEqual(series.mean_seconds, 20.0, delta=0.5)
                self.assertEqual((series.pauses, series.steps), (0, evaluated_steps))
                self.assertLess(series.noise, 0.01)

    def test_read_without_the_interval_the_same_stream_is_all_pauses_and_the_wrong_pace(self) -> None:
        series = step_series(iterative(self.bursts(4)))
        assert series is not None
        self.assertLess(series.mean_seconds, 5.0)

    def test_the_reuse_knobs_are_not_fit_features(self) -> None:
        values = feature_values({"width": 832, "height": 480, "pixels": 399360, "frames": 124, "steps": 30, "denoise_reuse": 2, "core_reuse": 1, "dit_layers": 50})
        self.assertNotIn("denoise_reuse", values)
        self.assertNotIn("core_reuse", values)
        self.assertIn("dit_layers", values)
