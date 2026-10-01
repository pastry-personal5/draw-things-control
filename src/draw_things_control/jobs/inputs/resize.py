"""Write run 1's copy of a job's first input: upright, alpha flattened, sRGB, 8-bit, at exactly the job's size.

Every job with an input gets one (owner decision, Milestone 09), so draw-things-cli only ever reads a PNG it takes as
it is, and this decides every value the model sees. The source is read into floating point, converted to sRGB there,
resampled, and rounded to 8 bits once, at the end.
"""

from __future__ import annotations

import io
import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
from loguru import logger
from PIL import Image, ImageCms, ImageOps

from draw_things_control.jobs.inputs.gamut import GamutMapper, GamutReport, read_matrix_profile
from draw_things_control.jobs.inputs.size import EXIF_ORIENTATION, IMAGE_ERRORS, Box, ResizePlan
from draw_things_control.jobs.media import oklab

BLACK = (0, 0, 0)
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
# Modes that hold more than 8 bits per channel; Pillow's convert("RGB") clips them instead of scaling.
HIGH_BIT_DEPTH_MODES = {"I", "I;16", "I;16L", "I;16B", "I;16N", "F"}
# Modes whose values are RGB or gray as they stand, read here into floating point; any other (CMYK, LAB, YCbCr) is
# converted by Pillow, through LittleCMS when it has a profile.
DIRECT_MODES = {"RGB", "RGBA", "RGBX", "RGBa", "L", "LA", "La", "P", "PA", "1"} | HIGH_BIT_DEPTH_MODES
# How far a downscaled pixel is pulled back into the range of the source pixels under the filter's main lobe.
# Linear-light Lanczos rings into dark halos beside bright edges; 0.7 brings them back to what sRGB-value
# Lanczos gives, while edges stay as sharp. 1.0 would remove ringing entirely but soften edges.
ANTI_RINGING = 0.7
# Pillow's own transposition for each EXIF orientation, as ImageOps.exif_transpose applies it.
ORIENTATIONS = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}
FFMPEG_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class SourceReport:
    """How the source's values became sRGB, for the resized-input check.

    ``conversion`` is ``none`` (no profile, or sRGB), ``matrix`` (a matrix-and-curves profile, applied in floating
    point with gamut mapping), ``littlecms`` (any other profile, LittleCMS's perceptual intent, 8-bit, what is still
    outside sRGB clipped), ``lab`` (Lab values, converted by LittleCMS's Lab transform, what is outside sRGB clipped,
    any profile unused), or ``failed`` (the profile could not be used; the values were taken as sRGB). A YCbCr or HSV
    source is converted to RGB first, and its profile then applies as an RGB source's.
    ``sixteen_bit`` is ``ffmpeg`` for a 16-bit RGB PNG read with rounding, ``high_bytes`` for one Pillow read by its high
    bytes because ffmpeg was not found, and None for any other source.
    """

    profile: str | None = None
    conversion: str = "none"
    sixteen_bit: str | None = None
    gamut: GamutReport | None = None


def resize_image(source: Path, plan: ResizePlan, destination: Path, ffmpeg: str | None = None) -> SourceReport:
    """Write ``source`` upright, in sRGB, scaled without distortion, and fitted to the plan's target, as 8-bit PNG.

    The picture is resampled once, straight from the source's floating-point sRGB values; see ``resample``.
    """
    values, report = read_srgb(source, ffmpeg)
    picture = _picture(values, plan.box, plan.picture_size)
    if picture.shape[1::-1] != plan.target_size:
        canvas = np.zeros((plan.target_size[1], plan.target_size[0], 3), dtype=np.float32)
        # An odd leftover puts the extra pixel on the right or bottom.
        x, y = (plan.target_size[0] - picture.shape[1]) // 2, (plan.target_size[1] - picture.shape[0]) // 2
        canvas[y : y + picture.shape[0], x : x + picture.shape[1]] = picture
        picture = canvas
    Image.fromarray(to_8_bit(picture), "RGB").save(destination, format="PNG")
    return report


def to_8_bit(values: np.ndarray) -> np.ndarray:
    """Floating-point sRGB values, 0 to 1, rounded once to 8 bits."""
    return np.clip(np.rint(values * 255), 0, 255).astype(np.uint8)


