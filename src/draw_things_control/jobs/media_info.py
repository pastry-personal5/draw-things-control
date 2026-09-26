"""Measure the size and frame count of the file a run actually wrote, which may differ from what was requested.

Draw Things may round a requested size to a multiple of 64, or crop it, and a video model may change the frame count,
so history records what the file holds. Videos are read with ffprobe; PNGs from their own header.
"""

from __future__ import annotations

import json
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from draw_things_control.core.numbers import positive_whole
from draw_things_control.jobs.frame_extraction import find_ffprobe

VIDEO_SUFFIXES = {".mov", ".mp4"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FFPROBE_TIMEOUT_SECONDS = 10

# Runs a command and returns what subprocess.run returns; tests replace it so ffprobe never runs.
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class MediaInfo:
    """What an output file holds: its displayed width and height, and its frame count (None for an image)."""

    width: int | None
    height: int | None
    frames: int | None = None


def measure_output(path: Path, *, ffprobe: str | None = None, run: CommandRunner = subprocess.run) -> MediaInfo:
    """The file's size and frame count; raises ValueError when it cannot be measured."""
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return _measure_video(path, ffprobe or find_ffprobe(), run)
    if path.suffix.lower() == ".png":
        return _measure_png(path)
    raise ValueError(f"Cannot measure {path.name}: not a PNG, MOV, or MP4 file")


def _measure_png(path: Path) -> MediaInfo:
    # The signature, then the IHDR chunk: its length, its type, and the width and height as big-endian 32-bit numbers.
    try:
        with path.open("rb") as file:
            header = file.read(24)
    except OSError as error:
        raise ValueError(f"Cannot read {path}: {error.strerror}") from error
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG image")
    width, height = struct.unpack(">II", header[16:24])
    return MediaInfo(width, height)


def _measure_video(path: Path, ffprobe: str | None, run: CommandRunner) -> MediaInfo:
    if ffprobe is None:
        raise ValueError(f"Cannot measure {path.name}: ffprobe was not found")
    # width and height are the displayed size, after any crop the stream carries, not the padded coded size.
    stream = _probe(ffprobe, path, ["-show_entries", "stream=width,height,nb_frames"], run)
    frames = positive_whole(stream.get("nb_frames"))
    if frames is None:
        # The container does not say; counting packets reads them without decoding, so it stays fast.
        frames = positive_whole(_probe(ffprobe, path, ["-count_packets", "-show_entries", "stream=nb_read_packets"], run).get("nb_read_packets"))
    return MediaInfo(positive_whole(stream.get("width")), positive_whole(stream.get("height")), frames)


def _probe(ffprobe: str, path: Path, entries: list[str], run: CommandRunner) -> dict[str, Any]:
    """The first video stream's entries; the path is an argument, never shell text."""
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", *entries, "-of", "json", str(path)]
    try:
        result = run(command, capture_output=True, text=True, errors="replace", check=False, timeout=FFPROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Cannot measure {path.name} with ffprobe: {error}") from error
    if result.returncode != 0:
        raise ValueError(f"Cannot measure {path.name}: ffprobe exited with {result.returncode}{': ' + result.stderr.strip() if result.stderr.strip() else ''}")
    try:
        streams = json.loads(result.stdout).get("streams") or []
    except (ValueError, AttributeError) as error:
        raise ValueError(f"Cannot measure {path.name}: ffprobe printed no JSON") from error
    if not streams or not isinstance(streams[0], dict):
        raise ValueError(f"Cannot measure {path.name}: it has no video stream")
    return streams[0]
