"""A first image's color profile applied in floating point, and its colors outside sRGB brought in at constant hue.

A matrix-and-curves ICC profile (Display P3, Adobe RGB, ProPhoto, Rec. 2020: the ``rXYZ``, ``gXYZ``, ``bXYZ``
colorants and the ``rTRC``, ``gTRC``, ``bTRC`` curves, of type ``curv`` or ``para``) takes each source value to linear
light, to XYZ, adapted from the profile connection space's D50 to D65 by Bradford, to linear sRGB, unbounded.

Clipping a color outside sRGB channel by channel bends its hue (a Display P3 red turns orange) and merges neighbouring
shades. Instead its chroma is compressed toward the neutral axis at constant Oklab lightness and hue (owner decision,
Milestone 09): colors within ``KNEE`` of the sRGB edge's chroma, at their lightness and hue, do not move; from there a
smooth curve, slope 1 at the knee, takes the source profile's own edge onto the sRGB edge. Where the profile reaches
no further than sRGB, nothing moves. Both edges are tabulated over lightness and hue, and the tables pick the colors
near the edge, whose edges are then found exactly; a color still outside is brought onto the exact edge.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from draw_things_control.jobs.media import oklab

# Compression starts at this share of the sRGB edge's chroma (owner decision, 2026-09-30).
KNEE = 0.9
# The profile connection space's white (D50) and sRGB's (D65), as XYZ.
D50 = np.array([0.9642, 1.0, 0.8249])
D65 = np.array([0.95047, 1.0, 1.08883])
BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])
LINEAR_SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
XYZ_TO_LINEAR_SRGB = np.linalg.inv(LINEAR_SRGB_TO_XYZ)
# Pixels converted at a time, so a large photo needs a few float copies of a chunk, not of the whole image.
CHUNK = 1 << 20
# The tabulated edges are only a filter: bilinear interpolation cuts the sharp peaks of the edge at the primaries (by
# 12% near sRGB's blue), so every color above this share of the tabulated sRGB edge gets both edges exactly.
CANDIDATE = 0.8
# A source edge beyond sRGB's by less than this (Oklab chroma; a just-noticeable difference is about 0.02) is the same
# edge: a profile's colorants are stored in 16.16 fixed point, so an sRGB profile's edge is sRGB's give or take this.
SAME_EDGE = 1e-4


def adaptation(source: np.ndarray, destination: np.ndarray) -> np.ndarray:
    """The Bradford matrix that takes colors seen under ``source`` white to ``destination`` white, both XYZ."""
    scale = (BRADFORD @ destination) / (BRADFORD @ source)
    return np.linalg.inv(BRADFORD) @ np.diag(scale) @ BRADFORD


D50_TO_D65 = adaptation(D50, D65)


@dataclass(frozen=True)
class ToneCurve:
    """One channel's ICC curve: a ``curv`` table or gamma, or a ``para`` function (types 0 to 4)."""

    table: tuple[float, ...] = ()
    gamma: float | None = None
    function: int | None = None
    parameters: tuple[float, ...] = ()

    def __call__(self, values: np.ndarray) -> np.ndarray:
        x = np.clip(values, 0.0, 1.0)
        if self.function is not None:
            return _parametric(self.function, self.parameters, x)
        if self.gamma is not None:
            return x**self.gamma
        if len(self.table) == 0:
            return x
        table = np.asarray(self.table)
        return np.interp(x, np.linspace(0.0, 1.0, len(table)), table)


def _parametric(function: int, parameters: tuple[float, ...], x: np.ndarray) -> np.ndarray:
    g, a, b, c, d, e, f = (*parameters, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)[:7]
    if function == 0:
        return x**g
    if function == 1:
        return np.where(x >= -b / a, np.maximum(a * x + b, 0.0) ** g, 0.0)
    if function == 2:
        return np.where(x >= -b / a, np.maximum(a * x + b, 0.0) ** g + c, c)
    if function == 3:
        return np.where(x >= d, np.maximum(a * x + b, 0.0) ** g, c * x)
    return np.where(x >= d, np.maximum(a * x + b, 0.0) ** g + e, c * x + f)


# The number of parameters of each ``para`` function type.
PARAMETRIC_COUNTS = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}


@dataclass(frozen=True)
class MatrixProfile:
    """An RGB profile's colorants (the columns, XYZ relative to D50) and curves."""

    colorants: np.ndarray
    curves: tuple[ToneCurve, ToneCurve, ToneCurve]

    @property
    def to_linear_srgb(self) -> np.ndarray:
        """The matrix from the profile's linear values to linear sRGB."""
        return XYZ_TO_LINEAR_SRGB @ D50_TO_D65 @ self.colorants

    def linear(self, values: np.ndarray) -> np.ndarray:
        """Source values (..., 3), 0 to 1, to the profile's linear light."""
        return np.stack([curve(values[..., index]) for index, curve in enumerate(self.curves)], axis=-1)


def read_matrix_profile(data: bytes) -> MatrixProfile | None:
    """The profile's colorants and curves when it is an RGB matrix-and-curves profile; None for any other."""
    if len(data) < 132 or data[16:20] != b"RGB " or data[20:24] != b"XYZ ":
        return None
    try:
        (count,) = struct.unpack(">I", data[128:132])
        tags: dict[bytes, bytes] = {}
        for index in range(count):
            signature, offset, size = struct.unpack(">4sII", data[132 + 12 * index : 144 + 12 * index])
            tags[signature] = data[offset : offset + size]
        colorants = np.array([_xyz(tags[name]) for name in (b"rXYZ", b"gXYZ", b"bXYZ")]).T
        red, green, blue = (_curve(tags[name]) for name in (b"rTRC", b"gTRC", b"bTRC"))
    except (KeyError, struct.error, ValueError):
        return None
    if red is None or green is None or blue is None:
        return None
    return MatrixProfile(colorants, (red, green, blue))


