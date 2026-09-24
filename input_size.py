"""Read input image sizes and check them against a job's size."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

EXIF_ORIENTATION = 0x0112
ROTATED_ORIENTATIONS = {5, 6, 7, 8}


def read_image_size(path: Path) -> tuple[int, int]:
    """Return the displayed width and height, reading only the file header."""
    try:
        with Image.open(path) as image:
            width, height = image.size
            orientation = image.getexif().get(EXIF_ORIENTATION)
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError(f"'input' is not a readable image: {path} ({error})") from error
    if orientation in ROTATED_ORIENTATIONS:
        return height, width
    return width, height


def check_input_size(path: Path, image_size: tuple[int, int], job_size: tuple[int, int], source: str) -> None:
    """Refuse a job whose first input image does not match its width and height."""
    if image_size == job_size:
        return
    image_width, image_height = image_size
    job_width, job_height = job_size
    raise ValueError(f"Input image {path} is {image_width}x{image_height}, but the job size is {job_width}x{job_height} ({source}). " f"Resize the image to {job_width}x{job_height}, or set config_override.width and config_override.height to {image_width} and {image_height}. " "Automatic resizing is planned for a later milestone.")
