"""Tests for reading matrix-and-curves ICC profiles and mapping a first image's gamut into sRGB."""

from __future__ import annotations

import struct
import unittest

import numpy as np

from draw_things_control.jobs.inputs.gamut import D50, D65, KNEE, GamutMapper, adaptation, compress_chroma, read_matrix_profile
from draw_things_control.jobs.media import oklab

SRGB_PRIMARIES = ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))
P3_PRIMARIES = ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060))
# The sRGB curve as ICC parametric function type 3.
SRGB_PARA = (3, (2.4, 1 / 1.055, 0.055 / 1.055, 1 / 12.92, 0.04045))


def primaries_matrix(primaries: tuple[tuple[float, float], ...]) -> np.ndarray:
    """Linear RGB to XYZ for primaries given as xy chromaticities, with gamut.py's D65 white."""
    columns = np.array([[x / y, 1.0, (1 - x - y) / y] for x, y in primaries]).T
    return columns * np.linalg.solve(columns, D65)


def _s15(value: float) -> bytes:
    return struct.pack(">i", round(value * 65536))


def _tag_xyz(values: np.ndarray) -> bytes:
    return b"XYZ " + bytes(4) + b"".join(_s15(value) for value in values)


def _tag_desc(text: str) -> bytes:
    ascii_text = text.encode("ascii") + b"\0"
    return b"desc" + bytes(4) + struct.pack(">I", len(ascii_text)) + ascii_text + bytes(4) + bytes(4) + bytes(2) + bytes(1) + bytes(67)


def icc_profile(primaries: tuple[tuple[float, float], ...], *, curve: bytes | None = None, name: str = "Test RGB") -> bytes:
    """A version 2 RGB matrix-and-curves display profile, its colorants adapted to D50 by Bradford as ICC requires.
    ``curve`` is one TRC tag used for all three channels; the sRGB parametric curve by default."""
    colorants = adaptation(D65, D50) @ primaries_matrix(primaries)
    function, parameters = SRGB_PARA
    trc = curve if curve is not None else b"para" + bytes(4) + struct.pack(">H", function) + bytes(2) + b"".join(_s15(value) for value in parameters)
    tags = [(b"desc", _tag_desc(name)), (b"wtpt", _tag_xyz(D50)), (b"rXYZ", _tag_xyz(colorants[:, 0])), (b"gXYZ", _tag_xyz(colorants[:, 1])), (b"bXYZ", _tag_xyz(colorants[:, 2])), (b"rTRC", trc), (b"gTRC", trc), (b"bTRC", trc)]
    table_size = 4 + 12 * len(tags)
    offset = 128 + table_size
    entries, data = b"", b""
    for signature, body in tags:
        padded = body + bytes(-len(body) % 4)
        entries += struct.pack(">4sII", signature, offset + len(data), len(body))
        data += padded
    size = 128 + table_size + len(data)
    header = struct.pack(">I4sI4s4s4s", size, b"none", 0x02100000, b"mntr", b"RGB ", b"XYZ ") + bytes(12) + b"acsp" + bytes(24) + struct.pack(">I", 0) + b"".join(_s15(value) for value in D50) + bytes(48)
    assert len(header) == 128
    return header + struct.pack(">I", len(tags)) + entries + data


def gamma_curve(gamma: float) -> bytes:
    return b"curv" + bytes(4) + struct.pack(">IH", 1, round(gamma * 256)) + bytes(2)


def exact_oklab(values: np.ndarray, primaries: tuple[tuple[float, float], ...]) -> np.ndarray:
    """What the colors mean, in Oklab, before any gamut mapping."""
    to_srgb = np.linalg.inv(primaries_matrix(SRGB_PRIMARIES)) @ primaries_matrix(primaries)
    return oklab.linear_srgb_to_oklab(oklab.srgb_to_linear(values) @ to_srgb.T)


