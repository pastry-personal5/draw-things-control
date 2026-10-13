"""Inspect H3 H.264 declarations and stage the characterized SDR conversion.

The source RGB convention is an owner-approved assumption for one h3.c
executable fingerprint. The observed encode path is limited-range BT.601 YUV;
neither missing metadata nor this module establishes an arbitrary H3 build's
transfer function.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from framechain.jobs.media.video_color import ColrTag, read_colr, read_colr_kind

if TYPE_CHECKING:
    from framechain.jobs.media.color_stats import ColorStats

H3_RGB_CONVENTION_SHA256 = "79d4386f8f0825992f2204c643554fbba7c9c44fd7bfe54f1c6b2ba22308be9f"
NATIVE = ColrTag("nclx", 1, 13, 5, False)
DELIVERY = ColrTag("nclx", 1, 1, 1, False)
CONVERT_FILTER = "colorspace=ispace=bt470bg:irange=tv:iprimaries=bt709:itrc=srgb:space=bt709:range=tv:primaries=bt709:trc=bt709:format=yuv420p"
DELIVERY_TO_SRGB_FILTER = "colorspace=ispace=bt709:irange=tv:iprimaries=bt709:itrc=bt709:space=bt470bg:range=tv:primaries=bt709:trc=srgb:format=yuv420p,scale=in_color_matrix=bt470bg:in_range=tv:out_range=pc,format=rgb24"
NATIVE_TO_SRGB_FILTER = "scale=in_color_matrix=bt470bg:in_range=tv:out_range=pc,format=rgb24"
COLOR_TIMEOUT_SECONDS = 180
DECODE_TIMEOUT_SECONDS = 90
PROBE_TIMEOUT_SECONDS = 30


class H3ColorCancelled(Exception):
    """A stop request received while a staged H3 conversion was running."""

    def __init__(self, received_signal: signal.Signals) -> None:
        self.received_signal = received_signal
        super().__init__(str(received_signal))


@dataclass(frozen=True)
class H3Declarations:
    container: ColrTag | None
    bitstream: ColrTag | None
    stream: dict[str, str | None]
    unsupported_container_kind: str | None = None

    def facts(self) -> dict[str, Any]:
        return {"container": self.container.text() if self.container else None, "unsupported_container_kind": self.unsupported_container_kind, "bitstream": self.bitstream.text() if self.bitstream else None, "stream": self.stream}


def inspect_h3_declarations(video: Path, ffmpeg: str, ffprobe: str) -> H3Declarations:
    """Read container, H.264 SPS/VUI, and FFprobe's merged stream view separately."""
    container = read_colr(video)
    container_kind = read_colr_kind(video)
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_range,color_space,color_transfer,color_primaries", "-of", "json", str(video)], capture_output=True, text=True, errors="replace", timeout=PROBE_TIMEOUT_SECONDS, check=False)
    if probe.returncode:
        raise ValueError(probe.stderr.strip() or "could not inspect H3 stream color")
    streams = json.loads(probe.stdout).get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise ValueError("could not inspect H3 video stream color")
    stream = {key: streams[0].get(key) for key in ("color_range", "color_space", "color_transfer", "color_primaries")}
    trace = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "trace", "-i", str(video), "-map", "0:v:0", "-frames:v", "1", "-c:v", "copy", "-bsf:v", "trace_headers", "-f", "null", "-"], capture_output=True, text=True, errors="replace", timeout=PROBE_TIMEOUT_SECONDS, check=False)
    if trace.returncode:
        raise ValueError(trace.stderr.strip()[-500:] or "could not inspect H3 H.264 color")
    fields = {key: int(value) for key, value in re.findall(r"\b(video_signal_type_present_flag|video_full_range_flag|colour_description_present_flag|colour_primaries|transfer_characteristics|matrix_coefficients)\s+\d+\s+=\s+(\d+)\b", trace.stderr)}
    if "video_signal_type_present_flag" not in fields:
        raise ValueError("H3 H.264 SPS color flags were unavailable")
    bitstream = None
    if fields["video_signal_type_present_flag"]:
        if "video_full_range_flag" not in fields or "colour_description_present_flag" not in fields:
            raise ValueError("H3 H.264 SPS color flags were incomplete")
        if fields["colour_description_present_flag"]:
            if not all(key in fields for key in ("colour_primaries", "transfer_characteristics", "matrix_coefficients")):
                raise ValueError("H3 H.264 SPS color description was incomplete")
            bitstream = ColrTag("vui", fields["colour_primaries"], fields["transfer_characteristics"], fields["matrix_coefficients"], bool(fields["video_full_range_flag"]))
        else:
            bitstream = ColrTag("vui", 2, 2, 2, bool(fields["video_full_range_flag"]))
    return H3Declarations(container, bitstream, stream, container_kind if container is None else None)


