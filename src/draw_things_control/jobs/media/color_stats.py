"""Robust color statistics of a frame, or a region of one, in Oklab, and the comparison of two sets of them.

Pixels cannot be matched one to one across motion, so frames are compared by statistics (Milestone 09). The owner's
four drifts are the lightness median (brightness), the lightness spread from the 10th to the 90th percentile
(contrast), the chroma median (saturation), and the chroma-weighted circular mean hue. A cast is the mean ``a`` and
``b`` of the least chromatic tenth of the pixels, when that tenth is near-neutral. Chosen by rank rather than by a
fixed chroma, the same pixels stay neutral when saturation changes; a fixed limit let pixels cross it, and the cast
then picked up the frame's dominant hue, so correcting a pure change of saturation shifted every color.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from draw_things_control.jobs.media import oklab

# Pixels with less chroma than this are near-neutral: they give no hue, and the cast is measured only when the least
# chromatic tenth of the pixels (NEUTRAL_SHARE) is below it.
NEUTRAL_CHROMA = 0.03
NEUTRAL_SHARE = 10.0
# The colored pixels' hues must agree at least this much (the chroma-weighted mean resultant length) to have a mean
# hue; below it they cancel out, and no hue is measured.
HUE_AGREEMENT = 0.05
PERCENTILES = (10.0, 50.0, 90.0)


@dataclass(frozen=True)
class ColorStats:
    """One frame's or region's statistics in Oklab."""

    pixels: int
    # The 10th, 50th, and 90th percentiles of lightness.
    lightness: tuple[float, float, float]
    # The chroma median.
    chroma: float
    # The chroma-weighted circular mean hue of the colored pixels, in radians; None when they have none.
    hue: float | None
    # The mean a and b of the least chromatic tenth of the pixels; None when that tenth is not near-neutral.
    cast: tuple[float, float] | None

    @property
    def median(self) -> float:
        return self.lightness[1]

    @property
    def spread(self) -> float:
        return self.lightness[2] - self.lightness[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pixels": self.pixels,
            "lightness": [round(value, 4) for value in self.lightness],
            "chroma": round(self.chroma, 4),
            "hue_degrees": None if self.hue is None else round(float(np.degrees(self.hue)), 2),
            "cast": None if self.cast is None else [round(value, 4) for value in self.cast],
        }


def measure(frame: np.ndarray, mask: np.ndarray | None = None) -> ColorStats:
    """The statistics of an sRGB frame (H, W, 3, 1.0 for white), or of the pixels ``mask`` selects."""
    pixels = frame.reshape(-1, 3) if mask is None else frame[mask]
    return measure_lab(oklab.srgb_to_oklab(pixels))


def measure_lab(lab: np.ndarray) -> ColorStats:
    """The statistics of Oklab colors (N, 3)."""
    count = len(lab)
    if count == 0:
        return ColorStats(0, (0.0, 0.0, 0.0), 0.0, None, None)
    low, middle, high = np.percentile(lab[:, 0], PERCENTILES)
    chroma = oklab.chroma(lab)
    neutral = chroma < NEUTRAL_CHROMA
    colored = lab[~neutral]
    hue: float | None = None
    if len(colored):
        # Each pixel's unit hue vector weighted by its chroma is just (a, b).
        a, b = float(np.mean(colored[:, 1])), float(np.mean(colored[:, 2]))
        if np.hypot(a, b) >= HUE_AGREEMENT * float(np.mean(chroma[~neutral])):
            hue = float(np.arctan2(b, a))
    least = float(np.percentile(chroma, NEUTRAL_SHARE))
    cast = None
    if least < NEUTRAL_CHROMA:
        grayest = chroma <= least
        cast = (float(np.mean(lab[grayest, 1])), float(np.mean(lab[grayest, 2])))
    return ColorStats(count, (float(low), float(middle), float(high)), float(np.median(chroma)), hue, cast)


@dataclass(frozen=True)
class Drift:
    """How far one set of statistics is from a reference: lightness in hundredths of Oklab L, contrast and chroma as
    ratios, hue in degrees, and the change of the cast in a and b."""

    lightness: float
    contrast: float
    chroma: float
    hue: float | None
    cast: tuple[float, float] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lightness": round(self.lightness, 2),
            "contrast": round(self.contrast, 4),
            "chroma": round(self.chroma, 4),
            "hue_degrees": None if self.hue is None else round(self.hue, 2),
            "cast": None if self.cast is None else [round(value, 4) for value in self.cast],
        }

    def text(self) -> str:
        """For example ``L -2.1, contrast x1.06, chroma x1.08, hue +3°``."""
        parts = [f"L {self.lightness:+.1f}", f"contrast x{self.contrast:.2f}", f"chroma x{self.chroma:.2f}"]
        if self.hue is not None:
            # Rounded first, so a small negative turn reads +0°, not -0°.
            parts.append(f"hue {round(self.hue) + 0:+d}°")
        return ", ".join(parts)


def _ratio(value: float, reference: float) -> float:
    return value / reference if reference > 1e-6 else 1.0


def turn_degrees(angle: float) -> float:
    """An angle in radians as degrees from -180 to 180."""
    return float(np.degrees(np.angle(np.exp(1j * angle))))


def compare(reference: ColorStats, measured: ColorStats) -> Drift:
    """How ``measured`` differs from ``reference``."""
    hue = turn_degrees(measured.hue - reference.hue) if measured.hue is not None and reference.hue is not None else None
    cast = (measured.cast[0] - reference.cast[0], measured.cast[1] - reference.cast[1]) if measured.cast is not None and reference.cast is not None else None
    return Drift(
        lightness=(measured.median - reference.median) * 100,
        contrast=_ratio(measured.spread, reference.spread),
        chroma=_ratio(measured.chroma, reference.chroma),
        hue=hue,
        cast=cast,
    )
