"""The color correction of one run (Milestone 09): the fit, the anchor's pull and its ramp, the caps, the smoothing over
frames, and applying the result to a frame. It knows nothing of files: statistics in, transforms out, frames in and out.

Each frame ``t`` of a run is fitted once, from its own statistics to a target between the run's input and the anchor:
``lerp(input, anchor, pull * smoothstep(t / (n - 1)))``. So every frame is brought back to the run's input in full,
which removes the drift the model added in the run, and only the anchor's pull is ramped in, from nothing at frame 0 (the
model's copy of the input, so clips join without a jump) to all of it at the last frame. To first order that is the fit
to the input followed by the ramped pull, with one transform to cap and scale.

The transform, in Oklab: lightness through a monotone curve (black and white stay; the 10th, 50th, and 90th percentiles
go to the target's median and spread), and ``(a, b)`` to ``gain * R(turn) * (a, b) + shift``: the ratio of chroma medians,
the difference of mean hues, and what is left of the neutral cast. Colors the transform leaves inside sRGB pass through
unchanged; one it pushes outside is brought onto the edge at constant lightness and hue.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from draw_things_control.jobs.media import oklab
from draw_things_control.jobs.media.color_stats import ColorStats, measure, turn_degrees

# Caps per run (proposed, Milestone 09; the owner's A/B chains set them). A cap that binds is reported, never exceeded:
# a large difference is more likely a change of scene than drift.
LIGHTNESS_CAP = 0.04
SPREAD_CAP = (0.92, 1.08)
CHROMA_CAP = (0.88, 1.12)
HUE_CAP_DEGREES = 6.0
CAST_CAP = 0.015
# Tone knots closer than this to each other, or to black or white, are dropped: a letterboxed frame's 10th percentile is
# black itself, and a lifted white's 90th can reach past 1.
KNOT_GAP = 0.01
# The parameters are smoothed over frames: a running median of 5, then a Gaussian of 2 frames.
MEDIAN_FRAMES = 5
GAUSSIAN_SIGMA = 2.0
# Every SAMPLE_STRIDE-th pixel of each row and column, kept from the first pass, to refine the chroma gain.
SAMPLE_STRIDE = 4
# A decoded frame is raised by the half level Draw Things truncated, so white reads up to 255.5 levels: inside the
# gamut for the check before the correction returns to sRGB, since the handoff clips at 255 anyway.
HEADROOM = 255.5 / 255


@dataclass(frozen=True)
class Target:
    """The statistics a frame is moved to."""

    lightness: tuple[float, float, float]
    chroma: float
    hue: float | None
    cast: tuple[float, float] | None


def smoothstep(share: float) -> float:
    share = min(max(share, 0.0), 1.0)
    return share * share * (3 - 2 * share)


def ramp(index: int, frames: int) -> float:
    """The share of the anchor's pull at frame ``index`` of ``frames``: nothing at frame 0, all of it at the last."""
    return 1.0 if frames <= 1 else smoothstep(index / (frames - 1))


def target_between(start: ColorStats, end: ColorStats | None, share: float) -> Target:
    """``share`` of the way from ``start``'s statistics to ``end``'s; hue the short way round."""
    if end is None or share <= 0:
        return Target(start.lightness, start.chroma, start.hue, start.cast)
    lightness = tuple(a + (b - a) * share for a, b in zip(start.lightness, end.lightness, strict=True))
    chroma = start.chroma + (end.chroma - start.chroma) * share
    if start.hue is None or end.hue is None:
        hue = start.hue if start.hue is not None else end.hue
    else:
        hue = start.hue + np.radians(turn_degrees(end.hue - start.hue)) * share
    if start.cast is None or end.cast is None:
        cast = start.cast if start.cast is not None else end.cast
    else:
        cast = (start.cast[0] + (end.cast[0] - start.cast[0]) * share, start.cast[1] + (end.cast[1] - start.cast[1]) * share)
    return Target((lightness[0], lightness[1], lightness[2]), chroma, None if hue is None else float(hue), cast)


