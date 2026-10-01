"""Tests for the sRGB curve every color measurement shares."""

from __future__ import annotations

import unittest

import numpy as np

from draw_things_control.jobs.media import oklab


class SrgbCurveTests(unittest.TestCase):
    def test_the_curve_is_computed_in_the_dtype_asked_for(self) -> None:
        values = np.linspace(0, 1, 11, dtype=np.float32)
        self.assertEqual(oklab.srgb_to_linear(values).dtype, np.float64)
        self.assertEqual(oklab.srgb_to_linear(values, np.float32).dtype, np.float32)

    def test_image_values_and_negative_ones_follow_the_same_curve(self) -> None:
        values = np.array([0.0, 0.02, 0.04045, 0.2, 0.5, 1.0, 1.2])
        linear = oklab.srgb_to_linear(values)
        self.assertAlmostEqual(float(linear[3]), ((0.2 + 0.055) / 1.055) ** 2.4)
        self.assertEqual(float(linear[1]), 0.02 / 12.92)
        # Extended to negative values by symmetry, so one negative value takes the other branch for the whole array.
        np.testing.assert_array_equal(oklab.srgb_to_linear(np.append(values, -0.5))[:-1], linear)
        self.assertEqual(float(oklab.srgb_to_linear(np.array(-0.5))), -float(oklab.srgb_to_linear(np.array(0.5))))


if __name__ == "__main__":
    unittest.main()
