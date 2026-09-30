"""The ``color_drift`` check of every video run, whether or not its job corrects (owner decision, Milestone 09).

Three comparisons, by statistics (``color_stats.py``), over the whole frame:

- the run's frame 0 against its input: how faithfully the model and the handoff carry color into a run; a nonzero
  mean here is a pipeline bias, not drift;
- the last frame against frame 0: the model's own drift within the run;
- the last frame against the first image: the chain's drift so far.

People, skin, and the background are measured apart once Apple Vision's regions are built (Milestone 09's increment
E); until then only the whole frame is.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.clip_frames import iter_frames, read_tool_png
from draw_things_control.jobs.media.color_stats import ColorStats, Drift, compare, measure
from draw_things_control.jobs.media.stream_color import StreamColor

# Proposed thresholds (Milestone 09), to be set from the first measurements of the owner's A/B chains: a drift beyond
# any of them warns.
LIGHTNESS_LIMIT = 3.0
RATIO_LIMIT = 0.10
HUE_LIMIT = 5.0
# Every fourth frame is measured for the curve over the run, with frame 0 and the last frame.
SAMPLE_EVERY = 4
# How long decoding a run's frames may take, as every ffmpeg call of a check has a limit; an 81-frame 832x448 ProRes
# clip takes a few seconds.
TIMEOUT_SECONDS = 300
# The comparisons, in the order the summary gives them, and their labels.
COMPARISONS = {"first_image_to_last": "since the first image", "frame_0_to_last": "within the run", "input_to_frame_0": "frame 0 from its input"}


@dataclass
class FrameSamples:
    """Statistics of frame 0, every fourth frame, and the last frame of a run, gathered while its frames are read."""

    curve: list[tuple[int, ColorStats]] = field(default_factory=list)
    frames: int = 0
    _last: np.ndarray | None = None

    def add(self, frame: np.ndarray) -> None:
        index = self.frames
        if index % SAMPLE_EVERY == 0:
            self.curve.append((index, measure(frame)))
            self._last = None
        else:
            self._last = frame
        self.frames += 1

    def finish(self) -> None:
        """Measure the last frame too, when it was not one of the sampled ones."""
        if self._last is not None:
            self.curve.append((self.frames - 1, measure(self._last)))
            self._last = None


def samples_from_stats(stats: list[ColorStats]) -> FrameSamples:
    """The samples the check reads, from statistics of every frame the correction's first pass already measured."""
    curve = [(index, frame) for index, frame in enumerate(stats) if index % SAMPLE_EVERY == 0]
    if stats and (len(stats) - 1) % SAMPLE_EVERY:
        curve.append((len(stats) - 1, stats[-1]))
    return FrameSamples(curve=curve, frames=len(stats))


def sample_frames(frames: Iterable[np.ndarray]) -> FrameSamples:
    samples = FrameSamples()
    for frame in frames:
        samples.add(frame)
    samples.finish()
    return samples


def check_color_drift(video: Path, color: StreamColor, ffmpeg: str, size: tuple[int, int], *, run_input: Path | None, first_image: Path | None, notes: tuple[str, ...] = ()) -> MediaCheck:
    """Decode ``video`` and compare its frames with its input and the chain's first image."""
    samples = sample_frames(iter_frames(video, color, ffmpeg, size, deadline=time.monotonic() + TIMEOUT_SECONDS))
    input_stats = measure(read_tool_png(run_input)) if run_input is not None else None
    first_stats = measure(read_tool_png(first_image)) if first_image is not None else None
    return drift_check(video.name, samples, input_stats, first_stats, notes)


def drift_check(file: str, samples: FrameSamples, input_stats: ColorStats | None, first_stats: ColorStats | None, notes: tuple[str, ...] = ()) -> MediaCheck:
    """The check from statistics already gathered; the correction gathers them in its own pass over the frames."""
    if not samples.curve:
        raise ValueError("it has no frame")
    frame_0, last = samples.curve[0][1], samples.curve[-1][1]
    comparisons: dict[str, Drift] = {}
    if first_stats is not None:
        comparisons["first_image_to_last"] = compare(first_stats, last)
    comparisons["frame_0_to_last"] = compare(frame_0, last)
    if input_stats is not None:
        comparisons["input_to_frame_0"] = compare(input_stats, frame_0)
    warnings = [f"{COMPARISONS[name].capitalize()}, it drifted beyond the limits: {drift.text()}." for name, drift in comparisons.items() if beyond_limits(drift)]
    summary = "; ".join(f"{COMPARISONS[name]}: {drift.text()}" for name, drift in comparisons.items())
    facts: dict[str, Any] = {
        "frames": samples.frames,
        "regions": ["frame"],
        "limits": {"lightness": LIGHTNESS_LIMIT, "ratio": RATIO_LIMIT, "hue_degrees": HUE_LIMIT},
        "comparisons": {name: drift.as_dict() for name, drift in comparisons.items()},
        "input": None if input_stats is None else input_stats.as_dict(),
        "first_image": None if first_stats is None else first_stats.as_dict(),
        "frame_0": frame_0.as_dict(),
        "last": last.as_dict(),
        "curve": [{"frame": index, "lightness": round(stats.median, 4), "spread": round(stats.spread, 4), "chroma": round(stats.chroma, 4), "hue_degrees": None if stats.hue is None else round(float(np.degrees(stats.hue)), 2)} for index, stats in samples.curve],
    }
    return MediaCheck("color_drift", file, summary, tuple(warnings), notes, facts)


def beyond_limits(drift: Drift) -> bool:
    return abs(drift.lightness) > LIGHTNESS_LIMIT or abs(drift.contrast - 1) > RATIO_LIMIT or abs(drift.chroma - 1) > RATIO_LIMIT or (drift.hue is not None and abs(drift.hue) > HUE_LIMIT)
