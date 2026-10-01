"""The frames of a Draw Things video as floating-point sRGB, and the PNGs this tool wrote read as draw-things-cli reads them.

draw-things-cli truncates every value it writes (``Int((v + 1) * 127.5)``), so a decoded level ``e`` stands for ``e``
to ``e + 1``: every frame read here is raised by the half level Draw Things truncated, ``0.5 * min(1, e)`` in 8-bit
sRGB units, tapered to nothing at black as the handoff's is (``frames.HANDOFF``). ffmpeg's 16-bit RGB is about 256
times the 8-bit level (white is 65283), so ``e`` is the sample / 256, exactly as the handoff takes it. A PNG this tool
wrote (a handoff, the first image) is read as draw-things-cli reads it, an 8-bit value or a 16-bit sample's high byte,
and is not raised.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from draw_things_control.jobs.media.frames import decode_filter, handoff_samples
from draw_things_control.jobs.media.stream_color import StreamColor

PROBE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ClipInfo:
    """What a video holds, for decoding its frames and encoding a copy of it."""

    width: int
    height: int
    frames: int | None
    # The frame rate as ffprobe gives it, such as ``16/1``.
    rate: str
    bit_rate: int | None
    codec: str | None
    tag: str | None

    @property
    def fps(self) -> float:
        return float(Fraction(self.rate)) if self.rate and self.rate != "0/0" else 0.0


def probe_clip(video: Path, ffprobe: str) -> ClipInfo:
    """One ffprobe call for the first video stream; raises ValueError when it cannot be read."""
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_frames,r_frame_rate,bit_rate,codec_name,codec_tag_string", "-of", "json", str(video)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False, timeout=PROBE_TIMEOUT_SECONDS)
        stream = json.loads(result.stdout)["streams"][0] if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as error:
        raise ValueError(f"ffprobe could not read {video.name}: {error}") from error
    if stream is None:
        raise ValueError(f"ffprobe could not read {video.name}: {result.stderr.strip() or f'exit code {result.returncode}'}")
    frames, bit_rate = str(stream.get("nb_frames", "")), str(stream.get("bit_rate", ""))
    return ClipInfo(
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        frames=int(frames) if frames.isdigit() else None,
        rate=str(stream.get("r_frame_rate") or "0/0"),
        bit_rate=int(bit_rate) if bit_rate.isdigit() else None,
        codec=stream.get("codec_name"),
        tag=stream.get("codec_tag_string"),
    )


def decoded_levels(samples: np.ndarray) -> np.ndarray:
    """ffmpeg's 16-bit RGB samples as 8-bit levels raised by the half level Draw Things truncated (float32)."""
    level = samples.astype(np.float32) / 256
    return level + 0.5 * np.minimum(level, 1.0)


def iter_frames(video: Path, color: StreamColor, ffmpeg: str, size: tuple[int, int], *, deadline: float | None = None) -> Generator[np.ndarray]:
    """Each frame of ``video``, decoded with ``color`` as the last frame is, as sRGB values (H, W, 3, float32, 1.0 for
    white) raised by the truncated half level. One frame is in memory at a time. Raises ValueError when ffmpeg fails,
    or when ``deadline`` (a ``time.monotonic`` value) passes; ffmpeg is stopped whenever the caller stops reading."""
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError(f"{video.name} has no frame size")
    frame_bytes = width * height * 6
    command = [ffmpeg, "-v", "error", "-nostdin", "-i", str(video), "-vf", decode_filter(color), "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    # ffmpeg's errors go to a file, since a pipe read only at the end fills and stalls it; and a timer kills ffmpeg at the
    # deadline, since a read it never answers would otherwise wait past it.
    with tempfile.TemporaryFile() as errors_file:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors_file)
        assert process.stdout is not None
        timer = threading.Timer(max(deadline - time.monotonic(), 0.0), process.kill) if deadline is not None else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        finished = False
        try:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    raise ValueError(f"reading {video.name} ran out of time")
                data = process.stdout.read(frame_bytes)
                if len(data) < frame_bytes:
                    break
                yield decoded_levels(np.frombuffer(data, dtype="<u2").reshape(height, width, 3)) / 255
            finished = True
        finally:
            if timer is not None:
                timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            errors_file.seek(0)
            errors = errors_file.read().decode(errors="replace").strip()
    if finished and deadline is not None and time.monotonic() > deadline:
        raise ValueError(f"reading {video.name} ran out of time")
    if finished and process.returncode != 0:
        raise ValueError(f"ffmpeg could not decode {video.name}: {errors or f'exit code {process.returncode}'}")


def read_tool_png(path: Path) -> np.ndarray:
    """A PNG this tool wrote, as sRGB values (H, W, 3, float32) read the way draw-things-cli reads it: an 8-bit value
    as it is, a 16-bit sample by its high byte (Pillow opens 16-bit RGB keeping each sample's high byte, as swift-png's
    ``>> 8`` does), with no half level added."""
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.float32) / 255


