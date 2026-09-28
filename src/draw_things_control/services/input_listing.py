"""Images available as a job's ``input`` (Milestone 02's ``GET /inputs``): every regular file under the input
directory whose header Pillow reads as an image, by the relative path a job's ``input`` takes."""

from __future__ import annotations

import os
import threading
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


# A file's modification time and size: unlike JobCatalog's own Signature (services/job_catalog.py), not `None` for a
# missing file, since a signature is only ever looked up for a file list_inputs' own os.walk just found.
Signature = tuple[int, int]
Cache = dict[Path, tuple[Signature, InputImage]]


def list_inputs(input_directory: Path, *, cache: Cache | None = None) -> list[InputImage]:
    """Every image in ``input_directory``, sub-directories included, by the relative path a job's ``input`` takes.
    A symbolic link, file or directory, is never followed, so a link cannot list or reach outside the directory. A
    regular file Pillow cannot read as an image is skipped, not reported as an error. Sorted by path.

    ``cache``, when given, is mutated in place and read again next call (``InputCatalog``'s own instance, held for a
    server process's whole lifetime): a file already in it is read again only when its modification time or size
    changed, the same idea ``JobCatalog``'s own file cache uses (``services/job_catalog.py``). None, the default and
    every other caller's own choice, always re-reads every file, as before this parameter existed."""
    if not input_directory.is_dir():
        if cache is not None:
            cache.clear()
        return []
    seen: set[Path] = set()
    images: list[InputImage] = []
    for root, directories, files in os.walk(input_directory, followlinks=False):
        # Never descend into a symbolic link to a directory.
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            seen.add(path)
            image = _read_cached(path, input_directory, cache)
            if image is not None:
                images.append(image)
    if cache is not None:
        for stale in set(cache) - seen:
            del cache[stale]
    images.sort(key=lambda image: image.path)
    return images


def _read_cached(path: Path, input_directory: Path, cache: Cache | None) -> InputImage | None:
    if cache is None:
        return _read(path, input_directory)
    signature = _signature(path)
    if signature is None:
        cache.pop(path, None)
        return None
    cached = cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    image = _read(path, input_directory)
    if image is None:
        cache.pop(path, None)
        return None
    cache[path] = (signature, image)
    return image


def _signature(path: Path) -> Signature | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


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


class InputCatalog:
    """A cached ``list_inputs``, held for a server process's whole lifetime (``ServerContext``): without it, each
    ``GET /inputs`` page re-read every image's header, including the ones a later page's own slice would discard.
    Thread-safe: a route runs on FastAPI's own worker thread pool."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._cache: Cache = {}

    def list(self) -> list[InputImage]:
        with self._lock:
            return list_inputs(self._directory, cache=self._cache)
