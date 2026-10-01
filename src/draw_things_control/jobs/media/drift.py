"""The ``color_drift`` check of every video run, whether or not its job corrects (owner decision, Milestone 09).

Three comparisons, by statistics (``color_stats.py``), over the whole frame:

- the run's frame 0 against its input: how faithfully the model and the handoff carry color into a run; a nonzero
  mean here is a pipeline bias, not drift;
- the last frame against frame 0: the model's own drift within the run;
- the last frame against the first image: the chain's drift so far.

With a segmenter (Apple Vision, Milestone 09's increment E), each is also made for people, their skin, and the
background (``regions.py``), on the frames compared: the run's input, the first image, frame 0, every fourth frame, and
the last frame. A region is compared only where both sides have enough of it. The summary adds skin's hue, and the
facts keep every region's numbers.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from draw_things_control.jobs.definition import DRIFT_SECONDS
from draw_things_control.jobs.media import oklab
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.clip_frames import iter_frames, read_tool_png
from draw_things_control.jobs.media.color_stats import ColorStats, Drift, compare
from draw_things_control.jobs.media.regions import REGIONS, RegionFinder, Regions, Segmenter, region_stats
from draw_things_control.jobs.media.stream_color import StreamColor

# Thresholds (Milestone 09, as proposed; accepted without the A/B chains, owner decision, 2026-10-01): a drift beyond
# any of them warns.
LIGHTNESS_LIMIT = 3.0
RATIO_LIMIT = 0.10
HUE_LIMIT = 5.0
# Every fourth frame is measured for the curve over the run, with frame 0 and the last frame.
SAMPLE_EVERY = 4
# The comparisons, in the order the summary gives them, and their labels.
COMPARISONS = {"first_image_to_last": "since the first image", "frame_0_to_last": "within the run", "input_to_frame_0": "frame 0 from its input"}
# A frame's statistics: the whole frame's as "frame", and each region's that counts (regions.py).
RegionStats = dict[str, ColorStats]
# A frame's regions, given the frame and its Oklab values; None measures the whole frame only.
RegionsOf = Callable[[np.ndarray, np.ndarray], Regions | None]


def measure_regions(frame: np.ndarray, regions_of: RegionsOf | None = None) -> RegionStats:
    """The statistics of ``frame`` and, given ``regions_of``, of each of its regions; converted to Oklab once."""
    lab = oklab.srgb_to_oklab(frame.reshape(-1, 3))
    return region_stats(frame, regions_of(frame, lab) if regions_of is not None else None, lab)


@dataclass
class FrameSamples:
    """Statistics of frame 0, every fourth frame, and the last frame of a run, gathered while its frames are read; with
    ``regions_of``, of their regions too."""

    curve: list[tuple[int, RegionStats]] = field(default_factory=list)
    frames: int = 0
    regions_of: RegionsOf | None = None
    _last: tuple[np.ndarray, RegionsOf | None] | None = None

    def add(self, frame: np.ndarray, regions_of: RegionsOf | None = None) -> None:
        """Add the next frame; ``regions_of``, when given, finds this frame's regions instead of the samples' own."""
        index = self.frames
        finder = regions_of if regions_of is not None else self.regions_of
        if index % SAMPLE_EVERY == 0:
            self.curve.append((index, measure_regions(frame, finder)))
            self._last = None
        else:
            self._last = (frame, finder)
        self.frames += 1

    def finish(self) -> None:
        """Measure the last frame too, when it was not one of the sampled ones."""
        if self._last is not None:
            frame, finder = self._last
            self.curve.append((self.frames - 1, measure_regions(frame, finder)))
            self._last = None


def samples_from_stats(stats: list[RegionStats]) -> FrameSamples:
    """The samples the check reads, from statistics of every frame the correction's first pass already measured."""
    curve = [(index, frame) for index, frame in enumerate(stats) if index % SAMPLE_EVERY == 0]
    if stats and (len(stats) - 1) % SAMPLE_EVERY:
        curve.append((len(stats) - 1, stats[-1]))
    return FrameSamples(curve=curve, frames=len(stats))


def sample_frames(frames: Iterable[np.ndarray], regions_of: RegionsOf | None = None) -> FrameSamples:
    samples = FrameSamples(regions_of=regions_of)
    for frame in frames:
        samples.add(frame)
    samples.finish()
    return samples


