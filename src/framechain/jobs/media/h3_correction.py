"""Bounded H3 correction on explicitly decoded sRGB pixels.

This deliberately does not use Draw Things' half-level restoration, ProRes
path, or correction encoder. The caps are versioned for H3 and conservative;
the owner-approved source RGB convention is enforced by the caller.
"""

from __future__ import annotations

import math
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from framechain.jobs.media import oklab
from framechain.jobs.media.color_stats import ColorStats, measure, turn_degrees
from framechain.jobs.media.h3_color import DELIVERY_TO_SRGB_FILTER, NATIVE_TO_SRGB_FILTER, H3ColorCancelled, measure_h3_image
from framechain.jobs.media.regions import FRAMES_SHARE, FrameMasks, RegionFinder, Segmenter, blend_weights, make_segmenter, region_stats

H3_CORRECTION_VERSION = 1
LIGHTNESS_CAP = 0.03
CONTRAST_CAP = (0.95, 1.05)
CHROMA_CAP = (0.92, 1.08)
HUE_CAP_DEGREES = 4.0
CAST_CAP = 0.01
CLIPPED_PIXEL_CAP = 0.02
SKIN_LIGHTNESS_CAP = 0.015
SKIN_CHROMA_CAP = (0.96, 1.04)
SKIN_HUE_CAP_DEGREES = 2.0
SKIN_CAST_CAP = 0.005
CORRECTION_TIMEOUT_SECONDS = 600
ENCODE_FILTER = "colorspace=ispace=gbr:irange=pc:iprimaries=bt709:itrc=srgb:space=bt709:range=tv:primaries=bt709:trc=bt709:format=yuv420p"


@dataclass(frozen=True)
class H3Params:
    lightness: float = 0.0
    contrast: float = 1.0
    chroma: float = 1.0
    hue: float = 0.0
    cast_a: float = 0.0
    cast_b: float = 0.0
    bound: tuple[str, ...] = ()

    @property
    def identity(self) -> bool:
        return self.lightness == 0 and self.contrast == 1 and self.chroma == 1 and self.hue == 0 and self.cast_a == 0 and self.cast_b == 0


def _cap(value: float, low: float, high: float, label: str, bounds: set[str]) -> float:
    if value < low or value > high:
        bounds.add(label)
    return min(max(value, low), high)


def _target(start: ColorStats, anchor: ColorStats | None, share: float) -> tuple[float, float, float, float | None, tuple[float, float] | None]:
    if anchor is None or share == 0:
        return start.median, start.spread, start.chroma, start.hue, start.cast
    hue = start.hue
    if start.hue is not None and anchor.hue is not None:
        hue = start.hue + math.radians(turn_degrees(anchor.hue - start.hue)) * share
    cast = start.cast
    if start.cast is not None and anchor.cast is not None:
        cast = (start.cast[0] + (anchor.cast[0] - start.cast[0]) * share, start.cast[1] + (anchor.cast[1] - start.cast[1]) * share)
    return start.median + (anchor.median - start.median) * share, start.spread + (anchor.spread - start.spread) * share, start.chroma + (anchor.chroma - start.chroma) * share, hue, cast


def _params(current: ColorStats, target: tuple[float, float, float, float | None, tuple[float, float] | None], strength: float, *, skin: bool = False) -> H3Params:
    median, spread, chroma, hue, cast = target
    bounds: set[str] = set()
    lightness_cap = SKIN_LIGHTNESS_CAP if skin else LIGHTNESS_CAP
    chroma_cap = SKIN_CHROMA_CAP if skin else CHROMA_CAP
    hue_cap = SKIN_HUE_CAP_DEGREES if skin else HUE_CAP_DEGREES
    cast_cap = SKIN_CAST_CAP if skin else CAST_CAP
    prefix = "skin_" if skin else ""
    lightness = _cap(median - current.median, -lightness_cap, lightness_cap, prefix + "lightness", bounds)
    contrast = _cap(spread / current.spread, *CONTRAST_CAP, "contrast", bounds) if current.spread > 1e-6 and spread > 1e-6 else 1.0
    gain = _cap(chroma / current.chroma, *chroma_cap, prefix + "chroma", bounds) if current.chroma > 1e-6 and chroma > 1e-6 else 1.0
    turn = _cap(math.radians(turn_degrees(hue - current.hue)), -math.radians(hue_cap), math.radians(hue_cap), prefix + "hue", bounds) if hue is not None and current.hue is not None else 0.0
    shift_a = shift_b = 0.0
    if cast is not None and current.cast is not None:
        cosine, sine = math.cos(turn), math.sin(turn)
        moved = (gain * (cosine * current.cast[0] - sine * current.cast[1]), gain * (sine * current.cast[0] + cosine * current.cast[1]))
        shift_a, shift_b = cast[0] - moved[0], cast[1] - moved[1]
        length = math.hypot(shift_a, shift_b)
        if length > cast_cap:
            bounds.add(prefix + "cast")
            shift_a *= cast_cap / length
            shift_b *= cast_cap / length
    return H3Params(lightness * strength, contrast**strength, gain**strength, turn * strength, shift_a * strength, shift_b * strength, tuple(sorted(bounds)))


