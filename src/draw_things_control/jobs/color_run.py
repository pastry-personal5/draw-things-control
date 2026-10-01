"""Correct one run's colors (Milestone 09): two passes over its frames, the corrected copy, the corrected handoff, and
the checks that say what was done.

The first pass measures every frame (``color_stats.py``), which also gives the run's ``color_drift`` check; the
parameters are then fitted, capped, and smoothed (``correction.py``); the second pass applies them and pipes each
frame to the encoder of the copy, ``<clip>-cc.<ext>``, in the original's format. The handoff is the corrected last
frame, from the floating-point result, not decoded again from the copy, written as Milestone 08's handoff is, less the
half level its values already carry. A file Draw Things wrote is never re-encoded, and none of its pixels change.

With a segmenter (Apple Vision, increment E), the first pass also finds each frame's regions (``regions.py``) and
keeps their masks; with ``color.regions``, people, their skin, and the background are then corrected apart (the
background by its own transform, owner decision, 2026-10-01), and the second pass blends the transforms through the
masks. A region is corrected apart only when the run's input,
its anchor, and half of its frames have enough of it; otherwise its parent's transform applies, with a note.

A correction that fails (an encoder or ffmpeg missing or failing, its time limit reached) returns a warning, and
deletes the copy and the handoff it may have begun; the run still succeeds, handing off the uncorrected frame (owner
decision). A segmenter that is missing or fails is not a failure: the whole frame is then one region, with a note.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from draw_things_control.jobs.definition import ASSUMED_FRAME_COUNT, ColorPolicy, correction_limit
from draw_things_control.jobs.media import oklab
from draw_things_control.jobs.media.checks import MediaCheck, MediaChecker
from draw_things_control.jobs.media.clip_frames import ClipEncoder, EncoderChoice, available_encoders, choose_encoder, encoder_works, iter_frames, probe_clip, read_tool_png
from draw_things_control.jobs.media.correction import Params, Transform, apply_regions, finish_run, fit_run, sample_pixels, scaled, skin_residuals, smooth, transform_for
from draw_things_control.jobs.media.drift import COMPARISONS, FrameSamples, RegionsOf, RegionStats, compare_regions, drift_check, drift_text, measure_regions, samples_from_stats
from draw_things_control.jobs.media.frames import handoff_samples, write_handoff_png
from draw_things_control.jobs.media.regions import FRAMES_SHARE, FrameMasks, RegionFinder, Segmenter, blend_weights, make_segmenter, region_stats, regions_from_masks
from draw_things_control.jobs.media.stream_color import StreamColor
from draw_things_control.jobs.media.tools import find_ffprobe
from draw_things_control.jobs.media.video_color import tag_video_colors

# What the copy is tagged as: BT.709, limited range, as Milestone 08's tagger writes it.
COPY_COLOR = StreamColor("bt709", "tv")
NO_VISION_NOTE = "Apple Vision is not available here, so the whole frame is corrected as one region."
# Pixels of a region kept from the first pass, at most, to refine people's chroma gain and to fit skin's residual.
REGION_SAMPLE = 4096


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
    """Corrects runs with ffmpeg, found when each correction runs, as the checks find it; and with the segmenter
    ``segmenter`` gives, when it gives one."""

    def __init__(self, find_ffmpeg: Callable[[], str | None], checker: MediaChecker | None = None, *, tagger: Callable[[Path, StreamColor], bool] = tag_video_colors, segmenter: Callable[[], Segmenter | None] | None = None) -> None:
        self._find_ffmpeg = find_ffmpeg
        self._checker = checker
        self._tagger = tagger
        self._segmenter = segmenter

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
        finder = RegionFinder(make_segmenter(self._segmenter))
        had_segmenter = finder.active
        input_stats = measure_regions(read_tool_png(request.run_input), finder)
        anchor_stats = measure_regions(read_tool_png(request.anchor), finder) if request.anchor is not None and request.policy.holds_to_anchor else None
        first_stats = measure_regions(read_tool_png(request.first_image), finder) if request.first_image is not None else None
        first = _FirstPass()
        for frame in iter_frames(request.video, request.color, ffmpeg, size, deadline=deadline):
            first.add(frame, finder)
        if not first.stats:
            raise ValueError(f"{request.video.name} has no frame")
        drift[0] = drift_check(request.video.name, samples_from_stats(first.stats), input_stats, first_stats, (*request.drift_notes, *finder.notes))
        # A Vision that failed during the run leaves the whole run to the whole frame, so no clip switches mid-way.
        plan = _plan(first, input_stats, anchor_stats, request.policy, had_segmenter, failed=bool(finder.notes))
        plan.notes.extend(finder.notes)
        del first.samples, first.people_samples, first.skin_samples, first.background_samples
        choice = choose_encoder(info, available_encoders(ffmpeg), lambda candidate: encoder_works(ffmpeg, info, candidate, request.copy.suffix, deadline))
        encoder = ClipEncoder(ffmpeg, request.copy, info, choice, deadline=deadline)
        created.append(request.copy)
        corrected = FrameSamples()
        last: np.ndarray | None = None
        onto_edge = 0
        count = len(first.stats)
        try:
            for index, frame in enumerate(iter_frames(request.video, request.color, ffmpeg, size, deadline=deadline)):
                if index >= count:
                    raise ValueError(f"{request.video.name} decoded more frames the second time")
                out, moved = apply_regions(frame, plan.frame[index], *plan.region_parts(first.masks, index, size))
                onto_edge += moved
                encoder.write(out)
                corrected.add(out, _regions_of(first.masks[index], size))
                last = out
            if encoder.frames != count or last is None:
                raise ValueError(f"{request.video.name} decoded {encoder.frames} frames the second time, {count} the first")
            encoder.close(deadline)
        except BaseException:
            encoder.abort()
            raise
        corrected.finish()
        # Read before the tagger, which adds a colr box, as the run's own video is read (``MediaChecker.probe``).
        written = self._checker.probe(request.copy) if self._checker is not None else None
        self._tagger(request.copy, COPY_COLOR)
        created.append(request.handoff)
        write_handoff_png(handoff_samples(last.astype(np.float64) * 255), request.handoff, executable=ffmpeg)
        checks: list[MediaCheck] = []
        if self._checker is not None and written is not None:
            output, _probe = self._checker.output(request.copy, None, written, COPY_COLOR)
            checks.append(output)
        checks.append(correction_check(request, plan, corrected, input_stats, first_stats, min(onto_edge / (count * info.width * info.height), 1.0), choice, round(time.monotonic() - started, 1), limit))
        return CorrectionResult(True, drift[0], tuple(checks))


def _regions_of(masks: FrameMasks | None, size: tuple[int, int]) -> RegionsOf | None:
    """The regions of a corrected frame: the masks found in its original, so nothing is segmented twice."""
    if masks is None:
        return None
    return lambda _frame, _lab: regions_from_masks(masks, size)


def _some(pixels: np.ndarray) -> np.ndarray:
    """At most ``REGION_SAMPLE`` of a region's pixels (N, 3), evenly spread."""
    return np.ascontiguousarray(pixels[:: max(1, len(pixels) // REGION_SAMPLE)])


@dataclass
class _FirstPass:
    """What the first pass keeps of each frame: its statistics, a sample of its pixels and of its people's and skin's,
    and its masks."""

    stats: list[RegionStats] = field(default_factory=list)
    samples: list[np.ndarray] = field(default_factory=list)
    people_samples: list[np.ndarray | None] = field(default_factory=list)
    skin_samples: list[np.ndarray | None] = field(default_factory=list)
    background_samples: list[np.ndarray | None] = field(default_factory=list)
    masks: list[FrameMasks | None] = field(default_factory=list)

    def add(self, frame: np.ndarray, finder: RegionFinder) -> None:
        lab = oklab.srgb_to_oklab(frame.reshape(-1, 3))
        regions = finder(frame, lab)
        self.stats.append(region_stats(frame, regions, lab))
        self.samples.append(sample_pixels(frame))
        self.masks.append(regions.masks if regions is not None else None)
        self.people_samples.append(_some(frame[regions.people]) if regions is not None and regions.people is not None else None)
        self.skin_samples.append(_some(frame[regions.skin]) if regions is not None and regions.skin is not None else None)
        self.background_samples.append(_some(frame[~regions.people]) if regions is not None and regions.people is not None else None)


@dataclass
class _Plan:
    """Every frame's transforms: the whole frame's, or the background's when people are corrected apart; people's and
    skin's when they are (skin's None for a frame with too little of it); the regions corrected; the caps that bound,
    counted over frames; and the notes."""

    frame: list[Transform]
    people: list[Transform] | None = None
    skin: list[Transform | None] | None = None
    regions: list[str] = field(default_factory=lambda: ["frame"])
    bound: dict[str, int] = field(default_factory=dict)
    params: list[Params] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def region_parts(self, masks: list[FrameMasks | None], index: int, size: tuple[int, int]) -> tuple[tuple[Transform, np.ndarray] | None, tuple[Transform, np.ndarray] | None]:
        """Frame ``index``'s people and skin transforms with their blending weights, as ``apply_regions`` takes them."""
        if self.people is None:
            return None, None
        weights = blend_weights(masks, index, size)
        if weights is None:
            return None, None
        skin = self.skin[index] if self.skin is not None else None
        return (self.people[index], weights[0]), ((skin, weights[1]) if skin is not None else None)

    def count(self, params: list[Params], prefix: str = "") -> None:
        for frame_params in params:
            for name in frame_params.bound:
                key = f"{prefix}{name}"
                self.bound[key] = self.bound.get(key, 0) + 1


def _counts(region: str, stats: list[RegionStats], input_stats: RegionStats, anchor_stats: RegionStats | None) -> bool:
    """Whether ``region`` is corrected apart in this run: the input and the anchor have enough of it, and so do at least
    ``FRAMES_SHARE`` of the frames."""
    if region not in input_stats or (anchor_stats is not None and region not in anchor_stats):
        return False
    return sum(region in frame for frame in stats) >= FRAMES_SHARE * len(stats)


def _plan(first: _FirstPass, input_stats: RegionStats, anchor_stats: RegionStats | None, policy: ColorPolicy, had_segmenter: bool, *, failed: bool = False) -> _Plan:
    """Fit, cap, smooth, and scale the run's transforms: the whole frame's, and with ``policy.regions``, people's,
    their skin's, and the background's where they count; none of theirs when the segmenter ``failed`` during the run."""
    pull, strength = policy.pull, policy.strength
    frame_stats = [stats["frame"] for stats in first.stats]
    frame_fitted = fit_run(frame_stats, input_stats["frame"], anchor_stats["frame"] if anchor_stats is not None else None, pull)
    frame_params = finish_run(smooth(frame_fitted), frame_stats, strength, first.samples)
    plan = _Plan(frame=[transform_for(stats, params) for stats, params in zip(frame_stats, frame_params, strict=True)], params=frame_params)
    plan.count(frame_params)
    if not policy.regions:
        return plan
    if not had_segmenter:
        plan.notes.append(NO_VISION_NOTE)
        return plan
    if failed:
        plan.notes.append("Apple Vision failed during the run, so the whole run is corrected as one region.")
        return plan
    if not _counts("people", first.stats, input_stats, anchor_stats):
        plan.notes.append("Too little of the frame is people (in the input, the anchor, or half of the frames), so the whole frame is corrected as one region.")
        return plan
    people_stats = [stats.get("people") for stats in first.stats]
    used = [people if people is not None else whole for people, whole in zip(people_stats, frame_stats, strict=True)]
    people_smoothed = smooth(fit_run(people_stats, input_stats["people"], anchor_stats["people"] if anchor_stats is not None else None, pull, frame_fitted))
    plan.regions.append("people")
    if _counts("skin", first.stats, input_stats, anchor_stats):
        residuals, after = skin_residuals(first.skin_samples, used, people_smoothed, input_stats["skin"], anchor_stats["skin"] if anchor_stats is not None else None, pull)
        residuals = [scaled(params, strength) for params in smooth(residuals)]
        plan.skin = [transform_for(stats, params) if stats is not None else None for stats, params in zip(after, residuals, strict=True)]
        plan.regions.append("skin")
        plan.count(residuals)
    else:
        plan.notes.append("Too little of the frame is skin (in the input, the anchor, or half of the frames), so people's correction applies to it.")
    samples = [people if people is not None else whole for people, whole in zip(first.people_samples, first.samples, strict=True)]
    people_params = finish_run(people_smoothed, used, strength, samples)
    plan.people = [transform_for(stats, params) for stats, params in zip(used, people_params, strict=True)]
    plan.count(people_params, "people ")
    if _counts("background", first.stats, input_stats, anchor_stats):
        # The background by its own transform, not the whole frame's (owner decision, 2026-10-01): E0017's drift sat
        # mostly in it, and the whole frame's left it 6.6° off within the run.
        background_stats = [stats.get("background") for stats in first.stats]
        used = [background if background is not None else whole for background, whole in zip(background_stats, frame_stats, strict=True)]
        fitted = fit_run(background_stats, input_stats["background"], anchor_stats["background"] if anchor_stats is not None else None, pull, frame_fitted)
        samples = [background if background is not None else whole for background, whole in zip(first.background_samples, first.samples, strict=True)]
        background_params = finish_run(smooth(fitted), used, strength, samples)
        plan.frame = [transform_for(stats, params) for stats, params in zip(used, background_params, strict=True)]
        plan.regions.append("background")
        plan.count(background_params, "background ")
    return plan


def correction_check(request: CorrectionRequest, plan: _Plan, corrected: FrameSamples, input_stats: RegionStats, first_stats: RegionStats | None, onto_edge: float, choice: EncoderChoice, seconds: float, limit_seconds: float) -> MediaCheck:
    """The ``color_correction`` check: what was done, the caps that bound, and the drift left, measured on the
    corrected frames, region by region where they have regions."""
    policy = request.policy
    params = plan.params
    frame_0, last = corrected.curve[0][1], corrected.curve[-1][1]
    left = {"frame_0_to_last": compare_regions(frame_0, last), "input_to_frame_0": compare_regions(input_stats, frame_0)}
    if first_stats is not None:
        left = {"first_image_to_last": compare_regions(first_stats, last), **left}
    notes = [f"The {name} cap bound in {count} of {len(params)} frames; a large difference is more likely a change of scene than drift." for name, count in sorted(plan.bound.items())]
    if onto_edge >= 0.001:
        # Mostly pixels already on the edge (a channel at 0 or 255, common in generated video) that any turn or gain moves
        # a hair outside.
        notes.append(f"{onto_edge:.1%} of the pixels were pushed outside sRGB and brought onto its edge at constant lightness and hue.")
    notes.extend(plan.notes)
    if choice.skipped:
        notes.append(f"{', '.join(choice.skipped)} is listed by ffmpeg but could not encode here, so {choice.encoder} wrote the copy.")
    summary = f"{_what(request, plan.regions)}; left after correction: " + "; ".join(f"{COMPARISONS[name]}: {drift_text(*drifts)}" for name, drifts in left.items())
    facts: dict[str, Any] = {
        "policy": policy.as_dict(),
        "anchor": str(request.anchor) if request.anchor is not None else None,
        "reanchored": request.reanchored,
        "regions": plan.regions,
        "encoder": choice.encoder,
        "frames": len(params),
        "seconds": seconds,
        "limit_seconds": limit_seconds,
        "caps_bound": plan.bound,
        "onto_edge_share": round(onto_edge, 4),
        "parameters": [{"frame": index, **frame_params.as_dict()} for index, frame_params in enumerate(params) if index % 4 == 0 or index == len(params) - 1],
        "left": {name: drift.as_dict() for name, (drift, _regions) in left.items()},
        "left_regions": {name: {region: drift.as_dict() for region, drift in regions.items()} for name, (_drift, regions) in left.items() if regions},
    }
    return MediaCheck("color_correction", request.copy.name, summary, (), tuple(notes), facts)


def _what(request: CorrectionRequest, regions: list[str]) -> str:
    """For example ``blend 0.25 toward walk-job-first-image.png, re-anchored at this run's input, people, skin and
    background apart``."""
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
    apart = regions[1:]
    if apart:
        text += ", " + (apart[0] if len(apart) == 1 else f"{', '.join(apart[:-1])} and {apart[-1]}") + " apart"
    return text
