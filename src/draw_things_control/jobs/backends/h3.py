"""H3 image-to-audio-video adapter.

All installation-specific paths live in :class:`H3Runtime`; the job file only
contains portable generation settings.  This deliberately does not reuse the
Draw Things colour/tagging or frame-handoff implementation.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from draw_things_control.core.errors import InputError
from draw_things_control.core.numbers import is_int, is_number
from draw_things_control.jobs.backend import H3_BACKEND, OutputPaths, RunContext, RunPreparation
from draw_things_control.jobs.definition import GenerationMode, JobDefinition
from draw_things_control.jobs.inputs.size import ResizePlan, decode_image, read_image_info
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.overrides import MAX_SEED

if TYPE_CHECKING:
    from draw_things_control.jobs.events import RunStatus
    from draw_things_control.jobs.parsing import JobParser
    from draw_things_control.jobs.planning import PlannedRun
    from draw_things_control.jobs.records import RunRecord

H3_FPS = 24
H3_INFO_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class H3Runtime:
    executable: Path
    model_directory: Path


@dataclass(frozen=True)
class H3Settings:
    width: int
    height: int
    frames: int
    seconds: float
    steps: int
    seed: int
    effective_frames: int

    @property
    def effective_seconds(self) -> float:
        return self.effective_frames / H3_FPS


@dataclass(frozen=True)
class H3Arguments:
    executable: Path
    model_directory: Path
    prompt: str
    first_frame: Path
    output: Path
    settings: H3Settings
    ffmpeg: str
    ffprobe: str

    @property
    def cwd(self) -> Path:
        """The H3 source directory, which contains its runtime Metal shader."""
        return self.executable.parent

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.executable), "-d", str(self.model_directory), "-p", self.prompt, "--first-frame", str(self.first_frame), "--width", str(self.settings.width), "--height", str(self.settings.height), "--seconds", _number(self.settings.seconds), "--steps", str(self.settings.steps), "--seed", str(self.settings.seed), "-o", str(self.output))

    @property
    def environment(self) -> dict[str, str]:
        # h3.c uses posix_spawnp for media utilities.  Put only the resolved
        # pair first and discard its own environment overrides.
        environment = dict(os.environ)
        environment.pop("H3_FFMPEG", None)
        environment.pop("H3_FFPROBE", None)
        environment["PATH"] = os.pathsep.join((str(Path(self.ffmpeg).parent), str(Path(self.ffprobe).parent), environment.get("PATH", "")))
        return environment


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def effective_frames(frames: int) -> int:
    return min(362, 5 + 17 * math.ceil(max(0, frames - 5) / 17))


def _round_half_away(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


class H3Backend:
    id = H3_BACKEND
    supported_modes = frozenset({"i2av"})

    def __init__(self, runtime: H3Runtime) -> None:
        self.runtime = runtime
        self._ffmpeg: str | None = None
        self._ffprobe: str | None = None

    def parse_job(self, parser: JobParser, data: dict[str, Any], source_text: str) -> JobDefinition:
        common = parser.parse_common(data)
        if common.mode is not GenerationMode.I2AV:
            parser.fail("mode", "is not supported by h3")
        for key in ("config_file", "config_override", "color", "desired_input_width", "desired_input_height", "max_input_crop_percent"):
            if key in data:
                parser.fail(key, "is not supported by h3")
        for index, pair in enumerate(data["prompt_pairs"]):
            if isinstance(pair, dict) and "negative" in pair:
                parser.fail(f"prompt_pairs[{index}].negative", "is not supported by h3")
        settings = self._settings(parser, data.get("backend_settings"))
        output = data.get("output", {})
        directory = parser.output_directory(output, common.name, {"directory", "extension"})
        extension = output.get("extension", "mp4")
        if extension != "mp4":
            parser.fail("output.extension", "must be mp4 for h3")
        # H3 always receives an exact 8-bit sRGB PNG.  A centre crop preserves
        # aspect ratio; it never letterboxes or stretches.
        assert common.input is not None
        if common.input.is_file():
            image_size, orientation = read_image_info(common.input)
            if parser.decode_input:
                decode_image(common.input)
            plan = _centre_crop_plan(image_size, orientation, settings.width, settings.height)
        elif parser.allow_missing_input:
            # A resume starts from its saved verified handoff, never this path.
            plan = ResizePlan(settings.width, settings.height, None, (settings.width, settings.height), None, (settings.width, settings.height), "none", (settings.width, settings.height), None)
        else:
            parser.fail("input", f"file does not exist: {common.input}")
        return JobDefinition(path=parser.path, name=common.name, mode=common.mode, input=common.input, run_count=common.run_count, prompt_pairs=common.prompt_pairs, output_directory=directory.resolve(), extension="mp4", run_timeout_seconds=common.run_timeout_seconds, requested_seed=settings.seed, requested_seed_source="backend_settings", backend=self.id, backend_data=settings, input_resize=plan, cooldown=common.cooldown, cooldown_source=common.cooldown_source, source_text=source_text)

    @staticmethod
    def _settings(parser: JobParser, value: Any) -> H3Settings:
        required = {"width", "height", "frames", "seconds", "steps", "seed"}
        if not isinstance(value, dict):
            parser.fail("backend_settings", "must be a mapping")
        if set(value) != required:
            missing, extra = required - set(value), set(value) - required
            parser.fail("backend_settings", f"must contain exactly {', '.join(sorted(required))}{'; missing ' + ', '.join(sorted(missing)) if missing else ''}{'; unknown ' + ', '.join(sorted(extra)) if extra else ''}")
        width, height, frames, steps, seed = (value[key] for key in ("width", "height", "frames", "steps", "seed"))
        if not all(is_int(item) for item in (width, height, frames, steps, seed)):
            parser.fail("backend_settings", "width, height, frames, steps, and seed must be integers")
        assert is_int(width) and is_int(height) and is_int(frames) and is_int(steps) and is_int(seed)
        if width < 32 or width % 32:
            parser.fail("backend_settings.width", "must be an integer at least 32 and divisible by 32")
        if height < 32 or height % 32:
            parser.fail("backend_settings.height", "must be an integer at least 32 and divisible by 32")
        if width * height > 768 * 1344:
            parser.fail("backend_settings", "width * height must not exceed 768 * 1344")
        if not 2 <= steps <= 1000:
            parser.fail("backend_settings.steps", "must be an integer from 2 to 1000")
        if not 0 <= seed <= MAX_SEED:
            parser.fail("backend_settings.seed", f"must be an integer from 0 to {MAX_SEED}")
        seconds = value["seconds"]
        if not is_number(seconds) or not math.isfinite(float(seconds)) or not 1 <= float(seconds) <= 15:
            parser.fail("backend_settings.seconds", "must be a finite number from 1 to 15")
        seconds = float(seconds)
        if not 24 <= frames <= 360:
            parser.fail("backend_settings.frames", "must be an integer from 24 to 360")
        if frames != _round_half_away(seconds * H3_FPS):
            parser.fail("backend_settings.frames", "must equal llround(seconds * 24)")
        return H3Settings(width, height, frames, seconds, steps, seed, effective_frames(frames))

    def safe_settings(self, job: JobDefinition) -> dict[str, Any]:
        value = cast(H3Settings, job.backend_data)
        return {"backend": self.id, "width": value.width, "height": value.height, "requested_frames": value.frames, "requested_seconds": value.seconds, "effective_frames": value.effective_frames, "effective_seconds": value.effective_seconds, "steps": value.steps, "seed": value.seed, "seed_source": "backend_settings", "output_extension": "mp4"}

    def snapshot_config(self, job: JobDefinition, params_directory: Path, max_bytes: int) -> str:
        return ""

    def finishing_seconds(self, job: JobDefinition) -> float:
        return 30.0

    def check_tools(self, job: JobDefinition, executable: str, media: MediaTools, find_executable: Callable[[str], str | None]) -> None:
        if os.environ.get("H3_FFMPEG") or os.environ.get("H3_FFPROBE"):
            raise InputError("H3_FFMPEG and H3_FFPROBE must not be set for h3 jobs")
        if not self.runtime.executable.is_file() or not os.access(self.runtime.executable, os.X_OK):
            raise InputError(f"H3 executable is missing or not executable: {self.runtime.executable}")
        if not self.runtime.model_directory.is_dir():
            raise InputError(f"H3 model directory is missing: {self.runtime.model_directory}")
        ffmpeg = find_executable("ffmpeg")
        ffprobe = find_executable("ffprobe")
        if ffmpeg is None:
            raise InputError("ffmpeg is missing for h3")
        if ffprobe is None:
            raise InputError("ffprobe is missing for h3")
        self._ffmpeg, self._ffprobe = ffmpeg, ffprobe
        try:
            result = subprocess.run((str(self.runtime.executable), "-d", str(self.runtime.model_directory), "--info"), capture_output=True, text=True, errors="replace", timeout=H3_INFO_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise InputError(f"H3 installation probe failed: {error}") from error
        if result.returncode != 0:
            raise InputError(f"H3 installation probe failed for model directory {self.runtime.model_directory}: {result.stderr.strip() or 'exit code ' + str(result.returncode)}")

    def output_paths(self, job: JobDefinition, output: Path) -> OutputPaths:
        return OutputPaths(output.with_name(f"{output.stem}-last-frame.png"))

    def child_output(self, run: PlannedRun) -> Path:
        return _partial_path(run.output)

    def arguments(self, job: JobDefinition, pair: Any, run_input: Path | None, output: Path, seed: int, executable: str) -> H3Arguments:
        if run_input is None:
            raise ValueError("H3 i2av requires a first frame")
        settings = cast(H3Settings, job.backend_data)
        # The job seed is fixed by validated H3 settings; an input seed supplied
        # by the common chain must agree, including resume.
        if seed != settings.seed:
            raise ValueError("H3 chain seed differs from backend_settings.seed")
        if self._ffmpeg is None or self._ffprobe is None:
            raise ValueError("H3 media tools were not resolved")
        return H3Arguments(self.runtime.executable, self.runtime.model_directory, pair.positive, run_input, _partial_path(output), settings, self._ffmpeg, self._ffprobe)

    def prepare_run(self, job: JobDefinition, context: RunContext) -> RunPreparation:
        return RunPreparation(chain_anchor=context.chain_anchor)

    def redact(self, command: Sequence[str]) -> tuple[str, ...]:
        return tuple("<h3>" if token == str(self.runtime.executable) else "<h3-model>" if token == str(self.runtime.model_directory) else "<media-tool>" if Path(token).name in {"ffmpeg", "ffprobe"} else token for token in command)

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None], finish_state: object | None, media: MediaTools, report: Callable[[int, MediaCheck], None] | None) -> tuple[RunStatus, int] | None:
        partial = _partial_path(run.output)
        try:
            if stop_requested() is not None:
                raise ValueError("cancelled while verifying H3 output")
            if self._ffmpeg is None or self._ffprobe is None:
                raise ValueError("H3 media tools were not resolved")
            facts = _verify_h3_mp4(partial, cast(H3Settings, job.backend_data), self._ffprobe)
            if stop_requested() is not None:
                raise ValueError("cancelled while verifying H3 output")
            os.replace(partial, run.output)
            handoff = run.last_frame
            if handoff is not None:
                _extract_h3_frame(run.output, handoff, self._ffmpeg)
                decode_image(handoff)
                record.last_frame = handoff.name
            record.output_width, record.output_height, record.output_frames = facts["width"], facts["height"], facts["frames"]
            record.output_frame_rate, record.output_duration = facts["frame_rate"], facts["video_duration"]
            record.audio_codec, record.audio_sample_rate, record.audio_channels, record.audio_duration = facts["audio_codec"], facts["audio_sample_rate"], facts["audio_channels"], facts["audio_duration"]
            if report is not None:
                report(run.number, MediaCheck("output", run.output.name, "H3 MP4 video and audio verified", facts=facts))
            return None
        except (OSError, ValueError):
            partial.unlink(missing_ok=True)
            run.output.unlink(missing_ok=True)
            if run.last_frame is not None:
                run.last_frame.unlink(missing_ok=True)
            from draw_things_control.jobs.events import RunStatus

            return (RunStatus.FAILED, 1)


def _partial_path(output: Path) -> Path:
    # The final output name is already reservation-unique. Its hidden sibling
    # is therefore unique too, without creating a file during preview.
    return output.with_name(f".{output.name}.partial.mp4")


def _centre_crop_plan(size: tuple[int, int], orientation: int | None, width: int, height: int) -> ResizePlan:
    source_width, source_height = size
    scale = max(width / source_width, height / source_height)
    crop_width, crop_height = width / scale, height / scale
    left, top = (source_width - crop_width) / 2, (source_height - crop_height) / 2
    scaled = (round(source_width * scale), round(source_height * scale))
    crop = 100 * (1 - crop_width * crop_height / (source_width * source_height))
    return ResizePlan(width, height, None, size, orientation, (width, height), "crop", scaled, crop, (left, top, left + crop_width, top + crop_height))


def _verify_h3_mp4(path: Path, settings: H3Settings, ffprobe: str) -> dict[str, Any]:
    command = [ffprobe, "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames,sample_rate,channels,duration", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=30, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "ffprobe failed")
    streams = json.loads(result.stdout).get("streams")
    if not isinstance(streams, list) or len(streams) != 2:
        raise ValueError("H3 output must have exactly one video and one audio stream")
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise ValueError("H3 output must have exactly one video and one audio stream")
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise ValueError("H3 output must use H.264 video and AAC audio")
    if (video.get("width"), video.get("height")) != (settings.width, settings.height):
        raise ValueError("H3 output dimensions differ from requested dimensions")
    numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/", 1)
    if float(numerator) / float(denominator) != H3_FPS:
        raise ValueError("H3 output is not 24 fps")
    frames = int(video.get("nb_read_frames", 0))
    if frames != settings.effective_frames:
        raise ValueError("H3 output frame count differs from effective frame count")
    if str(audio.get("sample_rate")) != "32000" or audio.get("channels") != 2:
        raise ValueError("H3 output audio must be 32 kHz stereo")
    video_duration, audio_duration = float(video["duration"]), float(audio["duration"])
    if not math.isfinite(video_duration) or not math.isfinite(audio_duration) or abs(video_duration - audio_duration) > 1 / H3_FPS:
        raise ValueError("H3 audio and video durations are not synchronized")
    return {"width": settings.width, "height": settings.height, "frames": frames, "frame_rate": H3_FPS, "video_duration": video_duration, "audio_codec": "aac", "audio_sample_rate": 32000, "audio_channels": 2, "audio_duration": audio_duration}


def _extract_h3_frame(video: Path, target: Path, ffmpeg: str) -> None:
    if target.exists():
        raise ValueError(f"handoff already exists: {target}")
    result = subprocess.run([ffmpeg, "-v", "error", "-n", "-sseof", "-1", "-i", str(video), "-frames:v", "1", "-vf", "format=rgb24", str(target)], capture_output=True, text=True, errors="replace", timeout=30, check=False)
    if result.returncode or not target.is_file():
        raise ValueError(result.stderr.strip() or "could not extract H3 final frame")
