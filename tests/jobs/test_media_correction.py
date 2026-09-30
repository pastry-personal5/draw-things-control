"""Tests for the color correction's numerics: the fit, the caps, the ramp, the smoothing, and a simulated chain."""

from __future__ import annotations

import unittest

import numpy as np

from draw_things_control.jobs.media import oklab
from draw_things_control.jobs.media.color_stats import ColorStats, compare, measure
from draw_things_control.jobs.media.correction import HEADROOM, LIGHTNESS_CAP, Params, Target, apply, fit, plan_run, ramp, refine_gains, sample_pixels, scaled, target_between, tone_curve, transform_for


def scene(seed: int = 7, size: int = 48) -> np.ndarray:
    """A frame as generated ones are: colors around one dominant hue (warm, as skin and lamplight are) at varied
    lightness and chroma, with a near-white, a near-black, and a gray patch, as sRGB values."""
    rng = np.random.default_rng(seed)
    count = size * size
    hue = np.radians(rng.normal(50, 35, count))
    lab = oklab.from_polar(rng.uniform(0.3, 0.9, count), rng.uniform(0.02, 0.12, count), hue)
    base = np.clip(oklab.oklab_to_srgb(lab), 0, 1).reshape(size, size, 3)
    base[: size // 4, : size // 4] = 0.97
    base[-size // 4 :, -size // 4 :] = 0.04
    base[: size // 4, -size // 4 :] = 0.5
    return base.astype(np.float32)


def drifted(frame: np.ndarray, lightness: float = 0.0, chroma: float = 1.0, hue_degrees: float = 0.0, cast: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    """``frame`` with a known drift applied in Oklab, clipped back into sRGB as a model's frame would be."""
    lab = oklab.srgb_to_oklab(frame.reshape(-1, 3))
    turn = np.radians(hue_degrees)
    a, b = lab[:, 1].copy(), lab[:, 2].copy()
    lab[:, 0] += lightness
    lab[:, 1] = chroma * (np.cos(turn) * a - np.sin(turn) * b) + cast[0]
    lab[:, 2] = chroma * (np.sin(turn) * a + np.cos(turn) * b) + cast[1]
    return np.clip(oklab.oklab_to_srgb(lab), 0, 1).reshape(frame.shape).astype(np.float32)


def corrected(frame: np.ndarray, target: ColorStats, strength: float = 1.0) -> np.ndarray:
    stats = measure(frame)
    params = scaled(fit(stats, target_between(target, None, 0.0)), strength)
    return apply(frame, transform_for(stats, params))[0]


class FitTests(unittest.TestCase):
    def test_no_change_is_identity_and_moves_no_pixel(self) -> None:
        frame = scene()
        stats = measure(frame)
        params = fit(stats, target_between(stats, None, 0.0))
        self.assertTrue(params.identity)
        out, moved = apply(frame, transform_for(stats, params))
        self.assertIs(out, frame)
        self.assertEqual(moved, 0)

    def test_a_synthetic_drift_is_removed(self) -> None:
        reference = scene()
        target = measure(reference)
        frame = drifted(reference, lightness=0.02, chroma=1.08, hue_degrees=4)
        drift = compare(target, measure(corrected(frame, target)))
        self.assertLess(abs(drift.lightness), 0.3)
        self.assertAlmostEqual(drift.chroma, 1.0, delta=0.01)
        self.assertAlmostEqual(drift.contrast, 1.0, delta=0.02)
        assert drift.hue is not None
        self.assertLess(abs(drift.hue), 0.5)

    def test_caps_hold_and_are_reported(self) -> None:
        reference = scene()
        params = fit(measure(drifted(reference, lightness=0.1, chroma=1.3, hue_degrees=20)), target_between(measure(reference), None, 0.0))
        self.assertAlmostEqual(abs(params.lightness), LIGHTNESS_CAP)
        self.assertAlmostEqual(params.gain, 0.88)
        self.assertAlmostEqual(abs(float(np.degrees(params.turn))), 6.0)
        self.assertTrue({"lightness", "chroma", "hue"} <= params.bound)

    def test_strength_0_is_identity_and_a_saturated_srgb_color_comes_out_unchanged(self) -> None:
        reference = scene()
        frame = drifted(reference, lightness=0.03, chroma=1.1, hue_degrees=5)
        frame[0, 0] = (1.0, 0.0, 0.0)
        out = corrected(frame, measure(reference), strength=0.0)
        self.assertIs(out, frame)
        np.testing.assert_array_equal(out[0, 0], np.array([1.0, 0.0, 0.0], dtype=np.float32))

    def test_a_decoded_white_raised_by_the_half_level_passes_through(self) -> None:
        # Draw Things' 255, decoded and raised by the half level, reads 255.5: inside, not clamped onto the edge.
        frame = np.repeat(np.linspace(0.1, 0.9, 48, dtype=np.float32)[:, None, None], 48, axis=1).repeat(3, axis=2)
        frame[:4, :4] = HEADROOM
        stats = measure(frame)
        out, moved = apply(frame, transform_for(stats, Params(gain=1.01)))
        self.assertEqual(moved, 0)
        self.assertGreater(float(out[0, 0, 0]), 1.0)

    def test_a_letterboxed_frame_whose_10th_percentile_is_black_is_corrected(self) -> None:
        frame = scene()
        frame[:12] = 0.0
        stats = measure(frame)
        self.assertLess(stats.lightness[0], 0.01)
        transform = transform_for(stats, fit(stats, target_between(measure(drifted(frame, lightness=0.02)), None, 0.0)))
        self.assertEqual((transform.knots_in[0], transform.knots_in[-1]), (0.0, 1.0))
        out, _moved = apply(frame, transform)
        self.assertTrue(np.all(np.isfinite(out)))
        self.assertTrue(np.all(out[:12] < 1e-6))

    def test_the_gain_is_refined_for_the_chroma_the_tone_curve_and_the_edge_take(self) -> None:
        # Saturated shadows, darkened at constant a and b, leave sRGB, and the edge takes some of their chroma.
        frame = scene()
        frame[8:40, 4:44] = np.array([0.10, 0.0, 0.0], dtype=np.float32) + np.random.default_rng(2).random((32, 40, 3)).astype(np.float32) * 0.02
        stats = measure(frame)
        target = Target((stats.lightness[0] - 0.035, stats.median, stats.lightness[2]), stats.chroma, stats.hue, stats.cast)
        planned = [fit(stats, target)]
        plain = measure(apply(frame, transform_for(stats, planned[0]))[0]).chroma
        [refined] = refine_gains(planned, [stats], [sample_pixels(frame)])
        better = measure(apply(frame, transform_for(stats, refined))[0]).chroma
        # Measured: 0.0497 before, 0.0465 with the fitted gain alone, 0.0487 refined.
        self.assertLess(plain, stats.chroma * 0.95)
        self.assertLess(abs(better - stats.chroma), abs(plain - stats.chroma))
        self.assertGreater(refined.gain, planned[0].gain)


class ToneCurveTests(unittest.TestCase):
    def test_black_and_white_stay_and_the_curve_is_monotone(self) -> None:
        knots_in, knots_out = (0.0, 0.2, 0.5, 0.8, 1.0), (0.0, 0.15, 0.53, 0.88, 1.0)
        values = np.linspace(-0.05, 1.05, 1101)
        curve = tone_curve(values, knots_in, knots_out)
        self.assertEqual(float(tone_curve(np.array([0.0]), knots_in, knots_out)[0]), 0.0)
        self.assertEqual(float(tone_curve(np.array([1.0]), knots_in, knots_out)[0]), 1.0)
        self.assertTrue(np.all(np.diff(curve) > 0))
        np.testing.assert_allclose(tone_curve(np.array(knots_in[1:-1]), knots_in, knots_out), knots_out[1:-1])
        # Slope 1 beyond white, so a lifted white passes through.
        self.assertAlmostEqual(float(tone_curve(np.array([1.002]), knots_in, knots_out)[0]), 1.002)


class RampTests(unittest.TestCase):
    def test_the_pull_is_nothing_at_frame_0_and_all_of_it_at_the_last(self) -> None:
        self.assertEqual((ramp(0, 17), ramp(16, 17), ramp(0, 1)), (0.0, 1.0, 1.0))
        self.assertTrue(all(ramp(index, 17) <= ramp(index + 1, 17) for index in range(16)))

    def test_frame_0_goes_to_the_inputs_statistics_and_the_last_toward_the_anchor(self) -> None:
        reference = scene()
        input_stats, anchor_stats = measure(drifted(reference, lightness=-0.02)), measure(reference)
        frames = [drifted(reference, lightness=-0.02 - 0.002 * index) for index in range(9)]
        params = plan_run([measure(frame) for frame in frames], input_stats, anchor_stats, pull=1.0, strength=1.0)
        first = apply(frames[0], transform_for(measure(frames[0]), params[0]))[0]
        last = apply(frames[-1], transform_for(measure(frames[-1]), params[-1]))[0]
        self.assertLess(abs(compare(input_stats, measure(first)).lightness), 0.4)
        self.assertLess(abs(compare(anchor_stats, measure(last)).lightness), 0.4)


def run_chain(anchor: str, *, runs: int = 20, alternate: bool = False, reanchor: str = "prompt_pair", first_weight: float = 0.25, seed: int = 3) -> float:
    """A chain with no draw-things-cli: a "model" that adds a fixed drift over each clip (lightness -0.01, chroma x1.03,
    hue +1.5 degrees at its last frame), and a small random error each run that no statistics can see coming (it lands
    on the handoff after the correction). Returns how far the last handoff's statistics are from the first image's."""
    rng = np.random.default_rng(seed)
    first = scene()
    first_stats = measure(first)
    handoff = first
    anchor_stats: ColorStats | None = first_stats
    previous_pair: int | None = None
    frames_per_clip = 5
    for run in range(runs):
        pair = run % 2 if alternate else 0
        input_stats = measure(handoff)
        if anchor in ("first", "blend") and reanchor == "prompt_pair" and previous_pair is not None and pair != previous_pair:
            anchor_stats = input_stats
        previous_pair = pair
        clip = [drifted(handoff, lightness=-0.01 * share, chroma=1 + 0.03 * share, hue_degrees=1.5 * share) for share in np.linspace(0, 1, frames_per_clip)]
        if anchor != "none":
            pull = {"previous": 0.0, "first": 1.0, "blend": first_weight}[anchor]
            stats = [measure(frame) for frame in clip]
            params = plan_run(stats, input_stats, anchor_stats if anchor != "previous" else None, pull, 1.0)
            clip = [apply(frame, transform_for(frame_stats, frame_params))[0] for frame, frame_stats, frame_params in zip(clip, stats, params, strict=True)]
        error = rng.normal(0, 1, 3)
        handoff = drifted(clip[-1], lightness=0.003 * error[0], chroma=1 + 0.008 * error[1], hue_degrees=0.5 * error[2])
    return statistics_distance(measure(handoff), first_stats)


def statistics_distance(measured: ColorStats, reference: ColorStats) -> float:
    """ΔE_OK between two frames' characteristic colors: the lightness median, and the chroma median at the mean hue.
    Per pixel, the pinned black and white and the model's own clipping at the gamut's edge would dominate instead."""

    def color(stats: ColorStats) -> np.ndarray:
        hue = stats.hue or 0.0
        return np.array([stats.median, stats.chroma * np.cos(hue), stats.chroma * np.sin(hue)])

    return float(np.linalg.norm(color(measured) - color(reference)))


class SimulatedChainTests(unittest.TestCase):
    def test_each_anchor_holds_the_chain_as_it_should(self) -> None:
        # Averaged over four chains, since what is left is a random walk: measured 0.207, 0.045, 0.012, and 0.004.
        none, previous, blend, first = (float(np.mean([run_chain(anchor, seed=seed) for seed in (1, 2, 3, 4)])) for anchor in ("none", "previous", "blend", "first"))
        # Uncorrected, the drift grows as added: 20 runs of chroma x1.03 and hue +1.5 degrees.
        self.assertGreater(none, 0.05)
        # Each run's own drift is removed; what is left is the random walk of the errors no fit can see.
        self.assertLess(previous, none / 3)
        self.assertLess(blend, 0.02)
        self.assertLess(blend, previous)
        self.assertLessEqual(first, blend + 0.002)

    def test_alternating_pairs_reanchor_every_run_unless_told_never(self) -> None:
        self.assertAlmostEqual(run_chain("blend", alternate=True), run_chain("previous", alternate=True), places=6)
        self.assertAlmostEqual(run_chain("blend", alternate=True, reanchor="never"), run_chain("blend"), places=6)


if __name__ == "__main__":
    unittest.main()