def _smooth(params: list[H3Params]) -> list[H3Params]:
    if len(params) < 3:
        return params
    columns = np.asarray([[item.lightness, math.log(item.contrast), math.log(item.chroma), item.hue, item.cast_a, item.cast_b] for item in params])
    padded = np.pad(columns, ((2, 2), (0, 0)), mode="edge")
    weights = np.asarray([1, 2, 3, 2, 1], dtype=np.float64) / 9
    smoothed = np.stack([weights @ padded[index : index + 5] for index in range(len(params))])
    return [replace(item, lightness=float(row[0]), contrast=float(math.exp(row[1])), chroma=float(math.exp(row[2])), hue=float(row[3]), cast_a=float(row[4]), cast_b=float(row[5])) for item, row in zip(params, smoothed, strict=True)]


def _apply_once(frame: np.ndarray, current: ColorStats, params: H3Params) -> tuple[np.ndarray, float]:
    if params.identity:
        return frame, 0.0
    lab = oklab.srgb_to_oklab(frame.reshape(-1, 3))
    lab[:, 0] = np.clip((lab[:, 0] - current.median) * params.contrast + current.median + params.lightness, 0, 1)
    cosine, sine = math.cos(params.hue), math.sin(params.hue)
    a, b = lab[:, 1].copy(), lab[:, 2].copy()
    lab[:, 1] = params.chroma * (cosine * a - sine * b) + params.cast_a
    lab[:, 2] = params.chroma * (sine * a + cosine * b) + params.cast_b
    linear = oklab.oklab_to_linear_srgb(lab)
    outside = np.any((linear < -oklab.INSIDE) | (linear > 1 + oklab.INSIDE), axis=1)
    fraction = float(np.mean(outside))
    if fraction:
        mapped, _ = oklab.onto_srgb_edge(lab[outside])
        linear[outside] = oklab.oklab_to_linear_srgb(mapped)
    corrected = np.clip(oklab.linear_to_srgb(linear).reshape(frame.shape), 0, 1).astype(np.float32)
    return corrected, fraction


def apply_h3_params(frame: np.ndarray, current: ColorStats, params: H3Params) -> tuple[np.ndarray, float, bool]:
    """Apply the fitted transform, reducing it to identity if gamut-edge changes exceed 2%."""
    trial = params
    for attempt in range(6):
        corrected, clipped = _apply_once(frame, current, trial)
        if clipped <= CLIPPED_PIXEL_CAP:
            return corrected, clipped, attempt > 0
        trial = replace(trial, lightness=trial.lightness * 0.5, contrast=math.sqrt(trial.contrast), chroma=math.sqrt(trial.chroma), hue=trial.hue * 0.5, cast_a=trial.cast_a * 0.5, cast_b=trial.cast_b * 0.5)
    return frame, 0.0, True


def _decoder(source: Path, width: int, height: int, ffmpeg: str, *, rec709_delivery: bool) -> subprocess.Popen[bytes]:
    color_filter = DELIVERY_TO_SRGB_FILTER if rec709_delivery else NATIVE_TO_SRGB_FILTER
    return subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source), "-vf", color_filter, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _read_frame(process: subprocess.Popen[bytes], width: int, height: int, index: int, deadline: float, stop_requested: Callable[[], signal.Signals | None]) -> np.ndarray:
    assert process.stdout is not None
    size = width * height * 3
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        if (received := stop_requested()) is not None:
            raise H3ColorCancelled(received)
        if (left := deadline - time.monotonic()) <= 0:
            raise ValueError("H3 correction timed out during frame decode")
        readable, _, _ = select.select([process.stdout], [], [], min(0.25, left))
        if not readable:
            continue
        chunk = os.read(process.stdout.fileno(), min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != width * height * 3:
        raise ValueError(f"H3 correction decode stopped at frame {index}")
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3).astype(np.float32) / 255.0