def declarations_match(declarations: H3Declarations, expected: ColrTag, *, require_explicit: bool) -> bool:
    """Check every declaration that is present; require all three for a delivery encode."""
    if declarations.unsupported_container_kind is not None:
        return False
    for tag in (declarations.container, declarations.bitstream):
        if tag is None:
            if require_explicit:
                return False
            continue
        if (tag.primaries, tag.transfer, tag.matrix, tag.full_range) != (expected.primaries, expected.transfer, expected.matrix, expected.full_range):
            return False
    names = {"color_range": "tv" if not expected.full_range else "pc", "color_space": "bt470bg" if expected.matrix == 5 else "bt709", "color_transfer": "iec61966-2-1" if expected.transfer == 13 else "bt709", "color_primaries": "bt709"}
    for field, expected_name in names.items():
        actual = declarations.stream[field]
        if actual not in (None, "unknown") and actual != expected_name:
            return False
        if require_explicit and actual != expected_name:
            return False
    return True


def packet_facts(video: Path, ffprobe: str, stream: str) -> list[tuple[int | None, int | None, int | None, str | None]]:
    """Packet payload hashes and integer time stamps, including AAC priming."""
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", stream, "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=pts,dts,duration,data_hash", "-of", "json", str(video)], capture_output=True, text=True, errors="replace", timeout=PROBE_TIMEOUT_SECONDS, check=False)
    if probe.returncode:
        raise ValueError(probe.stderr.strip() or "could not inspect H3 packets")
    packets = json.loads(probe.stdout).get("packets")
    if not isinstance(packets, list) or not packets:
        raise ValueError("H3 packet inspection found no packets")
    return [(packet.get("pts"), packet.get("dts"), packet.get("duration"), packet.get("data_hash")) for packet in packets]


