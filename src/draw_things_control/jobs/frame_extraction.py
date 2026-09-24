"""Extract the last frame of a generated video with ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def require_ffmpeg(executable: str = "ffmpeg") -> str:
    """Return ffmpeg's path, or explain how to get it."""
    path = shutil.which(executable)
    if path is None:
        raise ValueError(f"Could not find '{executable}' on PATH; video jobs need it to extract last frames (for example: brew install ffmpeg)")
    return path


def extract_last_frame(video: Path, png: Path, executable: str = "ffmpeg") -> None:
    """Write the final frame of ``video`` to ``png``, never overwriting a file."""
    ffmpeg = require_ffmpeg(executable)
    # Seek near the end and keep overwriting one image; the last frame decoded wins.
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-q:v", "1", str(png)]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    if result.returncode != 0 or not png.is_file():
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise ValueError(f"Could not extract the last frame of {video}: {detail}")
