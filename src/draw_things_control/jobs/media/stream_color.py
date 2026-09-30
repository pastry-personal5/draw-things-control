"""The color a video is decoded and tagged with: what its pixels measure, else what its stream states.

The rule (owner decision, 2026-09-30, superseding "honor the header" the same day): when the pixels tell which
matrix encoded them (``fingerprint.py``, for 4:4:4 at 10 bits or more, such as ProRes 4444), that matrix is used;
otherwise the one the stream states; BT.709 limited range when it states none, which is how Draw Things encodes its
untagged H.264 (Phase 2's measurement). Draw Things' ProRes states BT.601 for smaller frames (832x448, 448x576) and
BT.709 for larger ones, while its pixels are always BT.709, so the header alone cannot be trusted. The stream is read
from its first frame, before the tagger runs: ffmpeg decodes with the frame's color, and once a ``colr`` box is
added, ffprobe's stream-level value reports the box instead.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from draw_things_control.jobs.media.fingerprint import SAME_MATRIX, MatrixFingerprint, can_measure, measure_matrix

UNSTATED = {None, "", "unknown", "unspecified", "reserved", "N/A"}
# ffprobe's name of each matrix this can decode and tag: the scale filter's name for it, and its ITU-T H.273 code. The
# scale filter names BT.601 ``bt470`` (newer ffmpeg versions also read ``bt470bg``) and BT.2020 ``bt2020``, which every ffmpeg reads.
MATRICES = {"bt709": ("bt709", 1), "fcc": ("fcc", 4), "bt470bg": ("bt470", 5), "smpte170m": ("smpte170m", 6), "smpte240m": ("smpte240m", 7), "bt2020nc": ("bt2020", 9)}
DEFAULT_MATRIX = "bt709"
RANGES = {"tv": "limited range", "pc": "full range"}
TIMEOUT_SECONDS = 30

CommandRunner = Callable[..., Any]


@dataclass(frozen=True)
class StreamColor:
    """The matrix and range to decode and tag with, as ffprobe names them; None when nothing says."""

    matrix: str | None = None
    range: str | None = None
    # True when the pixels measured ``matrix`` over what the stream states, which is then ``stated`` (None: nothing).
    measured: bool = False
    stated: str | None = None
    # The measurement that chose the matrix, when the pixels were measured, so the output check need not decode again.
    fingerprint: MatrixFingerprint | None = field(default=None, compare=False, repr=False)

    @property
    def decode_matrix(self) -> str | None:
        """The scale filter's name for the matrix to decode with; None for a stated matrix this does not know."""
        known = MATRICES.get(self.matrix or DEFAULT_MATRIX)
        return known[0] if known is not None else None

    @property
    def decode_range(self) -> str:
        return self.range if self.range in RANGES else "tv"

    @property
    def matrix_code(self) -> int | None:
        """The H.273 code the ``colr`` box states; None for a stated matrix this does not know."""
        known = MATRICES.get(self.matrix or DEFAULT_MATRIX)
        return known[1] if known is not None else None

    def text(self) -> str:
        """For example ``bt709, limited range, as its stream states``, ``bt709, limited range: its stream states no
        matrix``, or ``bt709, limited range, measured from its pixels (its stream states smpte170m)``."""
        described = f"{self.matrix or DEFAULT_MATRIX}, {RANGES[self.decode_range]}"
        if self.measured:
            return f"{described}, measured from its pixels (its stream states {self.stated or 'no matrix'})"
        return f"{described}, as its stream states" if self.matrix is not None else f"{described}: its stream states no matrix"


def read_stream_color(video: Path, ffprobe: str | None, run: CommandRunner | None = None) -> StreamColor:
    """What the first frame of the first video stream states; nothing stated when ffprobe is missing or cannot tell."""
    return _probe(video, ffprobe, run)[0]


def resolve_video_color(video: Path, ffmpeg: str | None, ffprobe: str | None, run: CommandRunner | None = None) -> StreamColor:
    """The color to decode and tag ``video`` with: the matrix its pixels measure when they tell, else what it states."""
    stated, width, height, pix_fmt = _probe(video, ffprobe, run)
    if ffmpeg is None or not can_measure(pix_fmt) or width <= 0 or height <= 0:
        return stated
    try:
        fingerprint = measure_matrix(video, ffmpeg, width, height, full_range=stated.decode_range == "pc", run=run or subprocess.run)
    except ValueError:
        return stated
    measured = fingerprint.matrix
    if measured is None or SAME_MATRIX.get(stated.matrix or DEFAULT_MATRIX) == measured:
        return replace(stated, fingerprint=fingerprint)
    return replace(stated, matrix=measured, measured=True, stated=stated.matrix, fingerprint=fingerprint)


def _probe(video: Path, ffprobe: str | None, run: CommandRunner | None) -> tuple[StreamColor, int, int, str | None]:
    if ffprobe is None:
        return StreamColor(), 0, 0, None
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_entries", "stream=pix_fmt,width,height:frame=color_space,color_range", "-of", "json", str(video)]
    try:
        result = (run or subprocess.run)(command, capture_output=True, text=True, errors="replace", check=False, timeout=TIMEOUT_SECONDS)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        frame = (data.get("frames") or [{}])[0]
        stream = (data.get("streams") or [{}])[0]
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return StreamColor(), 0, 0, None
    matrix, color_range, pix_fmt = frame.get("color_space"), frame.get("color_range"), stream.get("pix_fmt")
    color = StreamColor(None if matrix in UNSTATED else str(matrix), None if color_range in UNSTATED else str(color_range))
    return color, int(stream.get("width") or 0), int(stream.get("height") or 0), pix_fmt