class ProfileReadingTests(unittest.TestCase):
    def test_parametric_and_table_curves_are_read(self) -> None:
        parametric = read_matrix_profile(icc_profile(P3_PRIMARIES))
        gamma = read_matrix_profile(icc_profile(P3_PRIMARIES, curve=gamma_curve(2.2)))
        assert parametric is not None and gamma is not None
        self.assertEqual(parametric.curves[0].function, 3)
        self.assertAlmostEqual(float(parametric.curves[0](np.array(0.5))), float(oklab.srgb_to_linear(np.array(0.5))), places=4)
        self.assertAlmostEqual(gamma.curves[0].gamma or 0, 563 / 256)
        self.assertAlmostEqual(float(gamma.curves[0](np.array(0.5))), 0.5 ** (563 / 256), places=6)
        table = b"curv" + bytes(4) + struct.pack(">I3H", 3, 0, 16384, 65535) + bytes(2)
        tabled = read_matrix_profile(icc_profile(P3_PRIMARIES, curve=table))
        assert tabled is not None
        self.assertAlmostEqual(float(tabled.curves[0](np.array(0.5))), 16384 / 65535, places=6)

    def test_the_colorants_give_the_known_p3_to_srgb_matrix(self) -> None:
        profile = read_matrix_profile(icc_profile(P3_PRIMARIES))
        assert profile is not None
        expected = np.linalg.inv(primaries_matrix(SRGB_PRIMARIES)) @ primaries_matrix(P3_PRIMARIES)
        np.testing.assert_allclose(profile.to_linear_srgb, expected, atol=2e-4)

    def test_other_profiles_are_not_matrix_profiles(self) -> None:
        self.assertIsNone(read_matrix_profile(b"not a profile"))
        gray = bytearray(icc_profile(P3_PRIMARIES))
        gray[16:20] = b"GRAY"
        self.assertIsNone(read_matrix_profile(bytes(gray)))


class GamutMappingTests(unittest.TestCase):
    def mapper(self, primaries: tuple[tuple[float, float], ...]) -> GamutMapper:
        profile = read_matrix_profile(icc_profile(primaries))
        assert profile is not None
        return GamutMapper(profile)

    def test_a_p3_red_keeps_its_hue_and_lightness(self) -> None:
        red = np.array([[[1.0, 0.0, 0.0]]])
        mapped, report = self.mapper(P3_PRIMARIES)(red)
        before, after = exact_oklab(red.reshape(-1, 3), P3_PRIMARIES)[0], oklab.srgb_to_oklab(mapped.reshape(-1, 3).astype(np.float64))[0]
        self.assertLess(abs(after[0] - before[0]), 0.01)
        turn = np.degrees(np.angle(np.exp(1j * (oklab.hue(after) - oklab.hue(before)))))
        self.assertLess(abs(float(turn)), 2)
        self.assertEqual(report.beyond_knee, 1)
        self.assertTrue(np.all((mapped >= 0) & (mapped <= 1)))

    def test_colors_within_the_knee_move_less_than_half_a_level_and_none_gains_chroma(self) -> None:
        values = np.random.default_rng(3).random((64, 64, 3))
        mapped, _report = self.mapper(P3_PRIMARIES)(values)
        flat = values.reshape(-1, 3)
        before = exact_oklab(flat, P3_PRIMARIES)
        after = oklab.srgb_to_oklab(mapped.reshape(-1, 3).astype(np.float64))
        edge = oklab.edge_chroma(before[:, 0], oklab.hue(before))
        within = oklab.chroma(before) <= KNEE * edge
        self.assertGreater(int(np.count_nonzero(within)), 1000)
        exact = oklab.oklab_to_srgb(before[within]) * 255
        self.assertLess(float(np.max(np.abs(mapped.reshape(-1, 3)[within] * 255 - exact))), 0.5)
        # Within float32's rounding near white and the profile's 16.16 fixed-point colorants.
        self.assertTrue(np.all(oklab.chroma(after) <= oklab.chroma(before) + 1e-4))

    def test_a_profile_no_larger_than_srgb_moves_nothing(self) -> None:
        values = np.random.default_rng(4).random((48, 48, 3))
        mapped, report = self.mapper(SRGB_PRIMARIES)(values)
        self.assertLess(float(np.max(np.abs(mapped - values))) * 255, 0.5)
        self.assertEqual(report.beyond_knee, 0)

    def test_the_curve_starts_at_the_knee_and_ends_on_the_srgb_edge(self) -> None:
        srgb_edge, source_edge = np.array(0.2), np.array(0.3)
        knee = KNEE * srgb_edge
        self.assertAlmostEqual(float(compress_chroma(knee, srgb_edge, source_edge)), float(knee))
        self.assertAlmostEqual(float(compress_chroma(source_edge, srgb_edge, source_edge)), 0.2)
        step = 1e-6
        slope = (compress_chroma(knee + step, srgb_edge, source_edge) - knee) / step
        self.assertAlmostEqual(float(slope), 1.0, places=3)
        samples = np.linspace(0, 0.3, 200)
        compressed = compress_chroma(samples, np.full_like(samples, 0.2), np.full_like(samples, 0.3))
        self.assertTrue(np.all(np.diff(compressed) > 0))
        # A source that reaches no further than sRGB is left alone.
        self.assertEqual(float(compress_chroma(np.array(0.19), srgb_edge, np.array(0.2))), 0.19)


if __name__ == "__main__":
    unittest.main()
