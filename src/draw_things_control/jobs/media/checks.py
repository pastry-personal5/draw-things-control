"""Checks of what each file of an i2v job holds: the first input, its resized copy, a run's video, and its last frame.

A check reads a file and says what it holds and whether that is what it should be. It never changes a file and
never fails a run: a file it cannot read is a warning that says so. The executor turns each check into a
``MediaChecked`` event, which the job log, ``dtc serve``'s output, and the TUI's Messages all show.
"""

from __future__ import annotations

import io
import json
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from draw_things_control.jobs.media.fingerprint import SAME_MATRIX, MatrixFingerprint, can_measure, measure_matrix
from draw_things_control.jobs.media.stream_color import RANGES, UNSTATED, CommandRunner, StreamColor
from draw_things_control.jobs.media.tools import find_ffprobe
from draw_things_control.jobs.media.video_color import ColrTag, read_colr

if TYPE_CHECKING:
    from draw_things_control.jobs.inputs.size import ResizePlan

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_COLOR_TYPES = {0: "gray", 2: "RGB", 3: "palette", 4: "gray and alpha", 6: "RGBA"}
# The chunks that say what color a PNG's values mean.
PNG_COLOR_CHUNKS = ("sRGB", "iCCP", "cHRM", "gAMA")
# What each --video-format writes: the codec, and the sample entry type when that tells the variants apart.
VIDEO_FORMATS = {"prores4444": ("prores", "ap4h"), "prores422hq": ("prores", "apch"), "h264": ("h264", None), "hevc": ("hevc", None)}
CODEC_NAMES = {"ap4h": "ProRes 4444", "ap4x": "ProRes 4444 XQ", "apch": "ProRes 422 HQ", "apcn": "ProRes 422", "apcs": "ProRes 422 LT", "apco": "ProRes 422 Proxy", "h264": "H.264", "hevc": "HEVC"}
# H.273 codes as the tagger writes them, and as ffprobe names them.
H273_MATRICES = {1: "bt709", 5: "bt470bg", 6: "smpte170m", 9: "bt2020nc"}
H273_NAMES = {1: "BT.709", 13: "sRGB", 5: "BT.601", 6: "BT.601", 9: "BT.2020"}
# How far, in 8-bit levels, the resized copy's mean color may drift from its source's. Measured: under 0.05 on the owner's
# input, 0 on test patterns; a shift from a wrong conversion or gamma is several levels.
DRIFT_LEVELS = 1.0
PROBE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class MediaCheck:
    """One check's result, before the executor stamps it into an event."""

    stage: str
    file: str
    summary: str
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return "warning" if self.warnings else "ok"


@dataclass(frozen=True)
class VideoProbe:
    """What ffprobe reads of a video's first stream, and the color its first frame states (None: not stated)."""

    codec: str | None
    tag: str | None
    profile: str | None
    width: int
    height: int
    pix_fmt: str | None
    frames: int | None
    color_space: str | None
    color_range: str | None
    color_primaries: str | None
    color_transfer: str | None

    @property
    def codec_text(self) -> str:
        name = CODEC_NAMES.get(self.tag or "") or CODEC_NAMES.get(self.codec or "") or (self.codec or "unknown codec")
        return f"{name} ({self.tag})" if self.tag else name

    def facts(self) -> dict[str, Any]:
        return {"codec": self.codec, "tag": self.tag, "profile": self.profile, "width": self.width, "height": self.height, "pix_fmt": self.pix_fmt, "frames": self.frames, "color_space": self.color_space, "color_range": self.color_range, "color_primaries": self.color_primaries, "color_transfer": self.color_transfer}


