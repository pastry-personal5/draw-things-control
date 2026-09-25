"""Extract the last frame of a generated video with ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Draw Things' videos carry no color tags (the current CLI writes untagged H.264; older ones wrote ProRes tagged
# smpte170m), so ffmpeg's PNG has no color chunks and viewers guess. The frame is converted to RGB first, at the
# depth the source needs (the format list only stops ffmpeg from choosing an unrelated depth; it picks the closest),
# and only then labeled sRGB, which is what Draw Things' models produce; labeling before the conversion would change
# the pixels. ffmpeg then writes the sRGB, cHRM, and gAMA chunks. The label does not change any pixel value.
SRGB_FORMATS = "format=pix_fmts=rgb24|rgba|rgb48be|rgba64be"
SRGB_LABEL = "setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc"
# For an untagged video: ffmpeg would assume BT.601, but Draw Things encodes BT.709 with limited range. Measured on
# its H.264 output against the input image of an i2v run, BT.601 decoding leaves a green cast on saturated colors
# (mean error 3.35 against 2.56 for BT.709 on the most saturated pixels), and that cast would compound along a chain.
UNTAGGED_DECODE = "scale=in_color_matrix=bt709:in_range=tv:out_range=pc"
UNSPECIFIED = {"", "unknown", "unspecified", "reserved", "N/A"}


def srgb_filter(matrix_known: bool) -> str:
    """The ffmpeg filter chain that converts to RGB (as BT.709 limited range when the video has no matrix) and labels sRGB."""
    return ",".join(part for part in (None if matrix_known else UNTAGGED_DECODE, SRGB_FORMATS, SRGB_LABEL) if part)


def require_ffmpeg(executable: str = "ffmpeg") -> str:
    """Return ffmpeg's path, or explain how to get it."""
    path = shutil.which(executable)
    if path is None:
        raise ValueError(f"Could not find '{executable}' on PATH; video jobs need it to extract last frames (for example: brew install ffmpeg)")
    return path


def has_matrix_tag(video: Path, ffmpeg: str) -> bool:
    """Whether the video's first stream says which YCbCr matrix it uses; when ffprobe cannot tell, assume it does not."""
    ffprobe = str(Path(ffmpeg).with_name("ffprobe")) if Path(ffmpeg).with_name("ffprobe").exists() else shutil.which("ffprobe")
    if ffprobe is None:
        return False
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_space", "-of", "default=nw=1:nk=1", str(video)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() not in UNSPECIFIED


def extract_last_frame(video: Path, png: Path, executable: str = "ffmpeg") -> None:
    """Write the final frame of ``video`` to ``png``, labeled sRGB, never overwriting a file."""
    ffmpeg = require_ffmpeg(executable)
    # Seek near the end and keep overwriting one image; the last frame decoded wins.
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-q:v", "1", "-vf", srgb_filter(has_matrix_tag(video, ffmpeg)), str(png)]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    if result.returncode != 0 or not png.is_file():
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise ValueError(f"Could not extract the last frame of {video}: {detail}")