def read_srgb(source: Path, ffmpeg: str | None = None) -> tuple[np.ndarray, SourceReport]:
    """The source, upright, alpha flattened onto black, as sRGB values (H, W, 3, float32, 0 to 1), and how they were made.

    A 16-bit RGB PNG is read through ffmpeg, which keeps every bit (Pillow 12.3 reads it as 8-bit, keeping each
    sample's high byte); without ffmpeg, Pillow's high bytes are used. A profile that is not sRGB is converted:
    a matrix-and-curves profile in floating point with gamut mapping (``gamut.py``), any other through LittleCMS.
    """
    with Image.open(source) as opened:
        profile = opened.info.get("icc_profile")
        orientation = opened.getexif().get(EXIF_ORIENTATION)
        deep = _is_16_bit_rgb_png(opened, source)
        if deep and ffmpeg is not None:
            values, image = _upright(_ffmpeg_rgb(source, ffmpeg, opened.size), orientation), None
        else:
            image = ImageOps.exif_transpose(opened)
            values = _float_rgb(image) if image.mode in DIRECT_MODES else None
    sixteen_bit = ("ffmpeg" if ffmpeg is not None else "high_bytes") if deep else None
    if image is not None and image.mode == "LAB":
        # Lab values name their colors: Pillow converts them with LittleCMS's Lab (D50) transform, and a profile
        # beside them is not needed (owner decision).
        return _float_rgb(image.convert("RGB")), SourceReport(_profile_name(profile) if profile else None, "lab", sixteen_bit)
    if image is not None and image.mode in ("YCbCr", "HSV"):
        # Encodings of RGB, whose values the profile then describes (owner decision).
        image = image.convert("RGB")
        values = _float_rgb(image)
    if not profile or _is_srgb(profile):
        if values is None:
            assert image is not None
            values = _float_rgb(image.convert("RGB"))
        return values, SourceReport(_profile_name(profile) if profile else None, "none", sixteen_bit)
    name = _profile_name(profile)
    matrix = read_matrix_profile(profile) if values is not None else None
    if matrix is not None:
        assert values is not None
        mapped, gamut = GamutMapper(matrix)(values)
        return mapped, SourceReport(name, "matrix", sixteen_bit, gamut)
    # LittleCMS converts 8-bit images only, so a floating-point source is rounded for it; gray stays gray, for a gray profile.
    if image is None or image.mode not in ("RGB", "L", "CMYK"):
        if values is None:
            # No mode Pillow opens today: every other one was read as floating point above. TemporaryInput reports it.
            raise ValueError(f"its mode {image.mode if image is not None else 'unknown'} with the profile {name} cannot be converted to sRGB")
        gray = image is not None and image.mode in HIGH_BIT_DEPTH_MODES | {"LA", "La"}
        image = Image.fromarray(to_8_bit(values[..., 0]), "L") if gray else Image.fromarray(to_8_bit(values), "RGB")
    converted = _littlecms(image, profile)
    if converted is None:
        fallback = values if values is not None else _float_rgb(image.convert("RGB"))
        return fallback, SourceReport(name, "failed", sixteen_bit)
    return _float_rgb(converted), SourceReport(name, "littlecms", sixteen_bit)


def _littlecms(image: Image.Image, profile: bytes) -> Image.Image | None:
    """``image`` converted from ``profile`` to sRGB with the perceptual intent, which uses the profile's own gamut
    mapping where it has one; None, with a warning, when the profile cannot be used."""
    try:
        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        converted = ImageCms.profileToProfile(image, source_profile, SRGB, renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB", flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
    except (ImageCms.PyCMSError, OSError, ValueError) as error:
        logger.warning("Could not convert the input's embedded color profile to sRGB ({}); using its pixel values as they are, so colors may shift", error)
        return None
    return converted


def _is_srgb(profile: bytes) -> bool:
    """Whether an embedded profile is already sRGB, so converting it would only cost time and nudge dark values."""
    return _profile_name(profile).startswith("sRGB")


def _profile_name(profile: bytes) -> str:
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(profile))).strip() or "unnamed profile"
    except (ImageCms.PyCMSError, OSError, ValueError):
        return "unreadable profile"


def _is_16_bit_rgb_png(image: Image.Image, source: Path) -> bool:
    """Whether a PNG holds 16-bit RGB or RGBA samples, which Pillow opens as 8-bit high bytes."""
    if image.format != "PNG" or image.mode not in ("RGB", "RGBA"):
        return False
    # Imported here: checks.py imports this module inside its own functions.
    from draw_things_control.jobs.media.checks import png_chunks

    header, _chunks = png_chunks(source)
    return header.get("bit_depth") == 16


