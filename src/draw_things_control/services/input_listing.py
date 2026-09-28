"""Images available as a job's ``input`` (Milestone 02's ``GET /inputs``): every regular file under the input
directory whose header Pillow reads as an image, by the relative path a job's ``input`` takes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from draw_things_control.jobs.inputs.size import read_image_info


@dataclass(frozen=True)
class InputImage:
    """One image under the input directory: its path relative to it (as a job's ``input`` takes it), size, measured
    width and height, and when it was last modified (an epoch)."""

    path: str
    bytes: int
    width: int
    height: int
    modified: float


def list_inputs(input_directory: Path) -> list[InputImage]:
    """Every image in ``input_directory``, sub-directories included, by the relative path a job's ``input`` takes.
    A symbolic link, file or directory, is never followed, so a link cannot list or reach outside the directory. A
    regular file Pillow cannot read as an image is skipped, not reported as an error. Sorted by path."""
    if not input_directory.is_dir():
        return []
    images: list[InputImage] = []
    for root, directories, files in os.walk(input_directory, followlinks=False):
        # Never descend into a symbolic link to a directory.
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            image = _read(path, input_directory)
            if image is not None:
                images.append(image)
    images.sort(key=lambda image: image.path)
    return images


def _read(path: Path, input_directory: Path) -> InputImage | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        (width, height), _orientation = read_image_info(path)
    except ValueError:
        return None
    try:
        status = path.stat()
    except OSError:
        return None
    return InputImage(path=str(path.relative_to(input_directory)), bytes=status.st_size, width=width, height=height, modified=status.st_mtime)