class MediaChecker:
    """Runs the checks with ffmpeg and ffprobe, found when each check runs, so a server started before they were installed
    finds them once they are; a test gives it a fake ``run`` so neither tool starts."""

    def __init__(self, find_ffmpeg: Callable[[], str | None], *, run: CommandRunner = subprocess.run) -> None:
        self._find_ffmpeg = find_ffmpeg
        self._run = run

    def _tools(self) -> tuple[str | None, str | None]:
        """ffmpeg, and the ffprobe beside it (else on PATH), as they are now."""
        ffmpeg = self._find_ffmpeg()
        return ffmpeg, find_ffprobe(ffmpeg)

    def input(self, path: Path) -> MediaCheck:
        """The job's input, before run 1; draw-things-cli reads its copy, never the file itself."""
        return _guarded("input", str(path), lambda: check_input(path))

    def handoff(self, path: Path) -> MediaCheck:
        """A resume's input: the chain's own last frame, which it continues from, before the resumed run."""
        return _guarded("input", str(path), lambda: check_handoff(path))

    def resized_input(self, source: Path, copy: Path, plan: ResizePlan) -> MediaCheck:
        """Run 1's copy of the input, resized or at scale 1, against the source read as the copy was made."""
        return _guarded("resized_input", copy.name, lambda: check_resized_input(source, copy, plan, self._find_ffmpeg()))

    def probe(self, video: Path) -> VideoProbe | str:
        """The video as its writer left it, read before the tagger adds a box; the reason as text when it cannot be read."""
        try:
            return probe_video(video, self._tools()[1], self._run)
        # Whatever breaks the probe, the run goes on.
        except Exception as error:
            return str(error)

    def output(self, video: Path, requested_format: str | None, written: VideoProbe | str, color: StreamColor) -> tuple[MediaCheck, VideoProbe | None]:
        """The run's video after tagging, decoded with ``color``; and the video as ffprobe reads it now."""
        result: list[VideoProbe | None] = [None]

        def check() -> MediaCheck:
            if isinstance(written, str):
                raise ValueError(written)
            ffmpeg, ffprobe = self._tools()
            tagged = probe_video(video, ffprobe, self._run)
            result[0] = tagged
            return check_output(video, requested_format, written, color, ffmpeg, self._run)

        return _guarded("output", video.name, check), result[0]

    def last_frame(self, png: Path, video: VideoProbe | None, color: StreamColor) -> MediaCheck:
        return _guarded("last_frame", png.name, lambda: check_last_frame(png, video, color))

    def color_drift(self, video: Path, color: StreamColor, run_input: Path | None, first_image: Path | None, notes: tuple[str, ...] = ()) -> MediaCheck:
        """The run's color drift (Milestone 09): its frames decoded with ``color``, against its input and the first image."""

        def check() -> MediaCheck:
            # Imported here, as LittleCMS is, so a process that never checks a run does not load them.
            from draw_things_control.jobs.media.clip_frames import probe_clip
            from draw_things_control.jobs.media.drift import check_color_drift

            ffmpeg, ffprobe = self._tools()
            if ffmpeg is None or ffprobe is None:
                raise ValueError("ffmpeg or ffprobe was not found")
            info = probe_clip(video, ffprobe)
            return check_color_drift(video, color, ffmpeg, (info.width, info.height), run_input=run_input, first_image=first_image, notes=notes)

        return _guarded("color_drift", video.name, check)


def _guarded(stage: str, file: str, check: Callable[[], MediaCheck]) -> MediaCheck:
    """``check()``, or a warning that the file could not be checked: a check never stops a run."""
    try:
        return check()
    # Whatever breaks a check, the run goes on.
    except Exception as error:
        return MediaCheck(stage, file, "could not be checked", warnings=(f"Could not check it: {error}.",))


# Images