# The corrected copy is encoded in the original's format (owner decision, Milestone 09). Each format's encoder, the
# pixel format it is fed, and its options: VideoToolbox first, and for ProRes, prores_ks when VideoToolbox is missing
# or cannot encode here (``encoder_works``).
# The frame headers are then set by a bitstream filter, since VideoToolbox drops the primaries and the transfer: BT.709
# primaries and matrix and limited range, and the sRGB transfer where the format has one; ProRes has none (only
# unknown, BT.709, PQ, and HLG), so its header leaves the transfer unknown and the colr box states it (owner decision).
PRORES_HEADER = "prores_metadata=color_primaries=bt709:colorspace=bt709"
H264_HEADER = "h264_metadata=colour_primaries=1:transfer_characteristics=13:matrix_coefficients=1:video_full_range_flag=0"
HEVC_HEADER = "hevc_metadata=colour_primaries=1:transfer_characteristics=13:matrix_coefficients=1:video_full_range_flag=0"
ENCODERS: dict[str, tuple[tuple[str, str, tuple[str, ...], str], ...]] = {
    "ap4h": (("prores_videotoolbox", "p416le", ("-profile:v", "4444"), PRORES_HEADER), ("prores_ks", "yuv444p10le", ("-profile:v", "4444"), PRORES_HEADER)),
    "apch": (("prores_videotoolbox", "p216le", ("-profile:v", "hq"), PRORES_HEADER), ("prores_ks", "yuv422p10le", ("-profile:v", "hq"), PRORES_HEADER)),
    "h264": (("h264_videotoolbox", "nv12", (), H264_HEADER),),
    "hevc": (("hevc_videotoolbox", "nv12", ("-tag:v", "hvc1"), HEVC_HEADER),),
}
ENCODE_FLAGS = "accurate_rnd+full_chroma_int"


@dataclass(frozen=True)
class EncoderChoice:
    """How the corrected copy is encoded."""

    encoder: str
    pixel_format: str
    options: tuple[str, ...]
    header: str
    # Encoders this ffmpeg lists that were passed over, since they could not encode here.
    skipped: tuple[str, ...] = ()


def available_encoders(ffmpeg: str) -> set[str]:
    """The encoder names this ffmpeg lists."""
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace", check=False, timeout=PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


def choose_encoder(original: ClipInfo, available: set[str], works: Callable[[EncoderChoice], bool] | None = None) -> EncoderChoice:
    """The encoder for a copy of ``original``, in its format; raises ValueError when this ffmpeg has none for it.
    ``works`` is asked about each listed encoder that has another after it: VideoToolbox is listed where it cannot open
    a hardware session (headless, or in a virtual machine), so listed is not enough (owner decision)."""
    key = original.tag if original.tag in ENCODERS else original.codec
    candidates = ENCODERS.get(key or "")
    if candidates is None:
        raise ValueError(f"no encoder is set for {original.tag or original.codec or 'its format'}")
    listed = [candidate for candidate in candidates if candidate[0] in available]
    if not listed:
        raise ValueError(f"ffmpeg has none of {', '.join(candidate[0] for candidate in candidates)}")
    skipped: tuple[str, ...] = ()
    for encoder, pixel_format, options, header in listed[:-1]:
        choice = _encoder_choice(original, encoder, pixel_format, options, header, skipped)
        if works is None or works(choice):
            return choice
        skipped += (encoder,)
    return _encoder_choice(original, *listed[-1], skipped)


def _encoder_choice(original: ClipInfo, encoder: str, pixel_format: str, options: tuple[str, ...], header: str, skipped: tuple[str, ...]) -> EncoderChoice:
    bit_rate = ("-b:v", str(original.bit_rate)) if encoder.endswith("_videotoolbox") and not encoder.startswith("prores") and original.bit_rate else ()
    return EncoderChoice(encoder, pixel_format, options + bit_rate, header, skipped)


