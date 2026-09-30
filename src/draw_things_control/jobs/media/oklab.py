"""Oklab (Ottosson, 2020, https://bottosson.github.io/posts/oklab/): the space colors are measured and corrected in.

Lightness ``L`` (0 black, 1 white) and the opponent axes ``a`` and ``b``; chroma is ``sqrt(a² + b²)`` and hue
``atan2(b, a)``. It keeps hue nearly constant when chroma changes, and its Euclidean distance works as a color
difference (``ΔE_OK``, about 0.02 for a just-noticeable one). sRGB values here are floats with 1.0 for white, and are
not clipped: a color outside sRGB has a channel below 0 or above 1, and the sRGB curve is extended to negative values
by symmetry, so a conversion round trips.
"""

from __future__ import annotations

import numpy as np

# Linear sRGB to cone responses, and the cube-rooted responses to Lab (Ottosson's matrices for linear sRGB).
LINEAR_SRGB_TO_LMS = np.array([[0.4122214708, 0.5363325363, 0.0514459929], [0.2119034982, 0.6806995451, 0.1073969566], [0.0883024619, 0.2817188376, 0.6299787005]])
LMS_TO_OKLAB = np.array([[0.2104542553, 0.7936177850, -0.0040720468], [1.9779984951, -2.4285922050, 0.4505937099], [0.0259040371, 0.7827717662, -0.8086757660]])
OKLAB_TO_LMS = np.linalg.inv(LMS_TO_OKLAB)
LMS_TO_LINEAR_SRGB = np.linalg.inv(LINEAR_SRGB_TO_LMS)
# How close to the sRGB cube a color must be to count as inside it.
INSIDE = 1e-6
# 2^-26 of the unit range: far finer than a just-noticeable difference (0.02).
BISECTION_STEPS = 26