def png_chunks(path: Path) -> tuple[dict[str, int], dict[str, bytes]]:
    """A PNG's header (width, height, bit_depth, color_type) and the color chunks before its image data."""
    header: dict[str, int] = {}
    chunks: dict[str, bytes] = {}
    with path.open("rb") as file:
        if file.read(8) != PNG_SIGNATURE:
            raise ValueError("not a PNG file")
        while True:
            head = file.read(8)
            if len(head) < 8:
                break
            length, kind = struct.unpack(">I4s", head)
            name = kind.decode("latin1")
            if name == "IDAT":
                break
            body = file.read(length)
            file.seek(4, io.SEEK_CUR)
            if name == "IHDR":
                width, height, depth, color_type = struct.unpack(">IIBB", body[:10])
                header = {"width": width, "height": height, "bit_depth": depth, "color_type": color_type}
            elif name in PNG_COLOR_CHUNKS:
                chunks[name] = body
    if not header:
        raise ValueError("a PNG file with no header")
    return header, chunks


def image_facts(path: Path) -> dict[str, Any]:
    """What an image holds, read without decoding its pixels."""
    with Image.open(path) as image:
        facts: dict[str, Any] = {"format": image.format, "width": image.width, "height": image.height, "mode": image.mode, "bit_depth": 16 if image.mode.startswith("I;16") or image.mode == "I" else 8}
        facts["alpha"] = "A" in image.getbands() or "transparency" in image.info
        profile = image.info.get("icc_profile")
        facts["icc_profile"] = _profile_name(profile) if profile else None
        orientation = image.getexif().get(0x0112)
        facts["exif_orientation"] = int(orientation) if orientation else None
    if facts["format"] == "PNG":
        header, chunks = png_chunks(path)
        facts["bit_depth"] = header["bit_depth"]
        facts["color_type"] = PNG_COLOR_TYPES.get(header["color_type"], str(header["color_type"]))
        facts["alpha"] = facts["alpha"] or header["color_type"] in (4, 6)
        facts["png_color_chunks"] = [name for name in PNG_COLOR_CHUNKS if name in chunks]
    return facts


def _profile_name(profile: bytes) -> str:
    # Imported here, so a process that never checks an image does not load LittleCMS (as executor.py does for resizing).
    from PIL import ImageCms

    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(profile))).strip() or "unnamed profile"
    except (ImageCms.PyCMSError, OSError, ValueError):
        return "unreadable profile"


def _image_text(facts: dict[str, Any]) -> str:
    """For example ``JPEG 959x1280, 8-bit RGB, ICC Display P3, no alpha, upright``."""
    kind = facts.get("color_type") or facts["mode"]
    parts = [f"{facts['format']} {facts['width']}x{facts['height']}", f"{facts['bit_depth']}-bit {kind}"]
    if facts.get("icc_profile"):
        parts.append(f"ICC {facts['icc_profile']}")
    chunks = [name for name in facts.get("png_color_chunks", []) if name != "iCCP"]
    if chunks:
        parts.append(f"{', '.join(chunks)} chunk{'s' if len(chunks) > 1 else ''}")
    if not facts.get("icc_profile") and not chunks:
        parts.append("no color profile (read as sRGB)")
    parts.append("alpha" if facts["alpha"] else "no alpha")
    orientation = facts.get("exif_orientation")
    if orientation not in (None, 1):
        parts.append(f"EXIF orientation {orientation}")
    return ", ".join(parts)


def _is_srgb_profile(name: str | None) -> bool:
    return name is not None and name.startswith("sRGB")


def check_input(path: Path) -> MediaCheck:
    """The job's first input. draw-things-cli reads its copy, upright 8-bit sRGB RGB at the job's size (owner
    decision, Milestone 09), so an input that is not plain 8-bit sRGB RGB is a note, not a warning."""
    facts = image_facts(path)
    warnings: list[str] = []
    notes: list[str] = []
    profile = facts.get("icc_profile")
    chunks = facts.get("png_color_chunks", [])
    if facts["mode"] == "CMYK" and not profile:
        warnings.append("It is CMYK with no ICC profile, so its conversion to RGB is a plain formula and colors may shift.")
    if ("gAMA" in chunks or "cHRM" in chunks) and not profile and "sRGB" not in chunks:
        warnings.append("It states its color with gAMA or cHRM only, which is not read: its values are taken as sRGB.")
    if profile and not _is_srgb_profile(profile):
        notes.append(f"The copy converts it from {profile} to sRGB.")
    plain = facts["mode"] == "RGB" and facts["bit_depth"] == 8 and not facts["alpha"] and (not profile or _is_srgb_profile(profile)) and facts.get("exif_orientation") in (None, 1)
    if not plain:
        notes.append("draw-things-cli reads its copy, upright 8-bit sRGB RGB, not the file itself.")
    return MediaCheck("input", str(path), _image_text(facts), tuple(warnings), tuple(notes), facts)