def _s15(raw: bytes) -> float:
    return struct.unpack(">i", raw)[0] / 65536


def _xyz(tag: bytes) -> tuple[float, float, float]:
    if tag[:4] != b"XYZ ":
        raise ValueError("not an XYZ tag")
    return _s15(tag[8:12]), _s15(tag[12:16]), _s15(tag[16:20])


def _curve(tag: bytes) -> ToneCurve | None:
    kind = tag[:4]
    if kind == b"curv":
        (count,) = struct.unpack(">I", tag[8:12])
        if count == 0:
            return ToneCurve()
        if count == 1:
            return ToneCurve(gamma=struct.unpack(">H", tag[12:14])[0] / 256)
        values = struct.unpack(f">{count}H", tag[12 : 12 + 2 * count])
        return ToneCurve(table=tuple(value / 65535 for value in values))
    if kind == b"para":
        (function,) = struct.unpack(">H", tag[8:10])
        wanted = PARAMETRIC_COUNTS.get(function)
        if wanted is None:
            return None
        return ToneCurve(function=function, parameters=tuple(_s15(tag[12 + 4 * index : 16 + 4 * index]) for index in range(wanted)))
    return None


@dataclass(frozen=True)
class GamutReport:
    """What the gamut mapping did: pixels compressed beyond the knee, the largest chroma reduction (Oklab), and the
    pixels still outside sRGB that were brought onto its edge."""

    beyond_knee: int = 0
    largest_reduction: float = 0.0
    onto_edge: int = 0

    def plus(self, other: GamutReport) -> GamutReport:
        return GamutReport(self.beyond_knee + other.beyond_knee, max(self.largest_reduction, other.largest_reduction), self.onto_edge + other.onto_edge)


class GamutMapper:
    """Converts a matrix-and-curves profile's values to sRGB values, compressing what lies outside sRGB."""

    def __init__(self, profile: MatrixProfile) -> None:
        self._profile = profile
        self._to_srgb = profile.to_linear_srgb
        self._from_srgb = np.linalg.inv(self._to_srgb)
        self._srgb_edge = oklab.srgb_edge()

    def __call__(self, values: np.ndarray) -> tuple[np.ndarray, GamutReport]:
        """Source values (H, W, 3), 0 to 1, to sRGB values (float32), with what the mapping moved."""
        flat = values.reshape(-1, 3)
        result = np.empty(flat.shape, dtype=np.float32)
        report = GamutReport()
        for start in range(0, len(flat), CHUNK):
            converted, part = self._chunk(flat[start : start + CHUNK].astype(np.float64))
            result[start : start + CHUNK] = converted
            report = report.plus(part)
        return result.reshape(values.shape), report

    def _chunk(self, values: np.ndarray) -> tuple[np.ndarray, GamutReport]:
        linear = self._profile.linear(values) @ self._to_srgb.T
        lab = oklab.linear_srgb_to_oklab(linear)
        lightness, chroma, hue = lab[:, 0], oklab.chroma(lab), oklab.hue(lab)
        near = chroma > CANDIDATE * self._srgb_edge(lightness, hue)
        compressed = chroma.copy()
        if np.any(near):
            srgb_edge = oklab.edge_chroma(lightness[near], hue[near])
            source_edge = np.maximum(oklab.edge_chroma(lightness[near], hue[near], self._from_srgb), srgb_edge)
            compressed[near] = compress_chroma(chroma[near], srgb_edge, source_edge)
        moved = compressed < chroma - 1e-6
        report_moved = int(np.count_nonzero(moved))
        largest = float(np.max(chroma - compressed)) if report_moved else 0.0
        lab = oklab.from_polar(lightness, compressed, hue)
        lab, onto_edge = oklab.onto_srgb_edge(lab)
        srgb = oklab.linear_to_srgb(np.clip(oklab.oklab_to_linear_srgb(lab), 0.0, 1.0))
        return srgb, GamutReport(report_moved, largest, onto_edge)


def compress_chroma(chroma: np.ndarray, srgb_edge: np.ndarray, source_edge: np.ndarray, knee: float = KNEE) -> np.ndarray:
    """Chroma compressed toward the neutral axis: unchanged up to ``knee`` of the sRGB edge, then a curve with slope 1
    at the knee that takes ``source_edge`` onto ``srgb_edge``. Where the source reaches no further than sRGB, unchanged.

    Between the knee ``k`` and the source edge, with ``t`` the share of the way from ``k`` to the source edge and ``r``
    the ratio of that span to the sRGB edge's, the result is ``k + (srgb_edge - k) * r t / (1 + (r - 1) t)``: it
    starts with slope 1, never grows chroma, and keeps the most saturated colors in order.
    """
    k = knee * srgb_edge
    span_in = source_edge - k
    span_out = srgb_edge - k
    reaches_beyond = source_edge > srgb_edge + SAME_EDGE
    safe_in = np.where(reaches_beyond, span_in, 1.0)
    safe_out = np.where(reaches_beyond, span_out, 1.0)
    ratio = safe_in / np.maximum(safe_out, 1e-12)
    t = np.clip((chroma - k) / safe_in, 0.0, 1.0)
    curved = k + safe_out * ratio * t / (1 + (ratio - 1) * t)
    return np.where(reaches_beyond & (chroma > k), np.minimum(curved, chroma), chroma)