def check_color_drift(video: Path, color: StreamColor, ffmpeg: str, size: tuple[int, int], *, run_input: Path | None, first_image: Path | None, notes: tuple[str, ...] = (), segmenter: Segmenter | None = None) -> MediaCheck:
    """Decode ``video`` and compare its frames with its input and the chain's first image; with ``segmenter``, region
    by region too."""
    finder = RegionFinder(segmenter)
    samples = sample_frames(iter_frames(video, color, ffmpeg, size, deadline=time.monotonic() + DRIFT_SECONDS), finder if finder.active else None)
    input_stats = measure_regions(read_tool_png(run_input), finder if finder.active else None) if run_input is not None else None
    first_stats = measure_regions(read_tool_png(first_image), finder if finder.active else None) if first_image is not None else None
    return drift_check(video.name, samples, input_stats, first_stats, (*notes, *finder.notes))


def compare_regions(reference: RegionStats, measured: RegionStats) -> tuple[Drift, dict[str, Drift]]:
    """The whole frame's drift, and each region's that both sides have."""
    return compare(reference["frame"], measured["frame"]), {region: compare(reference[region], measured[region]) for region in REGIONS if region in reference and region in measured}


def drift_text(drift: Drift, regions: dict[str, Drift]) -> str:
    """For example ``L -2.1, contrast x1.06, chroma x1.08, hue +3°, skin hue +2°``."""
    text = drift.text()
    skin = regions.get("skin")
    if skin is not None and skin.hue is not None:
        text += f", skin hue {round(skin.hue) + 0:+d}°"
    return text


def drift_check(file: str, samples: FrameSamples, input_stats: RegionStats | None, first_stats: RegionStats | None, notes: tuple[str, ...] = ()) -> MediaCheck:
    """The check from statistics already gathered; the correction gathers them in its own pass over the frames."""
    if not samples.curve:
        raise ValueError("it has no frame")
    frame_0, last = samples.curve[0][1], samples.curve[-1][1]
    comparisons: dict[str, tuple[Drift, dict[str, Drift]]] = {}
    if first_stats is not None:
        comparisons["first_image_to_last"] = compare_regions(first_stats, last)
    comparisons["frame_0_to_last"] = compare_regions(frame_0, last)
    if input_stats is not None:
        comparisons["input_to_frame_0"] = compare_regions(input_stats, frame_0)
    warnings = [f"{COMPARISONS[name].capitalize()}, it drifted beyond the limits: {drift_text(*drifts)}." for name, drifts in comparisons.items() if beyond_limits(*drifts)]
    summary = "; ".join(f"{COMPARISONS[name]}: {drift_text(*drifts)}" for name, drifts in comparisons.items())
    facts: dict[str, Any] = {
        "frames": samples.frames,
        "regions": ["frame", *(region for region in REGIONS if any(region in drifts[1] for drifts in comparisons.values()))],
        "limits": {"lightness": LIGHTNESS_LIMIT, "ratio": RATIO_LIMIT, "hue_degrees": HUE_LIMIT},
        "comparisons": {name: drift.as_dict() for name, (drift, _regions) in comparisons.items()},
        "region_comparisons": {name: {region: drift.as_dict() for region, drift in regions.items()} for name, (_drift, regions) in comparisons.items() if regions},
        "input": None if input_stats is None else input_stats["frame"].as_dict(),
        "first_image": None if first_stats is None else first_stats["frame"].as_dict(),
        "frame_0": frame_0["frame"].as_dict(),
        "last": last["frame"].as_dict(),
        "region_stats": {name: {region: stats.as_dict() for region, stats in measured.items() if region != "frame"} for name, measured in (("input", input_stats), ("first_image", first_stats), ("frame_0", frame_0), ("last", last)) if measured is not None and len(measured) > 1},
        "curve": [{"frame": index, "lightness": round(stats["frame"].median, 4), "spread": round(stats["frame"].spread, 4), "chroma": round(stats["frame"].chroma, 4), "hue_degrees": None if stats["frame"].hue is None else round(float(np.degrees(stats["frame"].hue)), 2)} for index, stats in samples.curve],
    }
    return MediaCheck("color_drift", file, summary, tuple(warnings), notes, facts)


def beyond_limits(drift: Drift, regions: dict[str, Drift] | None = None) -> bool:
    """Whether the whole frame drifted beyond a limit, or skin's hue did."""
    skin = (regions or {}).get("skin")
    if skin is not None and skin.hue is not None and abs(skin.hue) > HUE_LIMIT:
        return True
    return abs(drift.lightness) > LIGHTNESS_LIMIT or abs(drift.contrast - 1) > RATIO_LIMIT or abs(drift.chroma - 1) > RATIO_LIMIT or (drift.hue is not None and abs(drift.hue) > HUE_LIMIT)