def _write_frame(process: subprocess.Popen[bytes], data: bytes, deadline: float, stop_requested: Callable[[], signal.Signals | None]) -> None:
    assert process.stdin is not None
    written = 0
    while written < len(data):
        if (received := stop_requested()) is not None:
            raise H3ColorCancelled(received)
        if (left := deadline - time.monotonic()) <= 0:
            raise ValueError("H3 correction timed out during frame encode")
        _, writable, _ = select.select([], [process.stdin], [], min(0.25, left))
        if writable:
            written += os.write(process.stdin.fileno(), data[written : written + (1 << 20)])


def _wait_process(process: subprocess.Popen[bytes], deadline: float, stop_requested: Callable[[], signal.Signals | None], failure: str) -> None:
    while True:
        if (received := stop_requested()) is not None:
            raise H3ColorCancelled(received)
        if (left := deadline - time.monotonic()) <= 0:
            raise ValueError("H3 correction timed out")
        try:
            if process.wait(timeout=min(0.25, left)):
                raise ValueError(failure)
            return
        except subprocess.TimeoutExpired:
            continue


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _image_pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0


def correct_h3_video(source: Path, target: Path, ffmpeg: str, *, frames: int, width: int, height: int, run_input: Path, segment_anchor: Path | None, pull: float, strength: float, rec709_delivery: bool, stop_requested: Callable[[], signal.Signals | None], regions: bool = False, segmenter_factory: Callable[[], Segmenter | None] | None = None) -> dict[str, object]:
    """Two-pass sRGB correction; write H.264 Rec.709 TV while copying the source AAC packets."""
    if target.exists():
        raise ValueError("staged corrected H3 output already exists")
    input_stats = measure_h3_image(run_input)
    anchor_stats = measure_h3_image(segment_anchor) if segment_anchor is not None and pull > 0 else None
    finder = RegionFinder(make_segmenter(segmenter_factory)) if regions else None
    input_regions: dict[str, ColorStats] = {}
    anchor_regions: dict[str, ColorStats] = {}
    if finder is not None and finder.active:
        input_pixels = _image_pixels(run_input)
        input_regions = region_stats(input_pixels, finder(input_pixels))
        if finder.active and segment_anchor is not None and pull > 0:
            anchor_pixels = _image_pixels(segment_anchor)
            anchor_regions = region_stats(anchor_pixels, finder(anchor_pixels))
    deadline = time.monotonic() + CORRECTION_TIMEOUT_SECONDS
    decoder = _decoder(source, width, height, ffmpeg, rec709_delivery=rec709_delivery)
    stats: list[ColorStats] = []
    frame_regions: list[dict[str, ColorStats]] = []
    masks: list[FrameMasks | None] = []
    try:
        for index in range(frames):
            if (received := stop_requested()) is not None:
                raise H3ColorCancelled(received)
            if time.monotonic() > deadline:
                raise ValueError("H3 correction timed out during statistics pass")
            frame = _read_frame(decoder, width, height, index, deadline, stop_requested)
            found = finder(frame) if finder is not None and finder.active else None
            measured = region_stats(frame, found) if found is not None else {"frame": measure(frame)}
            stats.append(measured["frame"])
            frame_regions.append(measured)
            masks.append(found.masks if found is not None else None)
        _wait_process(decoder, deadline, stop_requested, "H3 correction decoder failed during statistics pass")
    finally:
        _stop(decoder)
        if decoder.stdout is not None:
            decoder.stdout.close()
        if decoder.stderr is not None:
            decoder.stderr.close()

    if finder is not None and not finder.active:
        frame_regions = []
        masks = []
    region_names = tuple(name for name in ("people", "background", "skin") if frame_regions and name in input_regions and (name in anchor_regions or anchor_stats is None) and sum(name in item for item in frame_regions) >= FRAMES_SHARE * frames)
    if "people" not in region_names:
        region_names = ()
    fitted = []
    fitted_regions: dict[str, list[H3Params]] = {name: [] for name in region_names}
    for index, current in enumerate(stats):
        ramp = 1.0 if frames <= 1 else (index / (frames - 1)) ** 2 * (3 - 2 * index / (frames - 1))
        fitted.append(_params(current, _target(input_stats, anchor_stats, pull * ramp), strength))
        for name in region_names:
            regional = frame_regions[index].get(name)
            if regional is None:
                fitted_regions[name].append(H3Params())
                continue
            fitted_regions[name].append(_params(regional, _target(input_regions[name], anchor_regions.get(name), pull * ramp), strength, skin=name == "skin"))
    fitted = _smooth(fitted)
    fitted_regions = {name: _smooth(values) for name, values in fitted_regions.items()}
    encoder = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", "24", "-i", "-", "-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-vf", ENCODE_FILTER, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv", "-c:a", "copy", "-movflags", "+faststart", str(target)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    decoder: subprocess.Popen[bytes] | None = None
    peak_clipped = 0.0
    reduced = 0
    try:
        decoder = _decoder(source, width, height, ffmpeg, rec709_delivery=rec709_delivery)
        assert encoder.stdin is not None
        for index in range(frames):
            if (received := stop_requested()) is not None:
                raise H3ColorCancelled(received)
            if time.monotonic() > deadline:
                raise ValueError("H3 correction timed out during encoding pass")
            frame = _read_frame(decoder, width, height, index, deadline, stop_requested)
            corrected, clipped, was_reduced = apply_h3_params(frame, stats[index], fitted[index])
            peak_clipped = max(peak_clipped, clipped)
            reduced += int(was_reduced)
            if region_names and "people" in frame_regions[index] and (weights := blend_weights(masks, index, (width, height))) is not None:
                person_weight, skin_weight = weights
                if "background" in region_names and "background" in frame_regions[index]:
                    background, clipped, was_reduced = apply_h3_params(frame, frame_regions[index]["background"], fitted_regions["background"][index])
                    peak_clipped = max(peak_clipped, clipped)
                    reduced += int(was_reduced)
                else:
                    background = corrected
                people, clipped, was_reduced = apply_h3_params(frame, frame_regions[index]["people"], fitted_regions["people"][index])
                peak_clipped = max(peak_clipped, clipped)
                reduced += int(was_reduced)
                if "skin" in region_names and "skin" in frame_regions[index]:
                    skin, clipped, was_reduced = apply_h3_params(frame, frame_regions[index]["skin"], fitted_regions["skin"][index])
                    peak_clipped = max(peak_clipped, clipped)
                    reduced += int(was_reduced)
                    corrected = background * (1 - person_weight[..., None]) + people * (person_weight - skin_weight)[..., None] + skin * skin_weight[..., None]
                else:
                    corrected = background * (1 - person_weight[..., None]) + people * person_weight[..., None]
            _write_frame(encoder, np.rint(corrected * 255).astype(np.uint8).tobytes(), deadline, stop_requested)
        encoder.stdin.close()
        _wait_process(decoder, deadline, stop_requested, "H3 correction decoder failed during encoding pass")
        _wait_process(encoder, deadline, stop_requested, "H3 correction encoder failed")
        if not target.is_file():
            raise ValueError("H3 correction encoder failed")
    finally:
        if decoder is not None:
            _stop(decoder)
        _stop(encoder)
        if decoder is not None and decoder.stdout is not None:
            decoder.stdout.close()
        if decoder is not None and decoder.stderr is not None:
            decoder.stderr.close()
        if encoder.stdin is not None and not encoder.stdin.closed:
            encoder.stdin.close()
        if encoder.stderr is not None:
            encoder.stderr.close()
    bound = sorted({label for item in fitted for label in item.bound} | {label for values in fitted_regions.values() for item in values for label in item.bound})
    return {"version": H3_CORRECTION_VERSION, "frames": frames, "bound": bound, "clipping_reduced_frames": reduced, "peak_clipped_fraction": round(peak_clipped, 4), "regions": list(region_names) if region_names else ["frame"], "region_fallback": "segmentation unavailable or insufficient" if regions and not region_names else None, "caps": {"lightness": LIGHTNESS_CAP, "contrast": list(CONTRAST_CAP), "chroma": list(CHROMA_CAP), "hue_degrees": HUE_CAP_DEGREES, "cast": CAST_CAP, "skin_lightness": SKIN_LIGHTNESS_CAP, "skin_chroma": list(SKIN_CHROMA_CAP), "skin_hue_degrees": SKIN_HUE_CAP_DEGREES, "skin_cast": SKIN_CAST_CAP, "clipped_fraction": CLIPPED_PIXEL_CAP}}