def check_handoff(path: Path) -> MediaCheck:
    """A resume's input, the chain's own last frame, checked as the handoff it is (owner decision), not as a person's
    input: an RGB PNG without alpha, at 8 or 16 bits. A last frame from before Milestone 08 can have alpha."""
    facts = image_facts(path)
    warnings: list[str] = []
    if facts["format"] != "PNG" or facts.get("color_type") != "RGB":
        warnings.append(f"It is {facts['format']} {facts.get('color_type') or facts['mode']}, not an RGB PNG as a last frame is.")
    if facts["alpha"]:
        warnings.append("It has alpha, which Draw Things reads as a mask.")
    return MediaCheck("input", str(path), f"{_image_text(facts)}; the chain's own last frame", tuple(warnings), (), facts)


def check_resized_input(source: Path, copy: Path, plan: ResizePlan, ffmpeg: str | None = None) -> MediaCheck:
    """Run 1's copy: 8-bit RGB PNG at the planned size, and the source's mean color kept, compared in the space the
    resize worked in (linear light when downscaling, sRGB values otherwise), over the picture only, bars left out. The
    source is read as the copy was made (``read_srgb``), so the check also says how its values became sRGB."""
    import numpy as np

    from draw_things_control.jobs.inputs.resize import read_srgb

    facts = image_facts(copy)
    warnings: list[str] = []
    notes: list[str] = []
    if facts["format"] != "PNG" or facts.get("color_type") != "RGB" or facts["bit_depth"] != 8:
        warnings.append(f"It is {facts['bit_depth']}-bit {facts.get('color_type') or facts['mode']}, not 8-bit RGB PNG.")
    if (facts["width"], facts["height"]) != plan.target_size:
        warnings.append(f"It is {facts['width']}x{facts['height']}, not the planned {plan.target_size[0]}x{plan.target_size[1]}.")
    original = image_facts(source)
    values, report = read_srgb(source, ffmpeg)
    height, width = values.shape[:2]
    left, top, right, bottom = plan.box if plan.box is not None else (0.0, 0.0, float(width), float(height))
    picture_width, picture_height = plan.picture_size
    downscaling = max((right - left) / picture_width, (bottom - top) / picture_height) > 1
    source_pixels = values[int(round(top)) : int(round(bottom)), int(round(left)) : int(round(right))]
    with Image.open(copy) as written:
        copy_pixels = np.asarray(written.convert("RGB"), dtype=np.float32) / 255
    x = (plan.target_size[0] - picture_width) // 2
    y = (plan.target_size[1] - picture_height) // 2
    copy_pixels = copy_pixels[y : y + picture_height, x : x + picture_width]
    # One channel at a time, so a large photo needs one more float channel, not a copy of all three.
    before, after = (np.array([_channel_mean(pixels[..., index], downscaling) for index in range(3)]) for pixels in (source_pixels, copy_pixels))
    drift = [round(float(value), 2) for value in after - before]
    facts["mean_color_drift_levels"] = drift
    facts["source"] = _source_facts(report)
    largest = max(abs(value) for value in drift)
    if largest > DRIFT_LEVELS:
        warnings.append(f"Its mean color moved by {', '.join(f'{value:+.1f}' for value in drift)} levels (R, G, B) from the source's, more than {DRIFT_LEVELS:.0f}.")
    _source_messages(report, warnings, notes)
    mean_color = f"mean color moved {largest:.1f} levels" if largest > DRIFT_LEVELS else f"mean color kept within {largest:.1f} levels"
    summary = f"{_image_text(facts)}; {_conversion_text(original, report)}; {plan.fit} to {plan.target_size[0]}x{plan.target_size[1]}; {mean_color}"
    return MediaCheck("resized_input", copy.name, summary, tuple(warnings), tuple(notes), facts)


