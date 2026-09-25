"""Build unique, timestamped output file names for the runs of a job."""

from __future__ import annotations

import random
from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path

Clock = Callable[[], datetime]
RandomNumber = Callable[[], int]
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
MAX_ATTEMPTS = 100


def random_four_digits() -> int:
    """Return a random number with exactly four digits."""
    return random.randint(1000, 9999)


def output_name(base: str, extension: str, now: datetime, number: int) -> str:
    """Join the base, timestamp, and number: ``<base>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>``."""
    return f"{base}-{now.strftime(TIMESTAMP_FORMAT)}-{number}.{extension}"


def last_frame_path(output: Path) -> Path:
    """Return the last-frame PNG path that belongs to a video output."""
    return output.with_name(f"{output.stem}-last-frame.png")


def next_output_path(directory: Path, base: str, extension: str, clock: Clock, random_number: RandomNumber, reserved: Collection[Path] = ()) -> Path:
    """Name a run's output, drawing new numbers while the output or its last frame exists."""
    for _ in range(MAX_ATTEMPTS):
        candidate = directory / output_name(base, extension, clock(), random_number())
        if not _taken(candidate, reserved) and not _taken(last_frame_path(candidate), reserved):
            return candidate
    raise ValueError(f"Could not find an unused output name in {directory}")


def job_file_stem(directory: Path, base: str, clock: Clock, random_number: RandomNumber) -> str:
    """Name the job manifest and log: ``<base>-<timestamp>-job``, adding a number if taken."""
    timestamp = clock().strftime(TIMESTAMP_FORMAT)
    stem = f"{base}-{timestamp}-job"
    for _ in range(MAX_ATTEMPTS):
        if not (directory / f"{stem}.json").exists() and not (directory / f"{stem}.log").exists():
            return stem
        stem = f"{base}-{timestamp}-{random_number()}-job"
    raise ValueError(f"Could not find an unused manifest name in {directory}")


def _taken(path: Path, reserved: Collection[Path]) -> bool:
    return path in reserved or path.exists()