def convert_h3_delivery(source: Path, target: Path, ffmpeg: str, stop_requested: Callable[[], signal.Signals | None] | None = None) -> None:
    """Convert assumed sRGB/BT.709 RGB encoded as BT.601 TV YUV to BT.709 TV, copying native AAC."""
    if target.exists():
        raise ValueError(f"staged H3 delivery already exists: {target}")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0", "-vf", CONVERT_FILTER, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv", "-c:a", "copy", "-movflags", "+faststart", str(target)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace") as process:
        deadline = time.monotonic() + COLOR_TIMEOUT_SECONDS
        while True:
            if stop_requested is not None and (received_signal := stop_requested()) is not None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise H3ColorCancelled(received_signal)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise ValueError("H3 Rec.709 conversion timed out")
            try:
                _, stderr = process.communicate(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    if process.returncode or not target.is_file():
        raise ValueError(stderr.strip()[-500:] or "H3 Rec.709 conversion failed")


def measure_h3_image(image: Path) -> ColorStats:
    """Measure an H3 input or prior handoff, both prepared as sRGB RGB images."""
    import numpy as np
    from PIL import Image

    from framechain.jobs.media.color_stats import measure

    with Image.open(image) as opened:
        frame = np.asarray(opened.convert("RGB"), dtype=np.float32) / 255.0
    return measure(frame)


def decode_h3_video_frame(video: Path, index: int, width: int, height: int, ffmpeg: str, *, rec709_delivery: bool, stop_requested: Callable[[], signal.Signals | None] | None = None) -> Any:
    """Decode a chosen frame to sRGB under the verified source or delivery interpretation."""
    import numpy as np

    color_filter = DELIVERY_TO_SRGB_FILTER if rec709_delivery else NATIVE_TO_SRGB_FILTER
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf", f"select=eq(n\\,{index}),{color_filter}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS
        while True:
            if stop_requested is not None and (received_signal := stop_requested()) is not None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise H3ColorCancelled(received_signal)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise ValueError("H3 drift frame decode timed out")
            try:
                pixels, stderr = process.communicate(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    if process.returncode or len(pixels) != width * height * 3:
        raise ValueError(stderr.decode(errors="replace").strip()[-500:] or f"could not decode H3 frame {index}")
    frame = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3).astype(np.float32) / 255.0
    return frame


def measure_h3_video_frame(video: Path, index: int, width: int, height: int, ffmpeg: str, *, rec709_delivery: bool, stop_requested: Callable[[], signal.Signals | None] | None = None) -> ColorStats:
    """Measure a chosen frame under the verified source or converted delivery interpretation."""
    from framechain.jobs.media.color_stats import measure

    return measure(decode_h3_video_frame(video, index, width, height, ffmpeg, rec709_delivery=rec709_delivery, stop_requested=stop_requested))


def measure_h3_drift(video: Path, frames: int, width: int, height: int, ffmpeg: str, *, rec709_delivery: bool, run_input: Path | None, segment_anchor: Path | None, stop_requested: Callable[[], signal.Signals | None] | None = None, regions: bool = False, segmenter_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Measure within-run, prepared-input, and current-segment-anchor drift."""
    import numpy as np
    from PIL import Image

    from framechain.jobs.media.color_stats import compare, measure
    from framechain.jobs.media.regions import RegionFinder, make_segmenter, region_stats

    first_pixels = decode_h3_video_frame(video, 0, width, height, ffmpeg, rec709_delivery=rec709_delivery, stop_requested=stop_requested)
    last_pixels = decode_h3_video_frame(video, frames - 1, width, height, ffmpeg, rec709_delivery=rec709_delivery, stop_requested=stop_requested)
    first, last = measure(first_pixels), measure(last_pixels)
    result: dict[str, Any] = {"first": first.as_dict(), "last": last.as_dict(), "first_to_last": compare(first, last).as_dict()}
    if run_input is None:
        result["input_to_first"] = {"unavailable": "r2av has no first-frame input"}
    else:
        try:
            input_stats = measure_h3_image(run_input)
            result["input_to_first"] = compare(input_stats, first).as_dict()
        except (OSError, ValueError):
            result["input_to_first"] = {"unavailable": "prepared i2av input could not be measured"}
    if segment_anchor is None:
        result["anchor_to_last"] = {"unavailable": "r2av has no i2av segment anchor"}
    else:
        try:
            result["anchor_to_last"] = compare(measure_h3_image(segment_anchor), last).as_dict()
        except (OSError, ValueError):
            result["anchor_to_last"] = {"unavailable": "i2av segment anchor could not be measured"}
    if regions:
        finder = RegionFinder(make_segmenter(segmenter_factory))
        if finder.active:
            frame_regions = [region_stats(pixels, finder(pixels)) for pixels in (first_pixels, last_pixels)]
            image_regions = []
            for path in (run_input, segment_anchor):
                if path is None or not finder.active:
                    image_regions.append({})
                    continue
                with Image.open(path) as image:
                    pixels = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
                image_regions.append(region_stats(pixels, finder(pixels)))
            if finder.active and "people" in frame_regions[0] and "people" in frame_regions[1]:
                comparisons: dict[str, Any] = {}
                for name in ("people", "background", "skin"):
                    if name not in frame_regions[0] or name not in frame_regions[1]:
                        continue
                    comparisons[name] = {"first_to_last": compare(frame_regions[0][name], frame_regions[1][name]).as_dict()}
                    if name in image_regions[0]:
                        comparisons[name]["input_to_first"] = compare(image_regions[0][name], frame_regions[0][name]).as_dict()
                    if name in image_regions[1]:
                        comparisons[name]["anchor_to_last"] = compare(image_regions[1][name], frame_regions[1][name]).as_dict()
                result["regions"] = comparisons
            else:
                result["regions"] = {"unavailable": "segmentation unavailable or insufficient"}
        else:
            result["regions"] = {"unavailable": "segmentation unavailable or insufficient"}
    return result