def _channel_mean(channel: Any, linear: bool) -> float:
    """A channel's mean in 8-bit sRGB levels, averaged in linear light when ``linear``."""
    import numpy as np

    from draw_things_control.jobs.media import oklab

    if not linear:
        return float(np.mean(channel, dtype=np.float64)) * 255
    return float(_srgb_level(np.mean(oklab.srgb_to_linear(channel, channel.dtype), dtype=np.float64)))


def _source_facts(report: Any) -> dict[str, Any]:
    gamut = report.gamut
    return {
        "profile": report.profile,
        "conversion": report.conversion,
        "sixteen_bit": report.sixteen_bit,
        "gamut": None if gamut is None else {"beyond_knee": gamut.beyond_knee, "largest_chroma_reduction": round(gamut.largest_reduction, 4), "onto_edge": gamut.onto_edge},
    }


def _source_messages(report: Any, warnings: list[str], notes: list[str]) -> None:
    """What the reading of the source did that the owner should know."""
    if report.sixteen_bit == "high_bytes":
        notes.append("ffmpeg was not found, so its 16-bit samples were read by their high bytes, about half a level dark.")
    if report.conversion == "littlecms":
        notes.append(f"{report.profile} is not a matrix-and-curves profile: LittleCMS converted it with the perceptual intent, at 8 bits, and clipped what was still outside sRGB.")
    elif report.conversion == "failed":
        warnings.append(f"Its profile ({report.profile}) could not be used, so its values were taken as sRGB and colors may shift.")
    gamut = report.gamut
    if gamut is not None and gamut.beyond_knee:
        notes.append(f"{gamut.beyond_knee} pixels beyond 90% of the sRGB edge's chroma were brought in at constant lightness and hue; the largest chroma reduction was {gamut.largest_reduction:.3f} (Oklab).")


def _srgb_level(linear: Any) -> Any:
    import numpy as np

    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055) * 255


def _conversion_text(original: dict[str, Any], report: Any) -> str:
    """What the copy did to the source's color."""
    steps: list[str] = []
    profile = original.get("icc_profile")
    if original.get("mode") in ("YCbCr", "HSV"):
        steps.append(f"{original['mode']} converted to RGB")
    if report.conversion == "lab":
        steps.append("Lab converted to sRGB" + (f", {profile} not needed" if profile else ""))
    elif report.conversion == "matrix":
        steps.append(f"converted from {profile} to sRGB with gamut mapping")
    elif report.conversion == "littlecms":
        steps.append(f"converted from {profile} to sRGB by LittleCMS")
    elif report.conversion == "failed":
        steps.append(f"{profile} not usable, values kept as sRGB")
    elif profile:
        steps.append("sRGB profile, values kept")
    else:
        steps.append("no profile, values kept as sRGB")
    if report.sixteen_bit == "ffmpeg":
        steps.append("16-bit read by ffmpeg and rounded to 8")
    elif report.sixteen_bit == "high_bytes":
        steps.append("16-bit read by its high bytes")
    elif original["bit_depth"] > 8:
        steps.append(f"{original['bit_depth']}-bit scaled to 8")
    if original["alpha"]:
        steps.append("transparency flattened onto black")
    if original.get("exif_orientation") not in (None, 1):
        steps.append("turned upright")
    return ", ".join(steps)


