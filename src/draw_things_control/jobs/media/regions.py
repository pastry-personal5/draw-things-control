"""A frame's regions (Milestone 09's increment E): people, their skin, and the background, from a ``Segmenter``.

A ``Segmenter`` gives a frame's person mask, at its own resolution, and the landmarks of each face, in the frame's
pixels. The skin of each face is the hull of its contour and its brows, less the eyes, the brows, and the outer lips
(``faceContour`` runs from one cheek over the chin to the other, open at the top, so it bounds nothing on its own).
Those pixels set a Gaussian over Oklab's ``a`` and ``b``, and the skin mask is how well each pixel fits it, confined by
the person mask: all of a pixel within ``SKIN_INNER`` standard deviations of the face's skin, none beyond
``SKIN_OUTER``. Lightness is left out: with it, the face's narrow range of light dropped lit and shaded body skin
(seen on E0017's and E0021's frames, 2026-10-01).

What is kept between the correction's two passes is ``FrameMasks``: the person and skin masks, 8-bit, at the
segmenter's resolution. Statistics read a region as the pixels its mask, stretched to the frame, holds at half or
more; blending reads the masks smoothed over three frames, stretched to the frame, then feathered by 1% of the frame's
shorter side, so no edge shows. A region counts only with enough of it: people 2% of the frame, skin 0.5%.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from loguru import logger
from PIL import Image, ImageDraw, ImageFilter

from draw_things_control.jobs.media import oklab
from draw_things_control.jobs.media.color_stats import ColorStats, measure_lab

# A region counts only with this share of the frame: people, and skin (Milestone 09).
PEOPLE_SHARE = 0.02
SKIN_SHARE = 0.005
# A region is corrected apart only when at least this share of a run's frames have it.
FRAMES_SHARE = 0.5
# Masks are feathered by this share of the frame's shorter side.
FEATHER_SHARE = 0.01
# A pixel is all skin within SKIN_INNER standard deviations (Mahalanobis, over Oklab's a and b) of its frame's face
# skin, and none beyond SKIN_OUTER, with a smoothstep between.
SKIN_INNER = 2.0
SKIN_OUTER = 3.0
# Added to each axis's standard deviation, in Oklab units, so a face of one flat color still has a usable Gaussian.
SKIN_SPREAD_FLOOR = 0.005
# A face's skin needs at least this many pixels to set the Gaussian.
MIN_FACE_PIXELS = 64
# The brows, the eyes, and the lips are cut out with an outline this share of the face's width, beyond their own points.
CUT_SHARE = 0.04
# The regions statistics are kept for, besides the whole frame.
REGIONS = ("people", "skin", "background")


@dataclass(frozen=True)
class Face:
    """One face's landmarks, each (N, 2) as x and y in the frame's pixels, the origin at the top left."""

    contour: np.ndarray
    left_eye: np.ndarray
    right_eye: np.ndarray
    left_brow: np.ndarray
    right_brow: np.ndarray
    lips: np.ndarray


@dataclass(frozen=True)
class Segmentation:
    """What a segmenter finds in a frame: the person mask (uint8, 255 for a person, at the segmenter's own resolution,
    stretched over the whole frame), and the faces."""

    person: np.ndarray
    faces: tuple[Face, ...] = ()


class Segmenter(Protocol):
    """Finds the people and faces of a frame (H, W, 3 sRGB values, 1.0 for white); raises when it cannot."""

    def __call__(self, frame: np.ndarray) -> Segmentation: ...


@dataclass(frozen=True)
class FrameMasks:
    """One frame's soft masks, uint8 at the segmenter's resolution: people, and the skin within them."""

    person: np.ndarray
    skin: np.ndarray
    faces: int


@dataclass(frozen=True)
class Regions:
    """One frame's regions at the frame's size: the masks kept, and the pixels each region's statistics read (None when
    the region is too small to count)."""

    masks: FrameMasks
    people: np.ndarray | None
    skin: np.ndarray | None


def stretch(mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """An 8-bit mask stretched to ``size`` (width, height), bilinear, as uint8."""
    height, width = mask.shape
    if (width, height) == size:
        return mask
    return np.asarray(Image.fromarray(mask, "L").resize(size, Image.Resampling.BILINEAR))


def _hull(points: np.ndarray) -> np.ndarray:
    """The convex hull of (N, 2) points, by the monotone chain."""
    unique = sorted({(float(x), float(y)) for x, y in points})
    if len(unique) < 3:
        return np.array(unique)

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return np.array(lower[:-1] + upper[:-1])


def face_skin(face: Face, size: tuple[int, int]) -> np.ndarray:
    """The face's own skin, as a boolean mask of ``size`` (width, height): the hull of its contour and brows, less its
    eyes, brows, and outer lips."""
    image = Image.new("L", size, 0)
    hull = _hull(np.concatenate([face.contour, face.left_brow, face.right_brow]))
    if len(hull) < 3:
        return np.zeros((size[1], size[0]), dtype=bool)
    draw = ImageDraw.Draw(image)
    draw.polygon([tuple(point) for point in hull], fill=255)
    width = max(1, round(float(np.ptp(hull[:, 0])) * CUT_SHARE))
    for part in (face.left_eye, face.right_eye, face.left_brow, face.right_brow, face.lips):
        if len(part) >= 3:
            draw.polygon([tuple(point) for point in part], fill=0, outline=0, width=width)
        if len(part) >= 2:
            draw.line([tuple(point) for point in part], fill=0, width=width)
    return np.asarray(image) > 0


@dataclass(frozen=True)
class SkinModel:
    """A Gaussian over Oklab's ``a`` and ``b``: the mean and the inverse covariance of a frame's face skin."""

    mean: np.ndarray
    inverse: np.ndarray

    def membership(self, lab: np.ndarray) -> np.ndarray:
        """How much each Oklab color (N, 3) is skin: 1 within ``SKIN_INNER`` standard deviations, 0 beyond ``SKIN_OUTER``."""
        offset = lab[:, 1:] - self.mean
        distance = np.sqrt(np.maximum(np.einsum("ni,ij,nj->n", offset, self.inverse, offset), 0))
        share = np.clip((distance - SKIN_INNER) / (SKIN_OUTER - SKIN_INNER), 0, 1)
        return (1 - share * share * (3 - 2 * share)).astype(np.float32)


def skin_model(lab: np.ndarray) -> SkinModel | None:
    """The Gaussian of a face's skin colors (N, 3): fitted, then fitted again without what lies beyond ``SKIN_OUTER``
    (hair, a hand, the background the hull takes in). None with too few pixels."""
    if len(lab) < MIN_FACE_PIXELS:
        return None
    model = _gaussian(lab)
    kept = lab[model.membership(lab) > 0]
    return _gaussian(kept) if len(kept) >= MIN_FACE_PIXELS else model


def _gaussian(lab: np.ndarray) -> SkinModel:
    chromaticity = lab[:, 1:]
    mean = chromaticity.mean(axis=0)
    covariance = np.cov(chromaticity.T) + np.diag(np.full(2, SKIN_SPREAD_FLOOR**2))
    return SkinModel(mean, np.linalg.inv(covariance))


def find_regions(frame: np.ndarray, segmentation: Segmentation, lab: np.ndarray | None = None) -> Regions:
    """``frame``'s regions from what the segmenter found in it; ``lab`` is the frame in Oklab (H * W, 3), when already
    converted."""
    height, width = frame.shape[:2]
    size = (width, height)
    lab = oklab.srgb_to_oklab(frame.reshape(-1, 3)) if lab is None else lab
    person = stretch(segmentation.person, size)
    skin = np.zeros((height, width), dtype=np.uint8)
    faces = [face_skin(face, size) for face in segmentation.faces]
    if faces:
        model = skin_model(lab[np.logical_or.reduce(faces).ravel()])
        if model is not None:
            membership = model.membership(lab).reshape(height, width)
            skin = np.rint(membership * person).astype(np.uint8)
    masks = FrameMasks(segmentation.person, _shrink(skin, segmentation.person.shape), len(segmentation.faces))
    return _regions(masks, person, skin)


def _shrink(mask: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    if mask.shape == shape:
        return mask
    return np.asarray(Image.fromarray(mask, "L").resize((shape[1], shape[0]), Image.Resampling.BILINEAR))


def regions_from_masks(masks: FrameMasks, size: tuple[int, int]) -> Regions:
    """A frame's regions at ``size`` (width, height) from the masks kept for it."""
    return _regions(masks, stretch(masks.person, size), stretch(masks.skin, size))