class ClipEncoder:
    """Writes the corrected copy: floating-point sRGB frames rounded to 8-bit levels as the handoff is (the ordered
    dither; the values already carry the half level), piped to ffmpeg as 16-bit RGB, each sample the level times 256,
    and converted with the BT.709 matrix, named, in limited range. ffmpeg's default RGB-to-YCbCr matrix is BT.601, so it
    is always named. Rounded to 8 bits, the copy holds what Draw Things' own files hold, 8-bit values in 12-bit ProRes:
    its last frame is the handoff's values, and its pixels keep the 8-bit structure that measures its matrix (measured:
    times 256 scores BT.709 0.134 against 0.250; times 257, or unrounded values, show no matrix). ``abort`` stops it and
    deletes what it wrote. ``deadline`` (a ``time.monotonic`` value) stops ffmpeg when it passes, so a write it never
    reads cannot wait past it."""

    def __init__(self, ffmpeg: str, output: Path, original: ClipInfo, choice: EncoderChoice, *, deadline: float | None = None) -> None:
        if output.exists():
            raise ValueError(f"{output.name} already exists")
        self.output = output
        self.choice = choice
        size = f"{original.width}x{original.height}"
        convert = f"scale=out_color_matrix=bt709:out_range=tv:flags={ENCODE_FLAGS},format={choice.pixel_format}"
        command = [ffmpeg, "-v", "error", "-nostdin", "-n", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", size, "-r", original.rate, "-i", "-", "-vf", convert, "-c:v", choice.encoder, *choice.options, "-bsf:v", choice.header, str(output)]
        # ffmpeg's errors go to a file, as iter_frames' do, since a pipe read only at the end fills and stalls it.
        self._errors_file = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._errors_file)
        except BaseException:
            self._errors_file.close()
            raise
        self._timed_out = False
        self._timer = threading.Timer(max(deadline - time.monotonic(), 0.0), self._time_out) if deadline is not None else None
        if self._timer is not None:
            self._timer.daemon = True
            self._timer.start()
        self.frames = 0

    def _time_out(self) -> None:
        self._timed_out = True
        self._process.kill()

    def write(self, frame: np.ndarray) -> None:
        assert self._process.stdin is not None
        samples = (handoff_samples(np.asarray(frame, dtype=np.float64) * 255) & 0xFF00).astype("<u2")
        try:
            self._process.stdin.write(samples.tobytes())
        except BrokenPipeError as error:
            if self._timed_out:
                raise ValueError(f"encoding {self.output.name} ran out of time") from error
            raise ValueError(f"ffmpeg stopped encoding {self.output.name}: {self._errors()}") from error
        self.frames += 1

    def close(self, deadline: float | None = None) -> None:
        """Finish the file; raises ValueError, deleting it, when ffmpeg fails or runs past ``deadline``."""
        assert self._process.stdin is not None
        try:
            try:
                self._process.stdin.close()
            except BrokenPipeError:
                # ffmpeg is gone; its exit code says why, below.
                pass
            timeout = max(deadline - time.monotonic(), 0.1) if deadline is not None else None
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.abort()
            raise ValueError(f"encoding {self.output.name} ran out of time") from error
        if self._timer is not None:
            self._timer.cancel()
        if self._timed_out:
            self.abort()
            raise ValueError(f"encoding {self.output.name} ran out of time")
        if self._process.returncode != 0 or not self.output.is_file():
            errors = self._errors()
            self.abort()
            raise ValueError(f"ffmpeg could not encode {self.output.name}: {errors or f'exit code {self._process.returncode}'}")
        self._errors_file.close()

    def abort(self) -> None:
        """Stop ffmpeg and delete a partial file; safe to call more than once."""
        if self._timer is not None:
            self._timer.cancel()
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait()
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except BrokenPipeError:
                # Data still buffered for the killed ffmpeg; the file is deleted all the same.
                pass
        self._errors_file.close()
        self.output.unlink(missing_ok=True)

    def _errors(self) -> str:
        if self._errors_file.closed or self._process.poll() is None:
            return ""
        self._errors_file.seek(0)
        return self._errors_file.read().decode(errors="replace").strip()


# What each probe found, for this process: by ffmpeg, encoder, pixel format, options, and size.
_ENCODER_WORKS: dict[tuple[str, str, str, tuple[str, ...], int, int], bool] = {}


def encoder_works(ffmpeg: str, original: ClipInfo, choice: EncoderChoice, suffix: str, deadline: float | None = None) -> bool:
    """Whether ``choice`` encodes one black frame of ``original``'s size into a ``suffix`` file, as ``ClipEncoder``
    encodes the copy. Kept for the process; a probe that runs out of time (``PROBE_TIMEOUT_SECONDS``, or ``deadline``
    first, so the correction's own limit holds) or cannot start ffmpeg counts as not working this once, and is not kept."""
    key = (ffmpeg, choice.encoder, choice.pixel_format, choice.options, original.width, original.height)
    known = _ENCODER_WORKS.get(key)
    if known is not None:
        return known
    limit = time.monotonic() + PROBE_TIMEOUT_SECONDS
    if deadline is not None:
        limit = min(limit, deadline)
    with tempfile.TemporaryDirectory(prefix="draw-things-control-") as directory:
        try:
            encoder = ClipEncoder(ffmpeg, Path(directory) / f"probe{suffix}", original, choice, deadline=limit)
        except OSError:
            return False
        try:
            encoder.write(np.zeros((original.height, original.width, 3), dtype=np.float32))
            encoder.close(limit)
        except ValueError:
            encoder.abort()
            if time.monotonic() >= limit:
                return False
            _ENCODER_WORKS[key] = False
            return False
    _ENCODER_WORKS[key] = True
    return True
