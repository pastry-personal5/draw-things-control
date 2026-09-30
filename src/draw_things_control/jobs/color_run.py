"""Correct one run's colors (Milestone 09): two passes over its frames, the corrected copy, the corrected handoff, and
the checks that say what was done.

The first pass measures every frame (``color_stats.py``), which also gives the run's ``color_drift`` check; the
parameters are then fitted, capped, and smoothed (``correction.py``); the second pass applies them and pipes each
frame to the encoder of the copy, ``<clip>-cc.<ext>``, in the original's format. The handoff is the corrected last
frame, from the floating-point result, not decoded again from the copy, written as Milestone 08's handoff is, less the
half level its values already carry. A file Draw Things wrote is never re-encoded, and none of its pixels change.

A correction that fails (an encoder or ffmpeg missing or failing, its time limit reached) returns a warning, and
deletes the copy and the handoff it may have begun; the run still succeeds, handing off the uncorrected frame (owner
decision). Vision's regions come later (increment E): until then, the whole frame is one region, with a note.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from draw_things_control.jobs.definition import ASSUMED_FRAME_COUNT, ColorPolicy, correction_limit
from draw_things_control.jobs.media.checks import MediaCheck, MediaChecker
from draw_things_control.jobs.media.clip_frames import ClipEncoder, available_encoders, choose_encoder, iter_frames, probe_clip, read_tool_png
from draw_things_control.jobs.media.color_stats import ColorStats, compare, measure
from draw_things_control.jobs.media.correction import Params, apply, plan_run, sample_pixels, transform_for
from draw_things_control.jobs.media.drift import COMPARISONS, FrameSamples, drift_check, samples_from_stats
from draw_things_control.jobs.media.frames import handoff_samples, write_handoff_png
from draw_things_control.jobs.media.stream_color import StreamColor
from draw_things_control.jobs.media.tools import find_ffprobe
from draw_things_control.jobs.media.video_color import tag_video_colors

# What the copy is tagged as: BT.709, limited range, as Milestone 08's tagger writes it.
COPY_COLOR = StreamColor("bt709", "tv")
REGIONS_NOTE = "Apple Vision's regions are not built yet, so the whole frame is corrected as one region."


@dataclass(frozen=True)
class CorrectionRequest:
    """One run to correct: its video and the color it is decoded with, its input, where the handoff and the copy go,
    and what its colors are held to."""

    video: Path
    color: StreamColor
    run_input: Path
    handoff: Path
    copy: Path
    policy: ColorPolicy
    anchor: Path | None = None
    reanchored: bool = False
    first_image: Path | None = None
    # Notes for the color_drift check, which the correction's first pass makes.
    drift_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionResult:
    """Whether the handoff and the copy were written, and the checks to report: the run's ``color_drift`` (None when
    the first pass did not finish, so the caller measures it itself), then the copy's ``output`` and the
    ``color_correction`` check."""

    succeeded: bool
    drift: MediaCheck | None
    checks: tuple[MediaCheck, ...]


Corrector = Callable[[CorrectionRequest], CorrectionResult]


class ColorCorrector:
    """Corrects runs with ffmpeg, found when each correction runs, as the checks find it."""

    def __init__(self, find_ffmpeg: Callable[[], str | None], checker: MediaChecker | None = None, *, tagger: Callable[[Path, StreamColor], bool] = tag_video_colors) -> None:
        self._find_ffmpeg = find_ffmpeg
        self._checker = checker
        self._tagger = tagger

    def __call__(self, request: CorrectionRequest) -> CorrectionResult:
        started = time.monotonic()
        drift: list[MediaCheck | None] = [None]
        created: list[Path] = []
        try:
            return self._correct(request, started, drift, created)
        # Whatever breaks the correction, the run goes on with its uncorrected frame (owner decision).
        except Exception as error:
            # Only what this correction began: the names were free when the run was planned.
            for path in created:
                path.unlink(missing_ok=True)
            failure = MediaCheck("color_correction", request.copy.name, "not corrected", warnings=(f"The correction failed ({error}), so the uncorrected last frame is the handoff and no copy is kept.",), facts={"policy": request.policy.as_dict(), "anchor": str(request.anchor) if request.anchor else None, "seconds": round(time.monotonic() - started, 1)})
            return CorrectionResult(False, drift[0], (failure,))

    def _correct(self, request: CorrectionRequest, started: float, drift: list[MediaCheck | None], created: list[Path]) -> CorrectionResult:
        ffmpeg = self._find_ffmpeg()
        ffprobe = find_ffprobe(ffmpeg)
        if ffmpeg is None or ffprobe is None:
            raise ValueError("ffmpeg or ffprobe was not found")
        info = probe_clip(request.video, ffprobe)
        # A clip whose frame count ffprobe does not report gets the frames the API's worst case assumes.
        limit = correction_limit(info.frames or ASSUMED_FRAME_COUNT)
        deadline = started + limit
        size = (info.width, info.height)
        input_stats = measure(read_tool_png(request.run_input))
        anchor_stats = measure(read_tool_png(request.anchor)) if request.anchor is not None and request.policy.holds_to_anchor else None
        first_stats = measure(read_tool_png(request.first_image)) if request.first_image is not None else None
        stats: list[ColorStats] = []
        samples: list[np.ndarray] = []
        for frame in iter_frames(request.video, request.color, ffmpeg, size, deadline=deadline):
            stats.append(measure(frame))
            samples.append(sample_pixels(frame))
        if not stats:
            raise ValueError(f"{request.video.name} has no frame")
        drift[0] = drift_check(request.video.name, samples_from_stats(stats), input_stats, first_stats, request.drift_notes)
        params = plan_run(stats, input_stats, anchor_stats, request.policy.pull, request.policy.strength, samples)
        del samples
        choice = choose_encoder(info, available_encoders(ffmpeg))
        encoder = ClipEncoder(ffmpeg, request.copy, info, choice)
        created.append(request.copy)
        corrected = FrameSamples()
        last: np.ndarray | None = None
        onto_edge = 0
        try:
            for index, frame in enumerate(iter_frames(request.video, request.color, ffmpeg, size, deadline=deadline)):
                if index >= len(stats):
                    raise ValueError(f"{request.video.name} decoded more frames the second time")
                out, moved = apply(frame, transform_for(stats[index], params[index]))
                onto_edge += moved
                encoder.write(out)
                corrected.add(out)
                last = out
            if encoder.frames != len(stats) or last is None:
                raise ValueError(f"{request.video.name} decoded {encoder.frames} frames the second time, {len(stats)} the first")
            encoder.close(deadline)
        except BaseException:
            encoder.abort()
            raise
        corrected.finish()
        self._tagger(request.copy, COPY_COLOR)
        created.append(request.handoff)
        write_handoff_png(handoff_samples(last.astype(np.float64) * 255), request.handoff)
        checks: list[MediaCheck] = []
        if self._checker is not None:
            written = self._checker.probe(request.copy)
            output, _probe = self._checker.output(request.copy, None, written, COPY_COLOR)
            checks.append(output)
        checks.append(correction_check(request, params, corrected, input_stats, first_stats, onto_edge / (len(params) * info.width * info.height), choice.encoder, round(time.monotonic() - started, 1), limit))
        return CorrectionResult(True, drift[0], tuple(checks))


def correction_check(request: CorrectionRequest, params: list[Params], corrected: FrameSamples, input_stats: ColorStats, first_stats: ColorStats | None, onto_edge: float, encoder: str, seconds: float, limit_seconds: float) -> MediaCheck:
    """The ``color_correction`` check: what was done, the caps that bound, and the drift left, measured on the
    corrected frames."""
    policy = request.policy
    frame_0, last = corrected.curve[0][1], corrected.curve[-1][1]
    left = {"frame_0_to_last": compare(frame_0, last), "input_to_frame_0": compare(input_stats, frame_0)}
    if first_stats is not None:
        left = {"first_image_to_last": compare(first_stats, last), **left}
    bound: dict[str, int] = {}
    for frame_params in params:
        for name in frame_params.bound:
            bound[name] = bound.get(name, 0) + 1
    notes = [f"The {name} cap bound in {count} of {len(params)} frames; a large difference is more likely a change of scene than drift." for name, count in sorted(bound.items())]
    if onto_edge >= 0.001:
        # Mostly pixels already on the edge (a channel at 0 or 255, common in generated video) that any turn or gain moves
        # a hair outside.
        notes.append(f"{onto_edge:.1%} of the pixels were pushed outside sRGB and brought onto its edge at constant lightness and hue.")
    if policy.regions:
        notes.append(REGIONS_NOTE)
    summary = f"{_what(request)}; left after correction: " + "; ".join(f"{COMPARISONS[name]}: {drift.text()}" for name, drift in left.items())
    facts: dict[str, Any] = {
        "policy": policy.as_dict(),
        "anchor": str(request.anchor) if request.anchor is not None else None,
        "reanchored": request.reanchored,
        "regions": ["frame"],
        "encoder": encoder,
        "frames": len(params),
        "seconds": seconds,
        "limit_seconds": limit_seconds,
        "caps_bound": bound,
        "onto_edge_share": round(onto_edge, 4),
        "parameters": [{"frame": index, **frame_params.as_dict()} for index, frame_params in enumerate(params) if index % 4 == 0 or index == len(params) - 1],
        "left": {name: drift.as_dict() for name, drift in left.items()},
    }
    return MediaCheck("color_correction", request.copy.name, summary, (), tuple(notes), facts)


def _what(request: CorrectionRequest) -> str:
    """For example ``blend 0.25 toward walk-job-first-image.png, re-anchored at this run's input``."""
    policy = request.policy
    if policy.anchor == "previous":
        text = "previous: each frame back to the run's input"
    else:
        weight = f" {policy.first_weight:g}" if policy.anchor == "blend" else ""
        target = request.anchor.name if request.anchor is not None else "no anchor, so back to the run's input"
        text = f"{policy.anchor}{weight} toward {target}"
        if request.reanchored:
            text += ", re-anchored at this run's input"
    if policy.strength != 1:
        text += f", strength {policy.strength:g}"
    return text