def _ffmpeg_rgb(source: Path, ffmpeg: str, size: tuple[int, int]) -> np.ndarray:
    """A 16-bit PNG's RGB, alpha flattened onto black, as floats, read by ffmpeg with every bit kept, as stored: ffmpeg
    8.1.1 turns a PNG upright by its EXIF orientation unless told not to, and ``_upright`` does that here."""
    command = [ffmpeg, "-v", "error", "-nostdin", "-noautorotate", "-i", str(source), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba64le", "-"]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"ffmpeg could not read it: {error}") from error
    width, height = size
    samples = np.frombuffer(result.stdout, dtype="<u2")
    if result.returncode != 0 or samples.size != width * height * 4:
        raise ValueError(f"ffmpeg could not read it: {result.stderr.decode(errors='replace').strip() or f'exit code {result.returncode}'}")
    rgba = samples.reshape(height, width, 4).astype(np.float32) / 65535
    return rgba[..., :3] * rgba[..., 3:]


def _upright(values: np.ndarray, orientation: int | None) -> np.ndarray:
    """``values`` turned as ImageOps.exif_transpose turns an image with this EXIF orientation."""
    method = ORIENTATIONS.get(orientation or 1)
    if method is None:
        return values
    channels = [np.asarray(Image.fromarray(np.ascontiguousarray(values[..., index]), "F").transpose(method)) for index in range(3)]
    return np.stack(channels, axis=-1)


def _float_rgb(image: Image.Image) -> np.ndarray:
    """RGB values from 0 to 1 (float32), alpha flattened onto black; gray becomes three equal channels, and high bit
    depth gray is scaled, not clipped."""
    if image.mode in HIGH_BIT_DEPTH_MODES:
        gray = _high_bit_depth_gray(image)
        return np.repeat(gray[..., None], 3, axis=-1)
    if "A" in image.getbands() or "transparency" in image.info or image.mode in ("RGBa", "La", "PA"):
        # Black stays black in every color space, so flattening before the profile conversion is safe.
        rgba = np.asarray(image.convert("RGBA"), dtype=np.float32) / 255
        return rgba[..., :3] * rgba[..., 3:]
    return np.asarray(image.convert("RGB"), dtype=np.float32) / 255


def _high_bit_depth_gray(image: Image.Image) -> np.ndarray:
    """A 16-bit or 32-bit grayscale image's values from 0 to 1."""
    if image.mode == "F":
        # Float images are taken as 0.0 to 1.0 when they fit, otherwise as 0 to 65535.
        low, high = cast(tuple[float, float], image.getextrema())
        scale = 1.0 if low >= 0 and high <= 1 else 1 / 65535
        return np.clip(np.asarray(image, dtype=np.float32) * scale, 0, 1)
    return np.clip(np.asarray(image.convert("I"), dtype=np.float32) / 65535, 0, 1)


def _picture(values: np.ndarray, box: Box | None, size: tuple[int, int]) -> np.ndarray:
    """The ``box`` area of ``values`` at ``size``: cut losslessly when that is whole pixels at scale 1, otherwise resampled."""
    height, width = values.shape[:2]
    left, top, right, bottom = box if box is not None else (0.0, 0.0, float(width), float(height))
    if all(value.is_integer() for value in (left, top, right, bottom)) and (right - left, bottom - top) == size:
        return values[int(top) : int(bottom), int(left) : int(right)]
    return resample(values, size, (left, top, right, bottom))


def resample(values: np.ndarray, size: tuple[int, int], box: Box | None = None) -> np.ndarray:
    """Resample the ``box`` area of floating-point sRGB values (H, W, 3) to ``size`` with Lanczos.

    Downscaling works in linear light, so fine bright detail keeps its brightness (in sRGB values,
    alternating white and black lines would average a third too dark), with partial anti-ringing.
    Upscaling works in sRGB values, which keeps edges sharper and rings less than linear light.
    The caller rounds the result once.
    """
    height, width = values.shape[:2]
    left, top, right, bottom = box if box is not None else (0.0, 0.0, float(width), float(height))
    # Source pixels per output pixel; the larger axis decides, as both axes scale almost alike.
    factor = max((right - left) / size[0], (bottom - top) / size[1])
    downscaling = factor > 1
    # Output pixel centers in source pixels, for the anti-ringing range.
    rows = _centers(top, bottom, size[1], height)
    columns = _centers(left, right, size[0], width)
    channels = []
    # One channel at a time keeps memory near one float copy of the source.
    for index in range(3):
        channel = np.ascontiguousarray(values[..., index], dtype=np.float32)
        if downscaling:
            channel = oklab.srgb_to_linear(channel, np.float32)
        resampled = np.array(Image.fromarray(channel, "F").resize(size, Image.Resampling.LANCZOS, box=(left, top, right, bottom)), dtype=np.float32)
        if downscaling:
            radius = math.ceil(factor)
            low = _window_at(channel, rows, columns, radius, np.minimum)
            high = _window_at(channel, rows, columns, radius, np.maximum)
            resampled += ANTI_RINGING * (np.clip(resampled, low, high) - resampled)
            resampled = _linear_to_srgb(resampled)
        channels.append(np.clip(resampled, 0, 1))
        del channel
    return np.stack(channels, axis=-1)


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


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0, 1)
    return np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1 / 2.4) - 0.055).astype(np.float32)


class TemporaryInput:
    """Run 1's copy of the input in its own temporary directory, removed by ``cleanup``; ``report`` says how its
    values were made."""

    def __init__(self, source: Path, plan: ResizePlan, ffmpeg: str | None = None) -> None:
        self._directory = Path(tempfile.mkdtemp(prefix="draw-things-control-"))
        width, height = plan.target_size
        self.path = self._directory / f"{source.stem}-{width}x{height}.png"
        try:
            self.report = resize_image(source, plan, self.path, ffmpeg)
        except BaseException as error:
            self.cleanup()
            # ValueError too: Pillow raises it for a mode it cannot convert, such as LAB.
            if isinstance(error, IMAGE_ERRORS + (ValueError,)):
                raise ValueError(f"Could not resize input {source}: {error}") from error
            raise

    def cleanup(self) -> None:
        """Remove the temporary directory; safe to call more than once."""
        shutil.rmtree(self._directory, ignore_errors=True)