def _regions(masks: FrameMasks, person: np.ndarray, skin: np.ndarray) -> Regions:
    people = person >= 128
    skinned = skin >= 128
    total = people.size
    return Regions(masks, people if people.sum() >= PEOPLE_SHARE * total else None, skinned if skinned.sum() >= SKIN_SHARE * total else None)


def region_stats(frame: np.ndarray, regions: Regions | None, lab: np.ndarray | None = None) -> dict[str, ColorStats]:
    """The statistics of the whole frame (``frame``) and of each region that counts: ``people``, ``skin``, and
    ``background`` (what is not people, when people count)."""
    lab = oklab.srgb_to_oklab(frame.reshape(-1, 3)) if lab is None else lab
    stats = {"frame": measure_lab(lab)}
    if regions is None or regions.people is None:
        return stats
    people = regions.people.ravel()
    stats["people"] = measure_lab(lab[people])
    if (~people).sum() >= PEOPLE_SHARE * people.size:
        stats["background"] = measure_lab(lab[~people])
    if regions.skin is not None:
        stats["skin"] = measure_lab(lab[regions.skin.ravel()])
    return stats


def blend_weights(masks: list[FrameMasks | None], index: int, size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray] | None:
    """Frame ``index``'s person and skin weights (H, W, 0 to 1) for blending: each mask averaged with its neighbours',
    stretched to ``size`` (width, height), and feathered by ``FEATHER_SHARE`` of the shorter side. Skin never weighs more
    than people. None when the frame has no masks."""
    if masks[index] is None:
        return None
    window = [mask for mask in masks[max(index - 1, 0) : index + 2] if mask is not None]
    radius = FEATHER_SHARE * min(size)
    weights = []
    for name in ("person", "skin"):
        mean = np.mean([getattr(mask, name).astype(np.float32) for mask in window], axis=0)
        stretched = Image.fromarray(np.rint(mean).astype(np.uint8), "L").resize(size, Image.Resampling.BILINEAR)
        weights.append(np.asarray(stretched.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255)
    person, skin = weights
    return person, np.minimum(skin, person)


def make_segmenter(factory: Callable[[], Segmenter | None] | None) -> Segmenter | None:
    """The segmenter ``factory`` gives; None, as with no Vision, when there is none or making it raises."""
    if factory is None:
        return None
    try:
        return factory()
    # Whatever breaks making it, colors are still measured and corrected over the whole frame.
    except Exception as error:
        logger.warning("The segmenter could not be made ({}); colors are measured and corrected over the whole frame", error)
        return None


class RegionFinder:
    """Finds each frame's regions with a segmenter, until it raises: from then on every frame is one region, and
    ``notes`` says why. With no segmenter, it finds none and says nothing."""

    def __init__(self, segmenter: Segmenter | None) -> None:
        self._segmenter = segmenter
        self.notes: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self._segmenter is not None

    def __call__(self, frame: np.ndarray, lab: np.ndarray | None = None) -> Regions | None:
        if self._segmenter is None:
            return None
        try:
            return find_regions(frame, self._segmenter(frame), lab)
        # Whatever breaks the segmenter, the whole frame is still measured and corrected.
        except Exception as error:
            self._segmenter = None
            self.notes = (f"Apple Vision failed ({error}), so the whole frame is one region from then on.",)
            return None