def srgb_to_linear(values: np.ndarray) -> np.ndarray:
    """The sRGB curve undone, extended to negative values by symmetry."""
    values = np.asarray(values, dtype=np.float64)
    magnitude = np.abs(values)
    return np.sign(values) * np.where(magnitude <= 0.04045, magnitude / 12.92, ((magnitude + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """The sRGB curve, extended to negative values by symmetry."""
    values = np.asarray(values, dtype=np.float64)
    magnitude = np.abs(values)
    return np.sign(values) * np.where(magnitude <= 0.0031308, magnitude * 12.92, 1.055 * magnitude ** (1 / 2.4) - 0.055)


def linear_srgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """Linear sRGB (..., 3) to Oklab (..., 3)."""
    lms = np.asarray(rgb, dtype=np.float64) @ LINEAR_SRGB_TO_LMS.T
    return np.cbrt(lms) @ LMS_TO_OKLAB.T


def oklab_to_linear_srgb(lab: np.ndarray) -> np.ndarray:
    """Oklab (..., 3) to linear sRGB (..., 3), unbounded."""
    lms = (np.asarray(lab, dtype=np.float64) @ OKLAB_TO_LMS.T) ** 3
    return lms @ LMS_TO_LINEAR_SRGB.T


def srgb_to_oklab(srgb: np.ndarray) -> np.ndarray:
    return linear_srgb_to_oklab(srgb_to_linear(srgb))


def oklab_to_srgb(lab: np.ndarray) -> np.ndarray:
    return linear_to_srgb(oklab_to_linear_srgb(lab))


def chroma(lab: np.ndarray) -> np.ndarray:
    return np.hypot(lab[..., 1], lab[..., 2])


def hue(lab: np.ndarray) -> np.ndarray:
    """Hue in radians, from -pi to pi."""
    return np.arctan2(lab[..., 2], lab[..., 1])


def delta_e(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """ΔE_OK, the Euclidean distance in Oklab."""
    return np.linalg.norm(np.asarray(first, dtype=np.float64) - np.asarray(second, dtype=np.float64), axis=-1)


def from_polar(lightness: np.ndarray, chroma_values: np.ndarray, hue_values: np.ndarray) -> np.ndarray:
    return np.stack((lightness, chroma_values * np.cos(hue_values), chroma_values * np.sin(hue_values)), axis=-1)


def inside_cube(rgb: np.ndarray) -> np.ndarray:
    """Whether each linear RGB color (..., 3) lies within [0, 1] in every channel."""
    return np.all((rgb >= -INSIDE) & (rgb <= 1 + INSIDE), axis=-1)


def edge_chroma(lightness: np.ndarray, hue_values: np.ndarray, from_srgb: np.ndarray | None = None) -> np.ndarray:
    """The largest chroma at each lightness and hue that stays inside a gamut, found by bisection.

    The gamut is the RGB cube reached from linear sRGB by the 3x3 ``from_srgb`` (None: sRGB itself). Lightness at or
    beyond black or white has no chroma inside any gamut. At a fixed lightness and hue, each cube-rooted cone response
    is ``L + C k`` (``k`` from the hue), so each RGB channel is a cubic in ``C``; the bisection evaluates those cubics.
    """
    lightness = np.asarray(lightness, dtype=np.float64)
    hue_values = np.broadcast_to(np.asarray(hue_values, dtype=np.float64), lightness.shape)
    to_rgb = LMS_TO_LINEAR_SRGB if from_srgb is None else from_srgb @ LMS_TO_LINEAR_SRGB
    # k_j for each cone: its response per unit of chroma along the hue.
    k = np.cos(hue_values)[..., None] * OKLAB_TO_LMS[:, 1] + np.sin(hue_values)[..., None] * OKLAB_TO_LMS[:, 2]
    lightness_column = lightness[..., None]
    # Each channel i is sum_j M_ij (L + C k_j)^3 = c0 + c1 C + c2 C^2 + c3 C^3.
    c0 = (lightness_column**3) @ np.ones((1, 3)) * to_rgb.sum(axis=1)
    c1 = 3 * lightness_column**2 * (k @ to_rgb.T)
    c2 = 3 * lightness_column * ((k**2) @ to_rgb.T)
    c3 = (k**3) @ to_rgb.T
    low = np.zeros(lightness.shape)
    high = np.ones(lightness.shape)
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2
        m = middle[..., None]
        rgb = ((c3 * m + c2) * m + c1) * m + c0
        inside = inside_cube(rgb)
        low = np.where(inside, middle, low)
        high = np.where(inside, high, middle)
    return np.where((lightness <= 0) | (lightness >= 1), 0.0, low)


class EdgeTable:
    """A gamut's edge chroma tabulated over lightness and hue, read by bilinear interpolation."""

    LIGHTNESS_STEPS = 129
    HUE_STEPS = 360

    def __init__(self, from_srgb: np.ndarray | None = None) -> None:
        self.lightness = np.linspace(0.0, 1.0, self.LIGHTNESS_STEPS)
        self.hues = np.linspace(-np.pi, np.pi, self.HUE_STEPS, endpoint=False)
        grid_lightness, grid_hues = np.meshgrid(self.lightness, self.hues, indexing="ij")
        self.table = edge_chroma(grid_lightness, grid_hues, from_srgb)

    def __call__(self, lightness: np.ndarray, hue_values: np.ndarray) -> np.ndarray:
        position = np.clip(np.asarray(lightness, dtype=np.float64), 0.0, 1.0) * (self.LIGHTNESS_STEPS - 1)
        row = np.minimum(np.floor(position).astype(np.int64), self.LIGHTNESS_STEPS - 2)
        row_weight = position - row
        turn = (np.asarray(hue_values, dtype=np.float64) + np.pi) / (2 * np.pi) * self.HUE_STEPS
        column = np.floor(turn).astype(np.int64) % self.HUE_STEPS
        column_weight = turn - np.floor(turn)
        following = (column + 1) % self.HUE_STEPS
        table = self.table
        lower = table[row, column] * (1 - column_weight) + table[row, following] * column_weight
        upper = table[row + 1, column] * (1 - column_weight) + table[row + 1, following] * column_weight
        return lower * (1 - row_weight) + upper * row_weight


_SRGB_EDGE: EdgeTable | None = None


def srgb_edge() -> EdgeTable:
    """The sRGB gamut's edge table, built once."""
    global _SRGB_EDGE
    if _SRGB_EDGE is None:
        _SRGB_EDGE = EdgeTable()
    return _SRGB_EDGE


def onto_srgb_edge(lab: np.ndarray) -> tuple[np.ndarray, int]:
    """Bring each color outside sRGB onto its edge at constant lightness and hue; colors inside pass unchanged.

    Lightness beyond white or black becomes white or black. Returns the colors and how many were moved.
    """
    lab = np.array(lab, dtype=np.float64)
    outside = ~inside_cube(oklab_to_linear_srgb(lab))
    count = int(np.count_nonzero(outside))
    if count:
        moved = lab[outside]
        lightness = np.clip(moved[..., 0], 0.0, 1.0)
        hue_values = hue(moved)
        limit = edge_chroma(lightness, hue_values)
        lab[outside] = from_polar(lightness, np.minimum(chroma(moved), limit), hue_values)
    return lab, count
