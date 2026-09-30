"""Which YCbCr matrix a video's pixels were encoded with, measured from the pixels rather than read from a tag.

Draw Things gives its encoder 8-bit RGB. Converted to YCbCr at 10 or more bits with some matrix, such a frame comes
back near whole 8-bit levels only when it is converted back with that same matrix; with any other matrix the
fractions spread evenly, a mean distance of 0.25 from the nearest level. So the matrix that scores lowest is the one
used. This needs full chroma (4:4:4): subsampled chroma is interpolated, which leaves no 8-bit structure to find.
The test and its measurements are in docs/research/prores-color-matrix.md.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Kr and Kb of each candidate, under the names ffprobe gives them.
MATRICES = {"bt470bg": (0.299, 0.114), "bt709": (0.2126, 0.0722), "bt2020nc": (0.2627, 0.0593)}
# ffprobe's names that mean the same matrix as a candidate.
SAME_MATRIX = {"smpte170m": "bt470bg", "bt470bg": "bt470bg", "bt709": "bt709", "bt2020nc": "bt2020nc"}
# The first frames are enough: every frame of Draw Things' clips has scored within 0.001 of the others.
FRAMES = 5
# A score this low, at least MARGIN under the next, names the matrix. Measured: the right matrix scores 0.197 on
# ffmpeg-made controls and 0.216 to 0.217 on Draw Things' ProRes 4444, the wrong ones 0.229 to 0.265.
FOUND_BELOW = 0.235
MARGIN = 0.012
# Every matrix scoring at least this is what noise gives (the noise run scored 0.250 for all three).
NOISE_FROM = 0.245
TIMEOUT_SECONDS = 120

CommandRunner = Callable[..., Any]


@dataclass(frozen=True)
class MatrixFingerprint:
    """Each candidate matrix's score: the mean distance of the decoded RGB values from whole 8-bit levels."""

    scores: dict[str, float]

    @property
    def matrix(self) -> str | None:
        """The matrix the pixels were encoded with, or None when the scores do not tell."""
        ranked = sorted(self.scores.items(), key=lambda item: item[1])
        (best, score), (_, second) = ranked[0], ranked[1]
        return best if score < FOUND_BELOW and second - score >= MARGIN else None

    @property
    def looks_like_noise(self) -> bool:
        return min(self.scores.values()) >= NOISE_FROM

    def text(self) -> str:
        """For example ``bt709 0.216, bt470bg 0.254, bt2020nc 0.255``, lowest first."""
        return ", ".join(f"{name} {score:.3f}" for name, score in sorted(self.scores.items(), key=lambda item: item[1]))


def can_measure(pix_fmt: str | None) -> bool:
    """Whether a pixel format keeps the structure the test reads: 4:4:4 YCbCr at 10 bits or more."""
    if pix_fmt is None or not pix_fmt.startswith(("yuv444p", "yuva444p")):
        return False
    depth = re.search(r"444p(\d+)", pix_fmt)
    return depth is not None and int(depth.group(1)) >= 10


def measure_matrix(video: Path, ffmpeg: str, width: int, height: int, *, full_range: bool, run: CommandRunner = subprocess.run) -> MatrixFingerprint:
    """Score each candidate matrix on the video's first frames; raises ValueError when they cannot be decoded."""
    import numpy as np

    # To 12-bit 4:4:4 without alpha: only the depth and the planes change, never the matrix or the range.
    command = [ffmpeg, "-v", "error", "-nostdin", "-i", str(video), "-frames:v", str(FRAMES), "-f", "rawvideo", "-pix_fmt", "yuv444p12le", "-"]
    try:
        result = run(command, capture_output=True, check=False, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"could not decode its frames: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() if isinstance(result.stderr, bytes) else str(result.stderr or "").strip()
        raise ValueError(f"could not decode its frames: {detail or f'exit code {result.returncode}'}")
    samples = np.frombuffer(result.stdout, dtype="<u2")
    plane = width * height
    frames = samples.size // (3 * plane)
    if frames == 0:
        raise ValueError("ffmpeg decoded no frame")
    yuv = samples[: frames * 3 * plane].reshape(frames, 3, height, width).astype(np.float32)
    if full_range:
        luma, blue, red = yuv[:, 0] / 4095, (yuv[:, 1] - 2048) / 4095, (yuv[:, 2] - 2048) / 4095
    else:
        luma, blue, red = (yuv[:, 0] - 256) / 3504, (yuv[:, 1] - 2048) / 3584, (yuv[:, 2] - 2048) / 3584
    scores: dict[str, float] = {}
    for name, (kr, kb) in MATRICES.items():
        r = luma + 2 * (1 - kr) * red
        b = luma + 2 * (1 - kb) * blue
        g = (luma - kr * r - kb * b) / (1 - kr - kb)
        distance = 0.0
        for channel in (r, g, b):
            levels = channel * 255
            distance += float(np.abs(levels - np.rint(levels)).mean())
        scores[name] = distance / 3
    return MatrixFingerprint(scores)