def check_last_frame(png: Path, video: VideoProbe | None, color: StreamColor) -> MediaCheck:
    """The last frame: 16-bit RGB without alpha, labeled sRGB, the video's size (the handoff from every source)."""
    facts = image_facts(png)
    warnings: list[str] = []
    chunks = facts.get("png_color_chunks", [])
    if facts["alpha"]:
        warnings.append("It has alpha, which Draw Things reads as a mask when the frame starts the next run.")
    if "sRGB" not in chunks and "iCCP" not in chunks:
        warnings.append("It is not labeled sRGB, so viewers guess its colors.")
    if facts["bit_depth"] != 16:
        warnings.append(f"It is {facts['bit_depth']}-bit, but the handoff is 16-bit from every source.")
    if video is not None:
        if (facts["width"], facts["height"]) != (video.width, video.height):
            warnings.append(f"It is {facts['width']}x{facts['height']}, but the video is {video.width}x{video.height}.")
    summary = f"{_image_text(facts)}; decoded from {color.text()}"
    facts["decoded_matrix"], facts["decoded_range"] = color.matrix or "bt709", color.decode_range
    return MediaCheck("last_frame", png.name, summary, tuple(warnings), (), facts)


# Videos


def probe_video(video: Path, ffprobe: str | None, run: CommandRunner = subprocess.run) -> VideoProbe:
    """One ffprobe call: the first video stream, and the color its first frame states. The frame is read rather than
    the stream because ffmpeg decodes with the frame's: once a ``colr`` box is added, the stream reports the box."""
    if ffprobe is None:
        raise ValueError("ffprobe was not found")
    entries = "stream=codec_name,codec_tag_string,profile,width,height,pix_fmt,nb_frames:frame=color_space,color_range,color_primaries,color_transfer"
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_entries", entries, "-of", "json", str(video)]
    try:
        result = run(command, capture_output=True, text=True, errors="replace", check=False, timeout=PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"ffprobe could not read it: {error}") from error
    if result.returncode != 0:
        raise ValueError(f"ffprobe could not read it: {result.stderr.strip() or f'exit code {result.returncode}'}")
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
    except (ValueError, KeyError, IndexError) as error:
        raise ValueError("ffprobe found no video stream") from error
    frame = (data.get("frames") or [{}])[0]

    def stated(key: str) -> str | None:
        value = frame.get(key)
        return None if value in UNSTATED else str(value)

    frames = stream.get("nb_frames")
    return VideoProbe(
        codec=stream.get("codec_name"),
        tag=stream.get("codec_tag_string") if stream.get("codec_tag_string") not in UNSTATED | {"[0][0][0][0]"} else None,
        profile=stream.get("profile"),
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        pix_fmt=stream.get("pix_fmt"),
        frames=int(frames) if str(frames).isdigit() else None,
        color_space=stated("color_space"),
        color_range=stated("color_range"),
        color_primaries=stated("color_primaries"),
        color_transfer=stated("color_transfer"),
    )


