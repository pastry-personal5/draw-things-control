"""Read input image sizes, check them against a job's size, and plan a resize."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

EXIF_ORIENTATION = 0x0112
ROTATED_ORIENTATIONS = {5, 6, 7, 8}
# Orientation 1 is upright; a missing tag means the same.
UPRIGHT_ORIENTATIONS = {None, 1}
SIZE_STEP = 64
MAX_DESIRED_SIZE = 8192
DEFAULT_MAX_CROP_PERCENT = 10
# Pillow's errors for a file it cannot open or decode, including one over its pixel limit (MAX_IMAGE_PIXELS).
IMAGE_ERRORS = (OSError, UnidentifiedImageError, Image.DecompressionBombError)

Box = tuple[float, float, float, float]


def read_image_info(path: Path) -> tuple[tuple[int, int], int | None]:
    """Return the displayed width and height and the EXIF orientation, reading only the file header."""
    try:
        with Image.open(path) as image:
            width, height = image.size
            orientation = image.getexif().get(EXIF_ORIENTATION)
    except IMAGE_ERRORS as error:
        raise ValueError(f"'input' is not a readable image: {path} ({error})") from error
    if orientation in ROTATED_ORIENTATIONS:
        return (height, width), orientation
    return (width, height), orientation


def decode_image(path: Path) -> None:
    """Decode every pixel, so an image with a readable header but broken data fails validation."""
    try:
        with Image.open(path) as image:
            image.load()
    except IMAGE_ERRORS as error:
        raise ValueError(f"'input' could not be decoded: {path} ({error})") from error


def check_input_size(path: Path, image_size: tuple[int, int], job_size: tuple[int, int], source: str) -> None:
    """Refuse a job whose first input image does not match its width and height."""
    if image_size == job_size:
        return
    image_width, image_height = image_size
    job_width, job_height = job_size
    raise ValueError(f"Input image {path} is {image_width}x{image_height}, but the job size is {job_width}x{job_height} ({source}). " f"Resize the image to {job_width}x{job_height}, set desired_input_width or desired_input_height, or set config_override.width and config_override.height to {image_width} and {image_height}.")


def floor_to_step(value: int | Fraction) -> int:
    """Round a size down to a multiple of 64."""
    return math.floor(value / SIZE_STEP) * SIZE_STEP


@dataclass(frozen=True)
class ResizePlan:
    """How the first input becomes the job's size: the target, the fit, and the scaled picture.

    ``fit`` is ``none`` (upright and already the target; used as-is), ``rotate`` (already the target, but
    needs an upright copy), ``scale`` (scaled to exactly the target: no crop and no bars), ``crop``, or
    ``letterbox``. ``box`` is the area of the upright input that is resampled; ``None`` means all of it.
    """

    desired_width: int | None
    desired_height: int | None
    max_crop_percent: float | None
    input_size: tuple[int, int]
    exif_orientation: int | None
    target_size: tuple[int, int]
    fit: str
    scaled_size: tuple[int, int]
    crop_percent: float | None
    box: Box | None = None

    @property
    def picture_size(self) -> tuple[int, int]:
        """The size the ``box`` area is resampled to: the scaled size when letterboxed, otherwise the target."""
        return self.scaled_size if self.fit == "letterbox" else self.target_size

    @property
    def needs_copy(self) -> bool:
        """Whether run 1 needs a resized or upright copy instead of the original file."""
        return self.fit != "none"

    def describe(self, name: str) -> str:
        """One line saying what happens to the input before run 1."""
        target = _size_text(self.target_size)
        if self.fit == "none":
            return f"Input {name} is already {target}; no resize needed"
        if self.fit == "rotate":
            return f"Input {name} (EXIF orientation {self.exif_orientation}) will be rotated upright; already {target}"
        rotated = "" if self.exif_orientation in UPRIGHT_ORIENTATIONS else f", after rotating it upright (EXIF orientation {self.exif_orientation})"
        scaled = _size_text(self.scaled_size)
        if self.fit == "scale":
            return f"Input {name} ({_size_text(self.input_size)}) will be scaled to {target} for run 1{rotated}"
        if self.fit == "crop":
            return f"Input {name} ({_size_text(self.input_size)}) will be scaled to {scaled} and cropped to {target} ({self.crop_percent:.1f}%) for run 1{rotated}"
        return f"Input {name} ({_size_text(self.input_size)}) will be scaled to {scaled} and letterboxed to {target} for run 1{rotated}"

    def as_manifest(self) -> dict[str, Any]:
        """The plan as the job manifest records it; the crop fields are set only when one desired key was given."""
        cropping = (self.desired_width is None) != (self.desired_height is None)
        return {
            "desired_width": self.desired_width,
            "desired_height": self.desired_height,
            "max_crop_percent": self.max_crop_percent if cropping else None,
            "input_size": list(self.input_size),
            "exif_orientation": self.exif_orientation,
            "target_size": list(self.target_size),
            "fit": self.fit,
            "scaled_size": list(self.scaled_size),
            "crop_percent": round(self.crop_percent, 1) if cropping and self.crop_percent is not None else None,
        }


def resize_plan(name: str, input_size: tuple[int, int], orientation: int | None, desired_width: int | None, desired_height: int | None, max_crop_percent: float | None = None) -> ResizePlan:
    """Work out the target size, the fit, and the source area from the desired size keys.

    ``max_crop_percent`` defaults to ``DEFAULT_MAX_CROP_PERCENT``.
    Raises ``ValueError`` whose message starts with the offending key in quotes.
    """
    if desired_width is None and desired_height is None:
        raise ValueError("resize_plan needs desired_width or desired_height")
    if max_crop_percent is None:
        max_crop_percent = DEFAULT_MAX_CROP_PERCENT
    input_width, input_height = input_size
    width = _floored("desired_input_width", desired_width)
    height = _floored("desired_input_height", desired_height)
    if width is None:
        assert height is not None
        width = _derived("desired_input_width", Fraction(height * input_width, input_height), name, input_size, f"height {height}")
    elif height is None:
        height = _derived("desired_input_height", Fraction(width * input_height, input_width), name, input_size, f"width {width}")
    target = (width, height)
    cropping = desired_width is None or desired_height is None

    upright = orientation in UPRIGHT_ORIENTATIONS
    if input_size == target:
        return ResizePlan(desired_width, desired_height, max_crop_percent, input_size, orientation, target, "none" if upright else "rotate", target, 0.0 if cropping else None)

    if cropping:
        scale = max(Fraction(width, input_width), Fraction(height, input_height))
        scaled = (max(_round(input_width * scale), width), max(_round(input_height * scale), height))
        crop_percent = max((scaled[0] - width) / scaled[0], (scaled[1] - height) / scaled[1]) * 100
        if crop_percent > max_crop_percent:
            given = f"width {desired_width}" if desired_width is not None else f"height {desired_height}"
            key = "desired_input_width" if desired_width is not None else "desired_input_height"
            lost, of = (scaled[0] - width, scaled[0]) if scaled[0] > width else (scaled[1] - height, scaled[1])
            raise ValueError(f"'max_input_crop_percent' is {max_crop_percent}, but {name} ({_size_text(input_size)}) at {given} scales to {_size_text(scaled)} and would lose {lost} px of {of} ({crop_percent:.1f}%) to reach {_size_text(target)}. Choose a larger {key}, raise max_input_crop_percent, or set both keys to letterbox instead.")
        # A scaled size equal to the target can still hide a sub-pixel crop, which the exact box keeps.
        return ResizePlan(desired_width, desired_height, max_crop_percent, input_size, orientation, target, "scale" if scaled == target else "crop", scaled, crop_percent, _cover_box(input_size, target, scale))

    scale = min(Fraction(width, input_width), Fraction(height, input_height))
    scaled = (min(_round(input_width * scale), width), min(_round(input_height * scale), height))
    return ResizePlan(desired_width, desired_height, max_crop_percent, input_size, orientation, target, "scale" if scaled == target else "letterbox", scaled, None)


def _cover_box(input_size: tuple[int, int], target: tuple[int, int], scale: Fraction) -> Box:
    """The centered source area that ``scale`` maps onto ``target``."""
    width, height = input_size
    target_width, target_height = target
    if scale == 1:
        # No scaling: whole pixels, so nothing is resampled. An odd leftover comes off the right or bottom.
        left, top = (width - target_width) // 2, (height - target_height) // 2
        return (float(left), float(top), float(left + target_width), float(top + target_height))
    # The exact, possibly fractional, centered area, so the aspect ratio and centering have no rounding error.
    box_width, box_height = target_width / scale, target_height / scale
    left, top = (width - box_width) / 2, (height - box_height) / 2
    return (float(left), float(top), float(left + box_width), float(top + box_height))


def _floored(key: str, value: int | None) -> int | None:
    if value is None:
        return None
    floored = floor_to_step(value)
    if floored < SIZE_STEP:
        raise ValueError(f"'{key}' is {value}, which floors to 0 (minimum {SIZE_STEP})")
    return floored


def _derived(key: str, exact: Fraction, name: str, input_size: tuple[int, int], given: str) -> int:
    value = floor_to_step(exact)
    prefix = f"'{key}' derived from {name} ({_size_text(input_size)}) and {given} is {float(exact):.1f}, which floors to {value}"
    if value < SIZE_STEP:
        raise ValueError(f"{prefix} (minimum {SIZE_STEP})")
    if value > MAX_DESIRED_SIZE:
        raise ValueError(f"{prefix} (maximum {MAX_DESIRED_SIZE})")
    return value


def _round(value: Fraction) -> int:
    """Round half up, so the result does not depend on banker's rounding."""
    return math.floor(value + Fraction(1, 2))


def _size_text(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"
