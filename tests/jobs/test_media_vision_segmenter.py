"""Tests for the Apple Vision segmenter: its landmark coordinates, its PNG, and one real run where Vision is installed."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image

from draw_things_control.jobs.media.vision_segmenter import VisionSegmenter, png_bytes, vision_segmenter

HAS_VISION = sys.platform == "darwin" and importlib.util.find_spec("Vision") is not None


class FakeRegion:
    def __init__(self, points: list[tuple[float, float]]) -> None:
        self._points = [SimpleNamespace(x=x, y=y) for x, y in points]

    def pointCount(self) -> int:  # noqa: N802 - Vision's name
        return len(self._points)

    def pointsInImageOfSize_(self, _size: object) -> list[SimpleNamespace]:  # noqa: N802 - Vision's name
        return self._points


class FakeLandmarks:
    def faceContour(self) -> FakeRegion:  # noqa: N802 - Vision's name
        return FakeRegion([(10.0, 50.0), (20.0, 40.0)])

    def leftEye(self) -> None:  # noqa: N802 - Vision's name
        return None

    rightEye = leftEyebrow = rightEyebrow = outerLips = leftEye


class LandmarkTests(unittest.TestCase):
    def test_points_are_turned_from_a_lower_left_origin_to_the_frames_rows(self) -> None:
        segmenter = object.__new__(VisionSegmenter)
        segmenter._quartz = SimpleNamespace(CGSizeMake=lambda width, height: (width, height))
        face = segmenter._face(SimpleNamespace(landmarks=FakeLandmarks), 96, 64)
        assert face is not None
        np.testing.assert_array_equal(face.contour, [[10.0, 14.0], [20.0, 24.0]])
        self.assertEqual(face.left_eye.shape, (0, 2))
        self.assertIsNone(segmenter._face(SimpleNamespace(landmarks=lambda: None), 96, 64))

    def test_the_png_holds_the_frame_rounded_to_8_bits(self) -> None:
        frame = np.array([[[0.0, 0.5, 1.0], [1.002, -0.01, 0.2]]], dtype=np.float32)
        decoded = np.asarray(Image.open(io.BytesIO(png_bytes(frame))))
        np.testing.assert_array_equal(decoded, [[[0, 128, 255], [255, 0, 51]]])


@unittest.skipUnless(HAS_VISION, "needs macOS and pyobjc-framework-Vision")
class VisionTests(unittest.TestCase):
    def test_a_frame_with_no_one_gives_an_8_bit_mask_and_no_face(self) -> None:
        segmenter = vision_segmenter()
        assert segmenter is not None
        gradient = np.linspace(0, 1, 96, dtype=np.float32)
        frame = np.repeat(np.repeat(gradient[None, :, None], 64, axis=0), 3, axis=2)
        segmentation = segmenter(frame)
        self.assertEqual(segmentation.person.dtype, np.uint8)
        self.assertEqual(segmentation.person.ndim, 2)
        self.assertLess(float(segmentation.person.mean()), 128)
        self.assertEqual(segmentation.faces, ())


class LazyImportTests(unittest.TestCase):
    def test_starting_the_cli_loads_neither_vision_nor_pyobjc(self) -> None:
        code = "import sys, draw_things_control.cli.app; print(sorted(name for name in ('Vision', 'objc', 'Quartz') if name in sys.modules))"
        loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(loaded, "[]")


if __name__ == "__main__":
    unittest.main()
