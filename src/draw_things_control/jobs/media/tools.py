"""Finding ffmpeg and ffprobe, which video jobs need."""

from __future__ import annotations

import shutil
from pathlib import Path


def find_ffmpeg() -> str | None:
    """ffmpeg's path, or None; for what can do without it, such as reading a 16-bit input by its high bytes."""
    return shutil.which("ffmpeg")


def require_ffmpeg(executable: str = "ffmpeg") -> str:
    """Return ffmpeg's path, or explain how to get it."""
    path = shutil.which(executable)
    if path is None:
        raise ValueError(f"Could not find '{executable}' on PATH; video jobs need it to extract last frames (for example: brew install ffmpeg)")
    return path


def find_ffprobe(ffmpeg: str | None = None) -> str | None:
    """ffprobe beside ``ffmpeg`` (the pair installed together; without one, the ffmpeg on PATH), or else on PATH; None when
    there is neither. The preflight and the measuring both look here, so they find the same ffprobe."""
    if ffmpeg is None:
        ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None and Path(ffmpeg).with_name("ffprobe").exists():
        return str(Path(ffmpeg).with_name("ffprobe"))
    return shutil.which("ffprobe")


def require_ffprobe(ffmpeg: str | None = None) -> str:
    """Return ffprobe's path, or explain how to get it; video jobs need it to measure their outputs."""
    path = find_ffprobe(ffmpeg)
    if path is None:
        raise ValueError("Could not find 'ffprobe' beside ffmpeg or on PATH; video jobs need it to measure their outputs (it comes with ffmpeg, for example: brew install ffmpeg)")
    return path