@dataclass(frozen=True)
class Params:
    """One frame's correction, before it is turned into a transform: the lightness median's shift, the spread's ratio,
    the change of the share of the spread below the median, the chroma gain, the hue turn (radians), and the shift of
    (a, b)."""

    lightness: float = 0.0
    spread: float = 1.0
    balance: float = 0.0
    gain: float = 1.0
    turn: float = 0.0
    shift: tuple[float, float] = (0.0, 0.0)
    # The caps that bound this frame's fit.
    bound: frozenset[str] = field(default=frozenset(), compare=False)

    @property
    def identity(self) -> bool:
        return self.lightness == 0 and self.spread == 1 and self.balance == 0 and self.gain == 1 and self.turn == 0 and self.shift == (0.0, 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {"lightness": round(self.lightness * 100, 2), "contrast": round(self.spread, 4), "chroma": round(self.gain, 4), "hue_degrees": round(float(np.degrees(self.turn)), 2), "cast": [round(self.shift[0], 4), round(self.shift[1], 4)]}


def _capped(value: float, low: float, high: float, name: str, bound: set[str]) -> float:
    if value < low or value > high:
        bound.add(name)
        return min(max(value, low), high)
    return value


def fit(stats: ColorStats, target: Target) -> Params:
    """The parameters that take ``stats`` to ``target``, each within its cap."""
    bound: set[str] = set()
    lightness = _capped(target.lightness[1] - stats.median, -LIGHTNESS_CAP, LIGHTNESS_CAP, "lightness", bound)
    target_spread = target.lightness[2] - target.lightness[0]
    spread = _capped(target_spread / stats.spread, *SPREAD_CAP, "contrast", bound) if stats.spread > 1e-6 and target_spread > 1e-6 else 1.0
    balance = _lower_share(target.lightness) - _lower_share(stats.lightness) if stats.spread > 1e-6 and target_spread > 1e-6 else 0.0
    gain = _capped(target.chroma / stats.chroma, *CHROMA_CAP, "chroma", bound) if stats.chroma > 1e-6 and target.chroma > 1e-6 else 1.0
    turn = 0.0
    if stats.hue is not None and target.hue is not None:
        cap = np.radians(HUE_CAP_DEGREES)
        turn = _capped(np.radians(turn_degrees(target.hue - stats.hue)), -cap, cap, "hue", bound)
    shift = (0.0, 0.0)
    if stats.cast is not None and target.cast is not None:
        # The gain and turn move the frame's cast too; the shift makes up the rest.
        cosine, sine = np.cos(turn), np.sin(turn)
        moved = (gain * (cosine * stats.cast[0] - sine * stats.cast[1]), gain * (sine * stats.cast[0] + cosine * stats.cast[1]))
        shift_a, shift_b = target.cast[0] - moved[0], target.cast[1] - moved[1]
        size = float(np.hypot(shift_a, shift_b))
        if size > CAST_CAP:
            bound.add("cast")
            shift_a, shift_b = shift_a * CAST_CAP / size, shift_b * CAST_CAP / size
        shift = (float(shift_a), float(shift_b))
    return Params(float(lightness), float(spread), float(balance), float(gain), float(turn), shift, frozenset(bound))


def _lower_share(lightness: tuple[float, float, float]) -> float:
    """The share of the 10th-to-90th percentile spread that lies below the median."""
    low, middle, high = lightness
    return (middle - low) / (high - low) if high - low > 1e-6 else 0.5


def scaled(params: Params, strength: float) -> Params:
    """``params`` scaled toward identity: ``strength`` 0 is no correction, 1 all of it."""
    if strength >= 1:
        return params
    if strength <= 0:
        return Params(bound=params.bound)
    return replace(params, lightness=params.lightness * strength, spread=params.spread**strength, balance=params.balance * strength, gain=params.gain**strength, turn=params.turn * strength, shift=(params.shift[0] * strength, params.shift[1] * strength))


def smooth(series: list[Params]) -> list[Params]:
    """Each parameter smoothed over frames, a running median of ``MEDIAN_FRAMES`` then a Gaussian of
    ``GAUSSIAN_SIGMA`` frames, edges repeated, so nothing flickers. Values within the caps stay within them."""
    if len(series) < 3:
        return series
    columns = np.array([[params.lightness, np.log(params.spread), params.balance, np.log(params.gain), params.turn, params.shift[0], params.shift[1]] for params in series])
    columns = _gaussian(_running_median(columns, MEDIAN_FRAMES), GAUSSIAN_SIGMA)
    return [replace(params, lightness=float(row[0]), spread=float(np.exp(row[1])), balance=float(row[2]), gain=float(np.exp(row[3])), turn=float(row[4]), shift=(float(row[5]), float(row[6]))) for params, row in zip(series, columns, strict=True)]


def _running_median(values: np.ndarray, width: int) -> np.ndarray:
    half = width // 2
    padded = np.concatenate([np.repeat(values[:1], half, axis=0), values, np.repeat(values[-1:], half, axis=0)])
    return np.stack([np.median(padded[index : index + width], axis=0) for index in range(len(values))])


def _gaussian(values: np.ndarray, sigma: float) -> np.ndarray:
    half = int(np.ceil(3 * sigma))
    offsets = np.arange(-half, half + 1)
    weights = np.exp(-(offsets**2) / (2 * sigma**2))
    weights /= weights.sum()
    padded = np.concatenate([np.repeat(values[:1], half, axis=0), values, np.repeat(values[-1:], half, axis=0)])
    return np.stack([weights @ padded[index : index + len(offsets)] for index in range(len(values))])


@dataclass(frozen=True)
class Transform:
    """A frame's correction as applied: the tone curve's knots (lightness in, lightness out, black and white included),
    and the color gain, turn, and shift."""

    knots_in: tuple[float, ...]
    knots_out: tuple[float, ...]
    gain: float
    turn: float
    shift: tuple[float, float]

    @property
    def identity(self) -> bool:
        return self.knots_in == self.knots_out and self.gain == 1 and self.turn == 0 and self.shift == (0.0, 0.0)


def transform_for(stats: ColorStats, params: Params) -> Transform:
    """The transform ``params`` make for a frame with ``stats``."""
    low, middle, high = stats.lightness
    out_middle = middle + params.lightness
    spread = (high - low) * params.spread
    share = min(max(_lower_share(stats.lightness) + params.balance, 0.05), 0.95)
    knots = [(low, out_middle - spread * share), (middle, out_middle), (high, out_middle + spread * (1 - share))]
    if params.lightness == 0 and params.spread == 1 and params.balance == 0:
        knots = [(low, low), (middle, middle), (high, high)]
    xs, ys = [0.0], [0.0]
    for x, y in knots:
        if KNOT_GAP <= x <= 1 - KNOT_GAP and KNOT_GAP <= y <= 1 - KNOT_GAP and x - xs[-1] >= KNOT_GAP and y > ys[-1]:
            xs.append(float(x))
            ys.append(float(y))
    xs.append(1.0)
    ys.append(1.0)
    return Transform(tuple(xs), tuple(ys), params.gain, params.turn, params.shift)


def tone_curve(values: np.ndarray, knots_in: tuple[float, ...], knots_out: tuple[float, ...]) -> np.ndarray:
    """A monotone cubic (Fritsch-Carlson) through the knots, and slope 1 beyond black and white."""
    x, y = np.asarray(knots_in), np.asarray(knots_out)
    secants = np.diff(y) / np.diff(x)
    tangents = np.empty(len(x))
    tangents[0], tangents[-1] = secants[0], secants[-1]
    tangents[1:-1] = (secants[:-1] + secants[1:]) / 2
    # Fritsch-Carlson: flat where a secant is flat, and tangents limited so no segment overshoots.
    for index, secant in enumerate(secants):
        if secant == 0:
            tangents[index] = tangents[index + 1] = 0.0
            continue
        alpha, beta = tangents[index] / secant, tangents[index + 1] / secant
        size = alpha * alpha + beta * beta
        if size > 9:
            factor = 3 / np.sqrt(size)
            tangents[index], tangents[index + 1] = factor * alpha * secant, factor * beta * secant
    values = np.asarray(values, dtype=np.float64)
    inside = np.clip(values, x[0], x[-1])
    segment = np.clip(np.searchsorted(x, inside, side="right") - 1, 0, len(x) - 2)
    width = x[segment + 1] - x[segment]
    t = (inside - x[segment]) / width
    t2, t3 = t * t, t * t * t
    curved = (2 * t3 - 3 * t2 + 1) * y[segment] + (t3 - 2 * t2 + t) * width * tangents[segment] + (-2 * t3 + 3 * t2) * y[segment + 1] + (t3 - t2) * width * tangents[segment + 1]
    return np.where(values < x[0], values - x[0] + y[0], np.where(values > x[-1], values - x[-1] + y[-1], curved))


def apply(frame: np.ndarray, transform: Transform) -> tuple[np.ndarray, int]:
    """``frame`` (H, W, 3 sRGB values) corrected, and how many pixels were brought back onto the sRGB edge. An
    identity transform returns the frame itself, so no rounding can move a pixel."""
    if transform.identity:
        return frame, 0
    lab = oklab.srgb_to_oklab(frame.reshape(-1, 3))
    lab[:, 0] = tone_curve(lab[:, 0], transform.knots_in, transform.knots_out)
    cosine, sine = np.cos(transform.turn), np.sin(transform.turn)
    a, b = lab[:, 1].copy(), lab[:, 2]
    lab[:, 1] = transform.gain * (cosine * a - sine * b) + transform.shift[0]
    lab[:, 2] = transform.gain * (sine * a + cosine * b) + transform.shift[1]
    linear = oklab.oklab_to_linear_srgb(lab)
    outside = np.any((linear < -oklab.INSIDE) | (linear > oklab.srgb_to_linear(np.array(HEADROOM)) + oklab.INSIDE), axis=-1)
    moved = int(np.count_nonzero(outside))
    if moved:
        # Only the colors the transform pushed outside, onto the edge at constant lightness and hue.
        edge, _count = oklab.onto_srgb_edge(lab[outside])
        linear[outside] = oklab.oklab_to_linear_srgb(edge)
    return oklab.linear_to_srgb(linear).reshape(frame.shape).astype(np.float32), moved


def plan_run(frame_stats: list[ColorStats], input_stats: ColorStats, anchor_stats: ColorStats | None, pull: float, strength: float, samples: list[np.ndarray] | None = None) -> list[Params]:
    """Every frame's parameters: fitted to the ramped target, capped, smoothed, and scaled by ``strength``; with each
    frame's ``samples`` (``sample_pixels``), the chroma gains are refined for what the tone curve and the gamut's edge do."""
    count = len(frame_stats)
    fitted = [fit(stats, target_between(input_stats, anchor_stats, pull * ramp(index, count))) for index, stats in enumerate(frame_stats)]
    planned = [scaled(params, strength) for params in smooth(fitted)]
    return refine_gains(planned, frame_stats, samples) if samples is not None else planned


def sample_pixels(frame: np.ndarray) -> np.ndarray:
    """The pixels ``refine_gains`` works on: every ``SAMPLE_STRIDE``-th of each row and column."""
    return np.ascontiguousarray(frame[::SAMPLE_STRIDE, ::SAMPLE_STRIDE])


def refine_gains(params: list[Params], frame_stats: list[ColorStats], samples: list[np.ndarray]) -> list[Params]:
    """Each frame's chroma gain, corrected for what the rest of its transform does to chroma.

    The tone curve works at constant a and b, so darkening a saturated shadow can push it outside sRGB, and bringing it
    onto the edge lowers its chroma; the gain, fitted from statistics alone, cannot see that. So each frame's transform
    is applied to a sample of its pixels, the chroma median it gives is measured, and the gain is scaled by what is
    missing (measured on E0012's run 2: its frame 0's chroma came out 8% short of its input's without it). The factors
    are smoothed as the parameters are, and the gain stays within its cap.
    """
    factors = []
    for frame_params, stats, sample in zip(params, frame_stats, samples, strict=True):
        if frame_params.identity or stats.chroma <= 1e-6:
            factors.append(0.0)
            continue
        before = measure(sample).chroma
        after = measure(apply(sample, transform_for(stats, frame_params))[0]).chroma
        factors.append(float(np.log(before * frame_params.gain / after)) if before > 1e-6 and after > 1e-6 else 0.0)
    column = np.array(factors)[:, None]
    if len(factors) >= 3:
        column = _gaussian(_running_median(column, MEDIAN_FRAMES), GAUSSIAN_SIGMA)
    refined = []
    for frame_params, factor in zip(params, column[:, 0], strict=True):
        if frame_params.identity:
            refined.append(frame_params)
            continue
        gain = frame_params.gain * float(np.exp(factor))
        bound = set(frame_params.bound)
        if not CHROMA_CAP[0] <= gain <= CHROMA_CAP[1]:
            bound.add("chroma")
            gain = min(max(gain, CHROMA_CAP[0]), CHROMA_CAP[1])
        refined.append(replace(frame_params, gain=gain, bound=frozenset(bound)))
    return refined