def check_output(video: Path, requested_format: str | None, written: VideoProbe, color: StreamColor, ffmpeg: str | None, run: CommandRunner = subprocess.run) -> MediaCheck:
    """The run's video: the format it was asked for, what its stream states, the ``colr`` box, and whether its pixels
    were encoded with the matrix it states. ``written`` is read before the tagger ran; ``color`` is what it is decoded with."""
    warnings: list[str] = []
    notes: list[str] = []
    facts: dict[str, Any] = written.facts()
    expected = VIDEO_FORMATS.get(requested_format or "")
    if expected is not None and (written.codec != expected[0] or (expected[1] is not None and written.tag != expected[1])):
        warnings.append(f"--video-format {requested_format} was asked for, but it is {written.codec_text}.")
    parts = [written.codec_text, f"{written.width}x{written.height}"]
    if written.frames is not None:
        parts.append(f"{written.frames} frames")
    parts.append(written.pix_fmt or "unknown pixel format")
    summary = ", ".join(parts)
    stated = "; stream states " + ", ".join(f"{label} {value or '-'}" for label, value in (("matrix", written.color_space), ("range", written.color_range), ("primaries", written.color_primaries), ("transfer", written.color_transfer)))
    summary += stated
    # When the pixels measured the matrix, the note under the colr box or the summary's "decoded as" says so instead.
    if written.color_space is None and not color.measured:
        notes.append(f"Its stream states no matrix, so it is decoded and tagged as BT.709 {RANGES[color.decode_range]}, Draw Things' measured encoding.")
    colr = read_colr(video)
    facts["colr"] = None if colr is None else {"kind": colr.kind, "primaries": colr.primaries, "transfer": colr.transfer, "matrix": colr.matrix, "full_range": colr.full_range}
    summary += f"; colr {_colr_text(colr)}"
    if colr is None:
        warnings.append("It has no colr box, so players guess its colors.")
    else:
        box_matrix = H273_MATRICES.get(colr.matrix)
        contradicts = written.color_space is not None and box_matrix is not None and SAME_MATRIX.get(box_matrix) != SAME_MATRIX.get(written.color_space)
        if contradicts and color.measured and SAME_MATRIX.get(box_matrix or "") == SAME_MATRIX.get(color.matrix or "bt709"):
            # The tagger wrote what the pixels measure, over the stream's wrong statement.
            notes.append(f"Its frames state {written.color_space}, but its pixels were encoded with {color.matrix}, so it is decoded and tagged as {color.matrix}; a reader of the frame header (ffmpeg) still sees {written.color_space}.")
        elif contradicts:
            warnings.append(f"The colr box states {box_matrix}, but the stream states {written.color_space}: a player may use either, and ffmpeg uses the stream's for ProRes.")
    fingerprint = _fingerprint(video, written, color, ffmpeg, run, notes)
    if fingerprint is not None:
        facts["matrix_scores"] = {name: round(score, 4) for name, score in fingerprint.scores.items()}
        measured = fingerprint.matrix
        facts["measured_matrix"] = measured
        if fingerprint.looks_like_noise:
            summary += f"; pixels show no 8-bit structure ({fingerprint.text()})"
            warnings.append("Its pixels show no 8-bit structure for any matrix, which is what noise gives: the frames may be noise.")
        elif measured is None:
            summary += f"; pixels inconclusive ({fingerprint.text()})"
            notes.append("Its pixels do not tell which matrix encoded them.")
        else:
            summary += f"; pixels encoded {measured} ({fingerprint.text()})"
            if SAME_MATRIX.get(color.matrix or "bt709") != measured:
                warnings.append(f"It is decoded as {color.matrix or 'bt709'}, but its pixels were encoded with {measured}.")
    summary += f"; decoded as {color.text()}"
    facts["decoded_matrix"], facts["decoded_range"] = color.matrix or "bt709", color.decode_range
    return MediaCheck("output", video.name, summary, tuple(warnings), tuple(notes), facts)


def _colr_text(colr: ColrTag | None) -> str:
    if colr is None:
        return "none"
    names = "/".join(H273_NAMES.get(code, str(code)) for code in (colr.primaries, colr.transfer, colr.matrix))
    return f"{colr.text()} ({names})"


def _fingerprint(video: Path, written: VideoProbe, color: StreamColor, ffmpeg: str | None, run: CommandRunner, notes: list[str]) -> MatrixFingerprint | None:
    if not can_measure(written.pix_fmt):
        notes.append(f"Its pixels are not measured: {written.pix_fmt or 'its pixel format'} is not 4:4:4 at 10 bits or more.")
        return None
    # The measurement resolve_video_color made before tagging, of the same frames; measured again only without it.
    if color.fingerprint is not None:
        return color.fingerprint
    if ffmpeg is None or written.width <= 0 or written.height <= 0:
        notes.append("Its pixels are not measured: ffmpeg was not found.")
        return None
    try:
        return measure_matrix(video, ffmpeg, written.width, written.height, full_range=written.color_range == "pc", run=run)
    except ValueError as error:
        notes.append(f"Its pixels are not measured: {error}.")
        return None
