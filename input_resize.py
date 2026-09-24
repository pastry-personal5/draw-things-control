"""Write the resized first input of a job."""

from __future__ import annotations

import io
import math
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
from loguru import logger
from PIL import Image, ImageCms, ImageOps

from input_size import IMAGE_ERRORS, Box, ResizePlan

BLACK = (0, 0, 0)
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
# Modes that hold more than 8 bits per channel; Pillow's convert("RGB") clips them instead of scaling.
HIGH_BIT_DEPTH_MODES = {"I", "I;16", "I;16L", "I;16B", "I;16N", "F"}
# How far a downscaled pixel is pulled back into the range of the source pixels under the filter's main lobe.
# Linear-light Lanczos rings into dark halos beside bright edges; 0.7 brings them back to what sRGB-value
# Lanczos gives, while edges stay as sharp. 1.0 would remove ringing entirely but soften edges.
ANTI_RINGING = 0.7


def resize_image(source: Path, plan: ResizePlan, destination: Path) -> None:
    """Write ``source`` upright, in sRGB, scaled without distortion, and fitted to the plan's target, as PNG.

    The picture is resampled once, straight from the source pixels; see ``resample``.
    """
    with Image.open(source) as opened:
        image = to_srgb(ImageOps.exif_transpose(opened))
    picture = _picture(image, plan.box, plan.picture_size)
    if picture.size != plan.target_size:
        canvas = Image.new("RGB", plan.target_size, BLACK)
        # An odd leftover puts the extra pixel on the right or bottom.
        canvas.paste(picture, ((plan.target_size[0] - picture.width) // 2, (plan.target_size[1] - picture.height) // 2))
        picture = canvas
    picture.save(destination, format="PNG")


def to_srgb(image: Image.Image) -> Image.Image:
    """Return 8-bit RGB in sRGB: transparency onto black, high bit depth scaled down, and any ICC profile converted."""
    # Read the profile first: the steps below make new images, which do not carry it.
    profile = image.info.get("icc_profile")
    if image.mode in HIGH_BIT_DEPTH_MODES:
        image = _to_8_bit_gray(image)
    if "A" in image.getbands() or "transparency" in image.info:
        # Black stays black in every color space, so flattening before the profile conversion is safe.
        gray = image.mode in ("L", "LA", "La")
        flattened = Image.alpha_composite(Image.new("RGBA", image.size, BLACK + (255,)), image.convert("RGBA"))
        image = flattened.convert("L" if gray else "RGB")
    if profile and not _is_srgb(profile):
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            # Relative colorimetric with black point compensation, the usual choice for photographs.
            converted = ImageCms.profileToProfile(image, source_profile, SRGB, renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB", flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
        except (ImageCms.PyCMSError, OSError, ValueError) as error:
            logger.warning("Could not convert the input's embedded color profile to sRGB ({}); using its pixel values as they are, so colors may shift", error)
        else:
            return converted
    return image.convert("RGB")


def _is_srgb(profile: bytes) -> bool:
    """Whether an embedded profile is already sRGB, so converting it would only cost time and nudge dark values."""
    try:
        description = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(profile)))
    except (ImageCms.PyCMSError, OSError, ValueError):
        return False
    return description.strip().startswith("sRGB")


def _to_8_bit_gray(image: Image.Image) -> Image.Image:
    """Scale a 16-bit or 32-bit grayscale image to 8 bits, rounding to the nearest level."""
    if image.mode == "F":
        # Float images are taken as 0.0 to 1.0 when they fit, otherwise as 0 to 65535.
        low, high = image.getextrema()
        scale = 255 if 0 <= low and high <= 1 else 255 / 65535
        return image.point(lambda value: value * scale + 0.5).convert("L")
    return image.convert("I").point(lambda value: value * (255 / 65535) + 0.5).convert("L")


def _picture(image: Image.Image, box: Box | None, size: tuple[int, int]) -> Image.Image:
    """The ``box`` area of ``image`` at ``size``: cut losslessly when that is whole pixels at scale 1, otherwise resampled."""
    left, top, right, bottom = box if box is not None else (0.0, 0.0, float(image.width), float(image.height))
    if all(value.is_integer() for value in (left, top, right, bottom)) and (right - left, bottom - top) == size:
        return image.crop((int(left), int(top), int(right), int(bottom)))
    return resample(image, size, (left, top, right, bottom))


def resample(image: Image.Image, size: tuple[int, int], box: Box | None = None) -> Image.Image:
    """Resample the ``box`` area of an 8-bit RGB image to ``size`` with Lanczos, in floating point.

    Downscaling works in linear light, so fine bright detail keeps its brightness (in sRGB values,
    alternating white and black lines would average a third too dark), with partial anti-ringing.
    Upscaling works in sRGB values, which keeps edges sharper and rings less than linear light.
    The result is rounded to 8 bits once, at the end.
    """
    left, top, right, bottom = box if box is not None else (0.0, 0.0, float(image.width), float(image.height))
    # Source pixels per output pixel; the larger axis decides, as both axes scale almost alike.
    factor = max((right - left) / size[0], (bottom - top) / size[1])
    downscaling = factor > 1
    rgb = np.asarray(image if image.mode == "RGB" else image.convert("RGB"))
    # Output pixel centers in source pixels, for the anti-ringing range.
    rows = _centers(top, bottom, size[1], image.height)
    columns = _centers(left, right, size[0], image.width)
    channels = []
    # One channel at a time keeps memory near one float copy of the source.
    for index in range(3):
        # The source has 8 bits, so a 256-entry table converts it exactly, without large temporary arrays.
        channel = (SRGB_TO_LINEAR if downscaling else SRGB_VALUES)[rgb[..., index]]
        resampled = np.array(Image.fromarray(channel, "F").resize(size, Image.Resampling.LANCZOS, box=(left, top, right, bottom)), dtype=np.float32)
        if downscaling:
            radius = math.ceil(factor)
            low = _window_at(channel, rows, columns, radius, np.minimum)
            high = _window_at(channel, rows, columns, radius, np.maximum)
            resampled += ANTI_RINGING * (np.clip(resampled, low, high) - resampled)
            resampled = _linear_to_srgb(resampled)
        channels.append(np.clip(np.rint(resampled * 255), 0, 255).astype(np.uint8))
        del channel
    return Image.fromarray(np.stack(channels, axis=-1), "RGB")


def _centers(start: float, end: float, count: int, limit: int) -> np.ndarray:
    """The source pixel under each output pixel's center."""
    positions = start + (np.arange(count) + 0.5) * ((end - start) / count)
    return np.clip(np.floor(positions).astype(np.int64), 0, limit - 1)


def _window_at(channel: np.ndarray, rows: np.ndarray, columns: np.ndarray, radius: int, combine: Callable[[np.ndarray, np.ndarray], np.ndarray]) -> np.ndarray:
    """Minimum or maximum of the source pixels within ``radius`` of each output center, edges repeated."""
    height, width = channel.shape
    across = channel[:, columns]
    for offset in range(-radius, radius + 1):
        if offset:
            across = combine(across, channel[:, np.clip(columns + offset, 0, width - 1)])
    result = across[rows]
    for offset in range(-radius, radius + 1):
        if offset:
            result = combine(result, across[np.clip(rows + offset, 0, height - 1)])
    return result


def _srgb_to_linear(values: np.ndarray) -> np.ndarray:
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4).astype(np.float32)


SRGB_VALUES = (np.arange(256) / 255).astype(np.float32)
SRGB_TO_LINEAR = _srgb_to_linear(SRGB_VALUES.astype(np.float64))


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0, 1)
    return np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1 / 2.4) - 0.055).astype(np.float32)


class TemporaryInput:
    """A resized first input in its own temporary directory, removed by ``cleanup``."""

    def __init__(self, source: Path, plan: ResizePlan) -> None:
        self._directory = Path(tempfile.mkdtemp(prefix="draw-things-control-"))
        width, height = plan.target_size
        self.path = self._directory / f"{source.stem}-{width}x{height}.png"
        try:
            resize_image(source, plan, self.path)
        except BaseException as error:
            self.cleanup()
            # ValueError too: Pillow raises it for a mode it cannot convert, such as LAB.
            if isinstance(error, IMAGE_ERRORS + (ValueError,)):
                raise ValueError(f"Could not resize input {source}: {error}") from error
            raise

    def cleanup(self) -> None:
        """Remove the temporary directory; safe to call more than once."""
        shutil.rmtree(self._directory, ignore_errors=True)
