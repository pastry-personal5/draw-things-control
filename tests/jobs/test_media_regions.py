"""Tests for a frame's regions (people, skin, background) with a fake segmenter, measured and corrected region by region."""

from __future__ import annotations

import unittest

import numpy as np

from draw_things_control.jobs.media import oklab
from draw_things_control.jobs.media.color_stats import measure
from draw_things_control.jobs.media.correction import SKIN_CAP, Params, Transform, apply_regions, fit_run, skin_residuals, transform_for
from draw_things_control.jobs.media.drift import drift_check, measure_regions, sample_frames
from draw_things_control.jobs.media.regions import Face, FrameMasks, RegionFinder, Segmentation, blend_weights, face_skin, find_regions, region_stats

WIDTH, HEIGHT = 96, 64
# The person: a column of the frame; their face at its top, their clothes below.
PERSON = (slice(4, 60), slice(40, 80))
CLOTHES = (slice(36, 60), slice(40, 80))


def oklab_noise(rng: np.random.Generator, shape: tuple[int, int], lightness: float, a: float, b: float, spread: float = 0.006) -> np.ndarray:
    """Colors around one, in cells of 4x4 pixels: texture a codec keeps, as it keeps a generated frame's, where noise
    of single pixels would lose chroma and contrast in ProRes alone."""
    cells = (-(-shape[0] // 4), -(-shape[1] // 4))
    count = cells[0] * cells[1]
    lab = np.stack([rng.normal(lightness, 0.04, count), rng.normal(a, spread, count), rng.normal(b, spread, count)], axis=1)
    srgb = np.clip(oklab.oklab_to_srgb(lab), 0, 1).reshape(*cells, 3)
    return np.repeat(np.repeat(srgb, 4, axis=0), 4, axis=1)[: shape[0], : shape[1]]


def person_frame(seed: int = 3) -> np.ndarray:
    """A bluish-gray background, a person of warm skin, and their clothes, a saturated blue."""
    rng = np.random.default_rng(seed)
    frame = oklab_noise(rng, (HEIGHT, WIDTH), 0.75, -0.01, -0.02)
    frame[PERSON] = oklab_noise(rng, (56, 40), 0.7, 0.035, 0.045)
    frame[CLOTHES] = oklab_noise(rng, (24, 40), 0.45, -0.02, -0.12)
    return frame.astype(np.float32)


def ellipse(center: tuple[float, float], radii: tuple[float, float], start: float, stop: float, count: int = 12) -> np.ndarray:
    angles = np.linspace(start, stop, count)
    return np.stack([center[0] + radii[0] * np.cos(angles), center[1] + radii[1] * np.sin(angles)], axis=1)


FACE = Face(
    # From one cheek over the chin to the other, open at the top, as Vision's faceContour is.
    contour=ellipse((60, 18), (12, 13), np.pi, 0),
    left_eye=ellipse((55, 14), (2.5, 1.2), 0, 2 * np.pi, 8),
    right_eye=ellipse((65, 14), (2.5, 1.2), 0, 2 * np.pi, 8),
    left_brow=np.array([[51.0, 10.0], [55.0, 9.0], [58.0, 10.0]]),
    right_brow=np.array([[62.0, 10.0], [65.0, 9.0], [69.0, 10.0]]),
    lips=ellipse((60, 25), (4, 1.5), 0, 2 * np.pi, 8),
)


def person_mask(scale: int = 2) -> np.ndarray:
    """The person's mask at half the frame's size, as Vision's comes at its own resolution."""
    mask = np.zeros((HEIGHT // scale, WIDTH // scale), dtype=np.uint8)
    mask[PERSON[0].start // scale : PERSON[0].stop // scale, PERSON[1].start // scale : PERSON[1].stop // scale] = 255
    return mask


class FakeSegmenter:
    """Finds the same person and face in every frame, and counts its calls."""

    def __init__(self, segmentation: Segmentation | None = None, *, fail_after: int | None = None) -> None:
        self.segmentation = segmentation or Segmentation(person_mask(), (FACE,))
        self.calls = 0
        self.fail_after = fail_after

    def __call__(self, frame: np.ndarray) -> Segmentation:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("Vision is gone")
        return self.segmentation


def drift_people(frame: np.ndarray, hue_degrees: float = 0.0, lightness: float = 0.0, chroma: float = 1.0) -> np.ndarray:
    """``frame`` with a drift in the person's pixels only."""
    out = frame.copy()
    lab = oklab.srgb_to_oklab(frame[PERSON].reshape(-1, 3))
    turn = np.radians(hue_degrees)
    a, b = lab[:, 1].copy(), lab[:, 2].copy()
    lab[:, 0] += lightness
    lab[:, 1] = chroma * (np.cos(turn) * a - np.sin(turn) * b)
    lab[:, 2] = chroma * (np.sin(turn) * a + np.cos(turn) * b)
    out[PERSON] = np.clip(oklab.oklab_to_srgb(lab), 0, 1).reshape(out[PERSON].shape)
    return out


class FaceSkinTests(unittest.TestCase):
    def test_the_face_is_the_hull_of_its_contour_and_brows_less_its_eyes_brows_and_lips(self) -> None:
        skin = face_skin(FACE, (WIDTH, HEIGHT))
        # A cheek and the middle of the forehead under the brows are skin; the open top is closed by the brows.
        self.assertTrue(skin[20, 52])
        self.assertTrue(skin[11, 60])
        # The eyes, a brow, and the lips are cut out, and nothing beyond the hull is in.
        self.assertFalse(skin[14, 55])
        self.assertFalse(skin[9, 55])
        self.assertFalse(skin[25, 60])
        self.assertFalse(skin[5, 60])
        self.assertFalse(skin[20, 30])


class FindRegionsTests(unittest.TestCase):
    def test_people_skin_and_background_are_found_and_measured(self) -> None:
        frame = person_frame()
        regions = find_regions(frame, FakeSegmenter()(frame))
        assert regions.people is not None and regions.skin is not None
        # The person's mask, stretched from half size, covers the person.
        self.assertAlmostEqual(float(regions.people.mean()), 56 * 40 / (WIDTH * HEIGHT), delta=0.02)
        # Skin is the person's skin-colored pixels, set by the face: their upper body, not their clothes or the background.
        self.assertGreater(float(regions.skin[PERSON[0].start : CLOTHES[0].start, 40:80].mean()), 0.8)
        self.assertLess(float(regions.skin[CLOTHES].mean()), 0.02)
        self.assertFalse(regions.skin[:, :36].any())
        stats = region_stats(frame, regions)
        self.assertEqual(sorted(stats), ["background", "frame", "people", "skin"])
        self.assertGreater(stats["skin"].mean[0], 0.02)
        self.assertLess(stats["background"].mean[1], 0)
        # The masks kept are at the segmenter's resolution.
        self.assertEqual(regions.masks.person.shape, (HEIGHT // 2, WIDTH // 2))
        self.assertEqual(regions.masks.skin.shape, (HEIGHT // 2, WIDTH // 2))

    def test_without_a_face_there_is_no_skin_and_a_tiny_person_is_no_region(self) -> None:
        frame = person_frame()
        faceless = find_regions(frame, Segmentation(person_mask()))
        self.assertIsNotNone(faceless.people)
        self.assertIsNone(faceless.skin)
        tiny = np.zeros((HEIGHT // 2, WIDTH // 2), dtype=np.uint8)
        tiny[0:2, 0:2] = 255
        regions = find_regions(frame, Segmentation(tiny, (FACE,)))
        self.assertIsNone(regions.people)
        self.assertEqual(list(region_stats(frame, regions)), ["frame"])

    def test_blend_weights_are_feathered_averaged_over_frames_and_skin_never_outweighs_people(self) -> None:
        frame = person_frame()
        masks = find_regions(frame, FakeSegmenter()(frame)).masks
        empty = FrameMasks(np.zeros_like(masks.person), np.zeros_like(masks.skin), 0)
        weights = blend_weights([masks, masks, empty], 1, (WIDTH, HEIGHT))
        assert weights is not None
        person, skin = weights
        self.assertEqual(person.shape, (HEIGHT, WIDTH))
        self.assertTrue(np.all(skin <= person + 1e-6))
        # Two of the three frames have the person, so the middle of them weighs about two thirds.
        self.assertAlmostEqual(float(person[30, 60]), 2 / 3, delta=0.02)
        # The edge is feathered: values between nothing and all of it across it.
        across = person[30, 34:46]
        self.assertTrue(np.any((across > 0.05) & (across < 0.6)))
        self.assertIsNone(blend_weights([None], 0, (WIDTH, HEIGHT)))

    def test_a_segmenter_that_fails_leaves_the_whole_frame_with_a_note(self) -> None:
        finder = RegionFinder(FakeSegmenter(fail_after=1))
        frame = person_frame()
        self.assertIsNotNone(finder(frame))
        self.assertIsNone(finder(frame))
        self.assertFalse(finder.active)
        self.assertIn("Apple Vision failed (Vision is gone)", finder.notes[0])
        self.assertIsNone(finder(frame))
        self.assertEqual(RegionFinder(None).notes, ())


class RegionDriftTests(unittest.TestCase):
    def test_a_drift_of_people_only_is_measured_on_people_and_skin_not_the_background(self) -> None:
        base = person_frame()
        frames = [drift_people(base, hue_degrees=6 * share) for share in np.linspace(0, 1, 9)]
        finder = RegionFinder(FakeSegmenter())
        samples = sample_frames(frames, finder)
        check = drift_check("run.mov", samples, measure_regions(base, finder), None)
        within = check.facts["region_comparisons"]["frame_0_to_last"]
        self.assertAlmostEqual(within["people"]["hue_degrees"], 6, delta=1)
        self.assertAlmostEqual(within["skin"]["hue_degrees"], 6, delta=1)
        self.assertAlmostEqual(within["background"]["hue_degrees"], 0, delta=0.5)
        self.assertEqual(check.facts["regions"], ["frame", "people", "skin", "background"])
        self.assertIn("skin hue +6°", check.summary)
        # Skin's hue beyond the limit warns, though the whole frame's did not drift as far.
        self.assertIn("skin hue +6°", check.warnings[0])
        self.assertIn("people", check.facts["region_stats"]["last"])

    def test_lightness_and_chroma_of_people_only_are_measured_back_on_people_and_skin(self) -> None:
        base = person_frame()
        finder = RegionFinder(FakeSegmenter())
        samples = sample_frames([base, drift_people(base, lightness=0.03, chroma=1.1)], finder)
        within = drift_check("run.mov", samples, None, None).facts["region_comparisons"]["frame_0_to_last"]
        for region in ("people", "skin"):
            self.assertAlmostEqual(within[region]["lightness"], 3, delta=0.5)
            self.assertAlmostEqual(within[region]["chroma"], 1.1, delta=0.02)
        self.assertAlmostEqual(within["background"]["lightness"], 0, delta=0.2)
        self.assertAlmostEqual(within["background"]["chroma"], 1.0, delta=0.01)

    def test_without_a_segmenter_only_the_whole_frame_is_measured(self) -> None:
        base = person_frame()
        check = drift_check("run.mov", sample_frames([base, base]), measure_regions(base), None)
        self.assertEqual(check.facts["regions"], ["frame"])
        self.assertEqual(check.facts["region_comparisons"], {})
        self.assertNotIn("skin", check.summary)


class RegionCorrectionTests(unittest.TestCase):
    def test_people_weights_blend_their_transform_and_leave_the_rest_alone(self) -> None:
        frame = person_frame()
        weights = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
        weights[PERSON] = 1.0
        identity = Transform((0.0, 1.0), (0.0, 1.0), 1.0, 0.0, (0.0, 0.0))
        warmer = Transform((0.0, 1.0), (0.0, 1.0), 1.0, 0.0, (0.01, 0.01))
        out, _moved = apply_regions(frame, identity, (warmer, weights))
        np.testing.assert_array_equal(out[:, :36], frame[:, :36])
        shifted = oklab.srgb_to_oklab(out[PERSON].reshape(-1, 3)) - oklab.srgb_to_oklab(frame[PERSON].reshape(-1, 3))
        self.assertAlmostEqual(float(np.median(shifted[:, 1])), 0.01, delta=0.001)
        # Skin weighs its own share of a pixel: half the skin weight, half the residual on top of people's.
        skin_weights = weights * 0.5
        out, _moved = apply_regions(frame, identity, (identity, weights), (warmer, skin_weights))
        shifted = oklab.srgb_to_oklab(out[PERSON].reshape(-1, 3)) - oklab.srgb_to_oklab(frame[PERSON].reshape(-1, 3))
        self.assertAlmostEqual(float(np.median(shifted[:, 1])), 0.005, delta=0.001)
        # The frame itself is never written to.
        np.testing.assert_array_equal(frame, person_frame())

    def test_a_frame_without_the_region_takes_its_parents_fit(self) -> None:
        parent = [Params(lightness=0.01), Params(lightness=0.02)]
        stats = measure(person_frame())
        fitted = fit_run([None, stats], stats, None, 0.0, parent)
        self.assertEqual(fitted[0], parent[0])
        self.assertTrue(fitted[1].identity)
        with self.assertRaises(ValueError):
            fit_run([None], stats, None, 0.0)

    def test_the_skin_residual_moves_skin_to_its_target_within_its_cap(self) -> None:
        frame = person_frame()
        regions = find_regions(frame, FakeSegmenter()(frame))
        assert regions.skin is not None
        skin = frame[regions.skin]
        target = measure(skin)
        warm = drift_people(frame, lightness=0.01)[regions.skin]
        identity = Params()
        residuals, after = skin_residuals([warm, None], [measure(warm), target], [identity, identity], target, None, 0.0)
        self.assertAlmostEqual(residuals[0].lightness, -0.01, delta=0.002)
        self.assertTrue(residuals[1].identity)
        self.assertIsNone(after[1])
        # Far off, the residual stops at its cap and says so.
        far = drift_people(frame, lightness=0.08)[regions.skin]
        residuals, _after = skin_residuals([far], [measure(far)], [identity], target, None, 0.0)
        self.assertAlmostEqual(residuals[0].lightness, -SKIN_CAP)
        self.assertIn("skin", residuals[0].bound)
        corrected = transform_for(measure(far), residuals[0])
        self.assertLess(corrected.knots_out[2], corrected.knots_in[2])


if __name__ == "__main__":
    unittest.main()
