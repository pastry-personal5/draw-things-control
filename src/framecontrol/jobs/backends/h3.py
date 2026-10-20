"""H3 image-to-audio-video adapter.

All installation-specific paths live in :class:`H3Runtime`; the job file only
contains portable generation settings.  This deliberately does not reuse the
Draw Things colour/tagging or frame-handoff implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from framecontrol.core.errors import InputError
from framecontrol.core.numbers import is_int, is_number, setting_number
from framecontrol.core.paths import linked_component
from framecontrol.core.process.spawn import run as run_process
from framecontrol.jobs.backend import H3_BACKEND, OutputPaths, RunContext, RunPreparation
from framecontrol.jobs.backends.h3_finish import H3_COLOR_POLICY_VERSION, H3_FPS, H3FinishColorState, H3FinishRequest, H3OutputSpec, _partial_path, finish_h3
from framecontrol.jobs.definition import COLOR_ANCHORS, REANCHOR_RULES, ColorPolicy, GenerationMode, JobDefinition
from framecontrol.jobs.inputs.size import ResizePlan, decode_image, read_image_info
from framecontrol.jobs.media.checks import MediaCheck
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.overrides import MAX_SEED
from framecontrol.jobs.timing.environment import executable_facts
from framecontrol.jobs.timing.parsers import H3StageParser, StageParser
from framecontrol.jobs.timing.timeline import NO_TIMER, StageTimer

if TYPE_CHECKING:
    from framecontrol.jobs.events import RunStatus
    from framecontrol.jobs.parsing import JobParser
    from framecontrol.jobs.planning import PlannedRun
    from framecontrol.jobs.records import RunRecord

H3_INFO_TIMEOUT_SECONDS = 30
_COMPATIBILITY_FLAGS = {"bf16_mlp": "--use-slower-bf16-mlp", "bf16_qkv": "--use-slower-bf16-qkv", "bf16_attention_output": "--use-slower-bf16-attention-output", "row_major_attention_output": "--use-slower-row-major-attention-output", "unfused_int8_inputs": "--use-slower-unfused-int8-inputs", "unfused_qkv_rope": "--use-slower-unfused-qkv-rope", "scalar_qkv_rms": "--use-slower-scalar-qkv-rms", "uncached_int8_scales": "--use-slower-uncached-int8-scales", "dynamic_fc1_k": "--use-slower-dynamic-fc1-k", "grouped_quantizer": "--use-slower-grouped-quantizer"}


@dataclass(frozen=True)
class H3Runtime:
    executable: Path
    model_directory: Path


@dataclass(frozen=True)
class H3Run:
    run: int
    pipeline: str
    reference_pair: str | None = None


@dataclass(frozen=True)
class H3Reference:
    kind: str
    path: Path
    audio: Path | None = None
    media_facts: dict[str, Any] | None = None
    sha256: str | None = None
    audio_sha256: str | None = None
    basename: str = ""
    audio_basename: str | None = None


@dataclass(frozen=True)
class H3ReferencePair:
    name: str
    references: tuple[H3Reference, ...]


@dataclass(frozen=True)
class H3JobData:
    settings: H3Settings
    runs: tuple[H3Run, ...]
    reference_pairs: tuple[H3ReferencePair, ...]
    input_directory: Path
    max_reference_file_bytes: int = 4 * 1024 * 1024 * 1024
    max_reference_bytes: int = 32 * 1024 * 1024 * 1024


def h3_run(data: H3JobData, number: int) -> H3Run:
    """Return a legacy chain run or a version-2 one-run adapter view."""
    runs = data.runs
    if not runs:
        raise ValueError("H3 job has no run plan")
    return runs[number - 1 if len(runs) > 1 else 0]


@dataclass(frozen=True)
class H3ColorState:
    run_input: Path | None
    segment_anchor: Path | None
    reanchor_reason: str | None


@dataclass(frozen=True)
class H3Settings:
    width: int
    height: int
    frames: int
    seconds: float
    steps: int
    seed: int
    effective_frames: int
    render_width: int | None = None
    render_height: int | None = None
    denoise_reuse: int = 1
    dit_layers: int = 50
    core_reuse: int = 1
    token_reduction: bool = False
    ssd_streaming: bool = False
    int8_row_fc2: bool = False
    reference_rope: bool = False
    reference_image_size: str = "match"
    compatibility: tuple[tuple[str, bool], ...] = ()
    explicit: frozenset[str] = frozenset()

    @property
    def effective_seconds(self) -> float:
        return self.effective_frames / H3_FPS


# The flags ``H3Command.command`` writes with a value after them (its first word, the executable, is not a flag); the
# rest stand alone. A ``--ref-*`` flag may carry a second word, its audio file.
H3_VALUE_FLAGS = frozenset({"-d", "-p", "-o", "--first-frame", "--ref-image", "--ref-video", "--ref-silent-video", "--ref-audio", "--ref-video-audio", "--ref-image-size", "--width", "--height", "--frames", "--steps", "--seed", "--render-width", "--render-height", "--reuse", "--layers", "--core-reuse"})
# The flags a command leaves out while a setting is at its default, with that default, in the order the command writes them.
H3_DEFAULT_ARGUMENTS = (("--reuse", str(H3Settings.denoise_reuse)), ("--layers", str(H3Settings.dit_layers)), ("--core-reuse", str(H3Settings.core_reuse)))


def is_h3_command(command: Sequence[str]) -> bool:
    """Whether ``command`` is an H3 command (``H3Command.command`` starts ``executable -d``), not a draw-things-cli one."""
    return len(command) > 1 and command[1] == "-d"


@dataclass(frozen=True)
class H3Arguments:
    executable: Path
    model_directory: Path
    prompt: str
    pipeline: str
    references: tuple[tuple[str, Path, Path | None], ...]
    first_frame: Path | None
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
        args = [str(self.executable), "-d", str(self.model_directory), "-p", self.prompt]
        if self.pipeline == "i2av":
            if self.first_frame is None:
                raise ValueError("H3 i2av requires a first frame")
            args.extend(("--first-frame", str(self.first_frame)))
        else:
            for kind, path, audio in self.references:
                flag = {"image": "--ref-image", "video": "--ref-video", "silent_video": "--ref-silent-video", "audio": "--ref-audio", "video_audio": "--ref-video-audio"}[kind]
                args.extend((flag, str(path)))
                if audio is not None:
                    args.append(str(audio))
            args.extend(("--ref-image-size", self.settings.reference_image_size))
        args.extend(("--width", str(self.settings.width), "--height", str(self.settings.height), "--frames", str(self.settings.effective_frames), "--steps", str(self.settings.steps), "--seed", str(self.settings.seed), "-o", str(self.output)))
        if self.settings.render_width is not None:
            args.extend(("--render-width", str(self.settings.render_width), "--render-height", str(self.settings.render_height)))
        if "denoise_reuse" in self.settings.explicit or self.settings.denoise_reuse != 1:
            args.extend(("--reuse", str(self.settings.denoise_reuse)))
        if "dit_layers" in self.settings.explicit or self.settings.dit_layers != 50:
            args.extend(("--layers", str(self.settings.dit_layers)))
        if "core_reuse" in self.settings.explicit or self.settings.core_reuse != 1:
            args.extend(("--core-reuse", str(self.settings.core_reuse)))
        for key, flag in (("token_reduction", "--token-reduction"), ("ssd_streaming", "--ssd-streaming"), ("int8_row_fc2", "--use-int8-row-fc2"), ("reference_rope", "--use-reference-rope")):
            if getattr(self.settings, key):
                args.append(flag)
        args.extend(flag for key, flag in _COMPATIBILITY_FLAGS.items() if dict(self.settings.compatibility).get(key, False))
        return tuple(args)

    @property
    def unterminated_reading(self) -> Callable[[str], bool]:
        """H3 ends none of its redraws, so a reading that already reads whole is released once nothing follows it (see the runner)."""
        return H3StageParser().accepts

    @property
    def environment(self) -> dict[str, str]:
        # h3.c uses posix_spawnp for media utilities.  Put only the resolved
        # pair first and discard its own environment overrides.
        environment = dict(os.environ)
        environment.pop("H3_FFMPEG", None)
        environment.pop("H3_FFPROBE", None)
        environment["PATH"] = os.pathsep.join((str(Path(self.ffmpeg).parent), str(Path(self.ffprobe).parent), environment.get("PATH", "")))
        return environment


def effective_frames(frames: int) -> int:
    """Return the nearest H3-supported frame count on its ``17n + 5`` grid."""
    minimum, maximum = 5, 362
    grid_index = math.floor((frames - minimum) / 17 + 0.5)
    return min(maximum, max(minimum, minimum + 17 * grid_index))


def _round_half_away(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _integer_setting(parser: JobParser, value: dict[str, Any], key: str, minimum: int, maximum: int, default: int) -> int:
    result = value.get(key, default)
    if not is_int(result) or not minimum <= result <= maximum:
        parser.fail(f"backend_settings.{key}", f"must be an integer from {minimum} to {maximum}")
    return result


def _color_policy(parser: JobParser, value: Any, has_i2av: bool) -> ColorPolicy:
    if not isinstance(value, dict):
        parser.fail("color", "must be a mapping")
    keys = {"anchor", "strength", "first_weight", "reanchor", "regions"}
    extra = sorted(set(value) - keys)
    if extra:
        parser.fail(f"color.{extra[0]}", "is not a known H3 color key")
    anchor = value.get("anchor", "none")
    if anchor not in COLOR_ANCHORS:
        parser.fail("color.anchor", "must be none, previous, first, or blend")
    if anchor != "none" and not has_i2av:
        parser.fail("color.anchor", "requires at least one i2av run")
    allowed = {"none": {"anchor"}, "previous": {"anchor", "strength", "regions"}, "first": {"anchor", "strength", "reanchor", "regions"}, "blend": keys}[anchor]
    for key in value:
        if key not in allowed:
            parser.fail(f"color.{key}", f"is not applicable with anchor: {anchor}")
    strength = value.get("strength", 1)
    if not is_number(strength) or (finite_strength := setting_number(strength)) is None or not 0 <= finite_strength <= 1:
        parser.fail("color.strength", "must be a finite number from 0 to 1")
    first_weight = value.get("first_weight", 0.25)
    if not is_number(first_weight) or (finite_weight := setting_number(first_weight)) is None or not 0 <= finite_weight <= 1:
        parser.fail("color.first_weight", "must be a finite number from 0 to 1")
    reanchor = value.get("reanchor", "prompt_pair")
    if reanchor not in REANCHOR_RULES:
        parser.fail("color.reanchor", "must be prompt_pair or never")
    regions = value.get("regions", False)
    if not isinstance(regions, bool):
        parser.fail("color.regions", "must be true or false")
    return ColorPolicy(anchor=anchor, strength=finite_strength, first_weight=finite_weight, reanchor=reanchor, regions=regions)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reference_path_context(input_directory: Path, entry: dict[str, Any]) -> str:
    """Show the lookup root before the paths written in a reference entry."""
    root = input_directory.resolve()
    lines = [f"Looked in: {root}"]
    for key in ("path", "audio"):
        raw = entry.get(key)
        if isinstance(raw, str):
            value = raw.strip()
            candidate = Path(value)
            if candidate.is_absolute() and candidate.is_relative_to(root):
                value = str(candidate.relative_to(root))
            lines.append(f"  {key}: {value}")
    return "\n" + "\n".join(lines)


def _inspect_reference(kind: str, path: Path, audio_path: Path | None, *, decode: bool = True) -> dict[str, Any]:
    """Decode a declared reference and validate the streams H3 will consume. ``decode=False`` (a caller that only reads the
    job's fields, such as the Jobs list) keeps every header and stream check but skips decoding the pixels and samples."""
    if kind == "image":
        size, orientation = read_image_info(path)
        if decode:
            decode_image(path)
        return {"video": None, "audio": None, "image_size": list(size), "orientation": orientation}
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if ffprobe is None or ffmpeg is None:
        raise InputError("ffprobe and ffmpeg are required to validate H3 media references")

    def streams(source: Path) -> tuple[list[dict[str, Any]], float]:
        result = run_process((ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)), capture_output=True, text=True, errors="replace", timeout=20, check=False)
        if result.returncode != 0:
            raise ValueError(f"cannot inspect media: {result.stderr.strip() or 'ffprobe failed'}")
        payload = json.loads(result.stdout)
        items = payload.get("streams")
        if not isinstance(items, list) or not items:
            raise ValueError("contains no decodable media streams")
        duration = float(payload.get("format", {}).get("duration", 0.0) or 0.0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("has an invalid duration")
        return items, duration

    def decode_stream(source: Path, selector: str) -> None:
        if not decode:
            return
        result = run_process((ffmpeg, "-v", "error", "-i", str(source), "-map", selector, "-f", "null", "-"), capture_output=True, text=True, errors="replace", timeout=40, check=False)
        if result.returncode != 0:
            raise ValueError(f"cannot decode media: {result.stderr.strip() or 'ffmpeg failed'}")

    if kind in ("video", "video_audio", "silent_video"):
        stream_list, format_duration = streams(path)
        video_streams = [item for item in stream_list if item.get("codec_type") == "video"]
        audio_streams = [item for item in stream_list if item.get("codec_type") == "audio"]
        video_duration = float(video_streams[0].get("duration") or format_duration) if video_streams else 0.0
        if len(video_streams) != 1 or not math.isfinite(video_duration) or video_duration < 2 or video_duration > 15:
            raise ValueError("must contain one video stream with a duration from 2 to 15 seconds")
        if kind == "video" and not audio_streams:
            raise ValueError("video references require an embedded audio stream")
        if kind in ("video_audio", "silent_video") and not video_streams:
            raise ValueError("must contain a video stream")
        decode_stream(path, "0:v:0")
        embedded_audio = None
        if kind == "video" and audio_streams:
            decode_stream(path, "0:a:0")
            embedded_audio = float(audio_streams[0].get("duration") or video_duration)
            if not 2 <= embedded_audio <= 15:
                raise ValueError("embedded audio duration must be from 2 to 15 seconds")
            embedded_audio = min(embedded_audio, video_duration, 362 / H3_FPS)
        separate_audio = None
        if kind == "video_audio":
            assert audio_path is not None
            separate_audio = _inspect_audio(audio_path, ffprobe, ffmpeg, decode=decode)
            separate_audio = min(separate_audio, video_duration, 362 / H3_FPS)
        return {"video": video_duration, "audio": embedded_audio if kind == "video" else separate_audio}
    audio_duration = _inspect_audio(path, ffprobe, ffmpeg, decode=decode)
    return {"video": None, "audio": audio_duration}


def _inspect_audio(path: Path, ffprobe: str, ffmpeg: str, *, decode: bool = True) -> float:
    result = run_process((ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)), capture_output=True, text=True, errors="replace", timeout=20, check=False)
    if result.returncode != 0:
        raise ValueError(f"cannot inspect audio: {result.stderr.strip() or 'ffprobe failed'}")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    duration = float(audio[0].get("duration") or payload.get("format", {}).get("duration") or 0.0) if len(audio) == 1 else 0.0
    if len(audio) != 1 or not math.isfinite(duration) or not 2 <= duration <= 15:
        raise ValueError("must contain exactly one audio stream with a duration from 2 to 15 seconds")
    if not decode:
        return duration
    decoded = run_process((ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"), capture_output=True, text=True, errors="replace", timeout=40, check=False)
    if decoded.returncode != 0:
        raise ValueError(f"cannot decode audio: {decoded.stderr.strip() or 'ffmpeg failed'}")
    return duration


class H3Backend:
    id = H3_BACKEND
    supported_modes = frozenset({"i2av", "h3av"})

    def __init__(self, runtime: H3Runtime) -> None:
        self.runtime = runtime
        self._ffmpeg: str | None = None
        self._ffprobe: str | None = None
        self._installation: dict[str, Any] = {}
        self._reference_labels: dict[str, str] = {}

    def parse_job(self, parser: JobParser, data: dict[str, Any], source_text: str) -> JobDefinition:
        common = parser.parse_common(data)
        if common.mode not in (GenerationMode.I2AV, GenerationMode.H3AV):
            parser.fail("mode", "is not supported by h3-metal-cli")
        for key in ("config_file", "config_override", "desired_input_width", "desired_input_height", "max_input_crop_percent"):
            if key in data:
                parser.fail(key, "is not supported by h3-metal-cli")
        for index, pair in enumerate(data["prompt_pairs"]):
            if isinstance(pair, dict) and "negative" in pair:
                parser.fail(f"prompt_pairs[{index}].negative", "is not supported by h3-metal-cli")
        settings = self._settings(parser, data.get("backend_settings"))
        if common.mode is GenerationMode.H3AV:
            runs = self._runs(parser, data.get("h3_runs"), common.run_count)
            reference_pairs = self._reference_pairs(parser, data.get("reference_pairs"), runs, settings.effective_frames)
        else:
            if "h3_runs" in data or "reference_pairs" in data:
                parser.fail("h3_runs", "and reference_pairs are only supported in h3av jobs")
            runs, reference_pairs = tuple(H3Run(run, "i2av") for run in range(1, common.run_count + 1)), ()
        # A version-2 profile's color policy may serve an r2av run (measured, never corrected) beside i2av ones; the
        # compiler checks that some i2av run selects the profile.
        color = _color_policy(parser, data["color"], parser.run_view or any(run.pipeline == "i2av" for run in runs)) if "color" in data else ColorPolicy(regions=False)
        if settings.reference_image_size == "max" and not any(run.pipeline == "r2av" for run in runs):
            parser.fail("backend_settings.reference_image_size", "max requires at least one r2av run")
        output = data.get("output", {})
        directory = parser.output_directory(output, common.name, {"directory", "extension"})
        extension = output.get("extension", "mp4")
        if extension != "mp4":
            parser.fail("output.extension", "must be mp4 for h3-metal-cli")
        first_is_i2av = runs[0].pipeline == "i2av"
        if first_is_i2av and common.input is None:
            parser.fail("input", "is required when the first H3 run uses i2av")
        if not first_is_i2av and common.input is not None:
            parser.fail("input", "is forbidden when the first H3 run uses r2av")
        plan = None
        if first_is_i2av and common.input is not None and common.input.is_file():
            image_size, orientation = read_image_info(common.input)
            if parser.decode_input:
                decode_image(common.input)
            plan = _centre_crop_plan(image_size, orientation, settings.width, settings.height)
        elif first_is_i2av and parser.allow_missing_input:
            # A resume starts from its saved verified handoff, never this path.
            plan = ResizePlan(settings.width, settings.height, None, (settings.width, settings.height), None, (settings.width, settings.height), "none", (settings.width, settings.height), None)
        elif first_is_i2av:
            parser.fail("input", f"file does not exist: {common.input}")
        return JobDefinition(path=parser.path, name=common.name, mode=common.mode, input=common.input, run_count=common.run_count, prompt_pairs=common.prompt_pairs, output_directory=directory.resolve(), extension="mp4", run_timeout_seconds=common.run_timeout_seconds, requested_seed=settings.seed, requested_seed_source="backend_settings", backend=self.id, backend_data=H3JobData(settings, runs, reference_pairs, parser.global_config.input_directory.resolve(), parser.global_config.api_limits.max_reference_file_bytes, parser.global_config.api_limits.max_reference_bytes), input_resize=plan, color=color, cooldown=common.cooldown, cooldown_source=common.cooldown_source, source_text=source_text)

    @staticmethod
    def _settings(parser: JobParser, value: Any) -> H3Settings:
        required = {"width", "height", "frames", "seconds", "steps", "seed"}
        optional = {"render_width", "render_height", "denoise_reuse", "dit_layers", "core_reuse", "token_reduction", "ssd_streaming", "int8_row_fc2", "reference_rope", "reference_image_size", "compatibility"}
        if not isinstance(value, dict):
            parser.fail("backend_settings", "must be a mapping")
        if required - set(value) or set(value) - required - optional:
            missing, extra = required - set(value), set(value) - required - optional
            parser.fail("backend_settings", f"must contain {', '.join(sorted(required))}{'; missing ' + ', '.join(sorted(missing)) if missing else ''}{'; unknown ' + ', '.join(sorted(extra)) if extra else ''}")
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
        render_width, render_height = value.get("render_width"), value.get("render_height")
        if ("render_width" in value) != ("render_height" in value):
            parser.fail("backend_settings.render_width", "and render_height must be specified together")
        if "render_width" in value:
            if not is_int(render_width) or not is_int(render_height) or render_width < 32 or render_height < 32 or render_width % 32 or render_height % 32 or render_width > width or render_height > height or render_width * height != render_height * width:
                parser.fail("backend_settings.render_width", "and render_height must be divisible by 32, no larger than output, and have the same aspect ratio")
        denoise_reuse = _integer_setting(parser, value, "denoise_reuse", 1, 3, 1)
        dit_layers = _integer_setting(parser, value, "dit_layers", 35, 50, 50)
        core_reuse = _integer_setting(parser, value, "core_reuse", 1, 6, 1)
        if denoise_reuse > 1 and core_reuse > 1:
            parser.fail("backend_settings", "denoise_reuse and core_reuse cannot both be greater than 1")
        booleans = ("token_reduction", "ssd_streaming", "int8_row_fc2", "reference_rope")
        for key in booleans:
            if key in value and not isinstance(value[key], bool):
                parser.fail(f"backend_settings.{key}", "must be true or false")
        compatibility_keys = set(_COMPATIBILITY_FLAGS)
        compatibility = value.get("compatibility", {})
        if not isinstance(compatibility, dict):
            parser.fail("backend_settings.compatibility", "must be a mapping")
        extra = set(compatibility) - compatibility_keys
        if extra:
            parser.fail(f"backend_settings.compatibility.{sorted(extra)[0]}", "is not a known key")
        if any(not isinstance(item, bool) for item in compatibility.values()):
            parser.fail("backend_settings.compatibility", "values must be true or false")
        if value.get("int8_row_fc2", False) and (value.get("ssd_streaming", False) or compatibility.get("bf16_mlp", False)):
            parser.fail("backend_settings.int8_row_fc2", "conflicts with ssd_streaming and compatibility.bf16_mlp")
        reference_image_size = value.get("reference_image_size", "match")
        if reference_image_size not in ("match", "max"):
            parser.fail("backend_settings.reference_image_size", "must be match or max")
        return H3Settings(width, height, frames, seconds, steps, seed, parser.legacy_h3_frames if parser.legacy_h3_frames is not None else effective_frames(frames), render_width, render_height, denoise_reuse, dit_layers, core_reuse, value.get("token_reduction", False), value.get("ssd_streaming", False), value.get("int8_row_fc2", False), value.get("reference_rope", False), reference_image_size, tuple(sorted((dict.fromkeys(compatibility_keys, False) | compatibility).items())), frozenset(value))

    @staticmethod
    def _runs(parser: JobParser, value: Any, run_count: int) -> tuple[H3Run, ...]:
        if not isinstance(value, list) or len(value) != run_count:
            parser.fail("h3_runs", f"must contain exactly {run_count} run entries")
        result: list[H3Run] = []
        for index, item in enumerate(value):
            field = f"h3_runs[{index}]"
            if not isinstance(item, dict):
                parser.fail(field, "must be a mapping")
            if set(item) - {"run", "pipeline", "reference_pair"} or set(item) < {"run", "pipeline"}:
                parser.fail(field, "must contain run and pipeline, with reference_pair only for r2av")
            number, pipeline = item["run"], item["pipeline"]
            if not is_int(number) or number != index + 1:
                parser.fail(f"{field}.run", "must list each run number from 1 through run_count in order")
            if pipeline not in ("i2av", "r2av"):
                parser.fail(f"{field}.pipeline", "must be i2av or r2av")
            pair = item.get("reference_pair")
            if pipeline == "r2av" and (not isinstance(pair, str) or not pair):
                parser.fail(f"{field}.reference_pair", "is required for r2av")
            if pipeline == "i2av" and "reference_pair" in item:
                parser.fail(f"{field}.reference_pair", "is forbidden for i2av")
            result.append(H3Run(number, pipeline, pair))
        return tuple(result)

    @staticmethod
    def _reference_pairs(parser: JobParser, value: Any, runs: tuple[H3Run, ...], effective_frames_count: int) -> tuple[H3ReferencePair, ...]:
        selected = {run.reference_pair for run in runs if run.pipeline == "r2av" and run.reference_pair is not None}
        if not selected:
            if value is not None:
                parser.fail("reference_pairs", "is forbidden when no h3_runs entry uses r2av")
            return ()
        if not isinstance(value, list) or not value:
            parser.fail("reference_pairs", "must be a nonempty list when any h3 run uses r2av")
        pairs: list[H3ReferencePair] = []
        names: set[str] = set()
        aggregate_bytes = 0
        for pair_index, pair in enumerate(value):
            field = f"reference_pairs[{pair_index}]"
            if not isinstance(pair, dict) or set(pair) != {"name", "references"}:
                parser.fail(field, "must contain exactly name and references")
            name, references = pair["name"], pair["references"]
            if not isinstance(name, str) or not name or name in names:
                parser.fail(f"{field}.name", "must be a unique nonempty string")
            names.add(name)
            inactive = name in parser.inactive_reference_pairs
            if not isinstance(references, list) or not references or len(references) > 12:
                parser.fail(f"{field}.references", "must be a nonempty list of at most 12 references")
            parsed: list[H3Reference] = []
            images = videos = audios = 0
            audio_seconds = 0.0
            for entry_index, entry in enumerate(references):
                entry_field = f"{field}.references[{entry_index}]"
                if not isinstance(entry, dict) or entry.get("kind") not in ("image", "video", "video_audio", "silent_video", "audio"):
                    parser.fail(f"{entry_field}.kind", "must be image, video, video_audio, silent_video, or audio")
                kind = entry["kind"]
                expected = {"kind", "path", "audio"} if kind == "video_audio" else {"kind", "path"}
                if set(entry) != expected:
                    parser.fail(entry_field, "has unknown or missing fields")
                path_context = _reference_path_context(parser.global_config.input_directory, entry)
                paths: dict[str, Path] = {}
                snapshotted_any = False
                for key in ("path", "audio") if kind == "video_audio" else ("path",):
                    raw = entry[key]
                    key_field = f"{entry_field}.{key}"
                    if not isinstance(raw, str) or not raw.strip():
                        parser.fail(key_field, "must be a nonempty file path")
                    path = parser.reference_override(name, entry_index, key)
                    snapshotted = path is not None
                    snapshotted_any |= snapshotted
                    if parser.require_reference_snapshots and not snapshotted:
                        parser.fail(key_field, "has no saved reference snapshot")
                    if path is None:
                        input_directory = parser.global_config.input_directory
                        lexical = Path(raw.strip()).expanduser()
                        lexical = lexical if lexical.is_absolute() else input_directory / lexical
                        link = linked_component(lexical, input_directory)
                        if link is not None:
                            parser.fail(key_field, f"must not use symbolic links: {link}{path_context}")
                        try:
                            path = parser._confined_path(raw.strip(), input_directory, key_field)
                        except InputError as error:
                            raise type(error)(f"{error}{path_context}", field=error.field, path=error.path) from error
                    if not inactive and (not path.is_file() or path.is_symlink()):
                        parser.fail(key_field, f"must name an existing regular file and must not be a symbolic link{'' if snapshotted else path_context}")
                    if not snapshotted:
                        size = path.stat().st_size
                        file_limit = parser.global_config.api_limits.max_reference_file_bytes
                        if size > file_limit:
                            parser.fail(key_field, f"is {size} bytes, over the max_reference_file_bytes limit of {file_limit}{path_context}")
                        aggregate_bytes += size
                        total_limit = parser.global_config.api_limits.max_reference_bytes
                        if aggregate_bytes > total_limit:
                            parser.fail(key_field, f"makes reference assets exceed the max_reference_bytes limit of {total_limit}{path_context}")
                    paths[key] = path
                if kind == "image":
                    images += 1
                elif kind in ("video", "video_audio", "silent_video"):
                    videos += 1
                if kind in ("audio", "video", "video_audio"):
                    audios += 1
                audio_path = paths.get("audio") if kind == "video_audio" else paths["path"] if kind in ("audio", "video") else None
                saved = parser.reference_fact(name, entry_index, "path") if inactive else None
                saved_audio = parser.reference_fact(name, entry_index, "audio") if inactive and audio_path is not None else None
                if inactive:
                    if saved is None or not isinstance(saved.get("media_facts"), dict):
                        parser.fail(f"{entry_field}.path", "has no saved media facts")
                    duration = saved["media_facts"]
                    if kind == "video_audio" and saved_audio is None:
                        parser.fail(f"{entry_field}.audio", "has no saved reference snapshot")
                else:
                    try:
                        duration = _inspect_reference(kind, paths["path"], audio_path, decode=parser.decode_input)
                    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                        media_field = "audio" if kind == "video_audio" and audio_path is not None and "audio" in str(error).lower() else "path"
                        parser.fail(f"{entry_field}.{media_field}", f"{error}{'' if snapshotted_any else path_context}")
                if kind in ("video", "video_audio") and effective_frames_count < 56:
                    parser.fail(f"{entry_field}.path", "video audio references require at least 56 effective output frames")
                if kind in ("audio", "video", "video_audio") and not inactive:
                    consumed = duration["audio"] if kind == "video" else duration["audio"]
                    if kind in ("video", "video_audio") and duration["audio"] is not None:
                        consumed = min(consumed or 0.0, effective_frames_count / H3_FPS)
                    audio_seconds += consumed or 0.0
                path_digest = str(saved["sha256"]) if saved is not None else _file_sha256(paths["path"])
                audio_digest = str(saved_audio["sha256"]) if saved_audio is not None else _file_sha256(paths["audio"]) if paths.get("audio") is not None else None
                parsed.append(H3Reference(kind, paths["path"], paths.get("audio"), duration, path_digest, audio_digest, Path(entry["path"]).name, Path(entry["audio"]).name if "audio" in entry else None))
            if images > 9 or videos > 3 or audios > 3:
                parser.fail(f"{field}.references", "exceeds the Ref2VA image, video, or audio count limit")
            if images == 0 and videos == 0:
                parser.fail(f"{field}.references", "needs at least one image or video")
            if audio_seconds > 15:
                parser.fail(f"{field}.references", "consumed audio duration must not exceed 15 seconds")
            pairs.append(H3ReferencePair(name, tuple(parsed)))
        missing = selected - names
        unused = names - selected
        if missing:
            parser.fail("h3_runs", f"selects undeclared reference pair '{sorted(missing)[0]}'")
        if unused:
            parser.fail("reference_pairs", f"declares unselected pair '{sorted(unused)[0]}'")
        return tuple(pairs)

    def safe_settings(self, job: JobDefinition) -> dict[str, Any]:
        value = cast(H3JobData, job.backend_data)
        settings = value.settings
        switches = []
        if settings.render_width is not None:
            switches.extend(("--render-width", "--render-height"))
        if "denoise_reuse" in settings.explicit or settings.denoise_reuse != 1:
            switches.append("--reuse")
        if "dit_layers" in settings.explicit or settings.dit_layers != 50:
            switches.append("--layers")
        if "core_reuse" in settings.explicit or settings.core_reuse != 1:
            switches.append("--core-reuse")
        switches.extend(flag for key, flag in (("token_reduction", "--token-reduction"), ("ssd_streaming", "--ssd-streaming"), ("int8_row_fc2", "--use-int8-row-fc2"), ("reference_rope", "--use-reference-rope")) if getattr(settings, key))
        switches.extend(flag for key, flag in _COMPATIBILITY_FLAGS.items() if dict(settings.compatibility).get(key, False))
        if any(run.pipeline == "r2av" for run in value.runs):
            switches.append("--ref-image-size")
        return {"backend": self.id, "width": settings.width, "height": settings.height, "requested_frames": settings.frames, "requested_seconds": settings.seconds, "effective_frames": settings.effective_frames, "effective_seconds": settings.effective_seconds, "steps": settings.steps, "seed": settings.seed, "seed_source": "backend_settings", "output_extension": "mp4", "render_width": settings.render_width, "render_height": settings.render_height, "denoise_reuse": settings.denoise_reuse, "dit_layers": settings.dit_layers, "core_reuse": settings.core_reuse, "token_reduction": settings.token_reduction, "ssd_streaming": settings.ssd_streaming, "int8_row_fc2": settings.int8_row_fc2, "reference_rope": settings.reference_rope, "reference_image_size": settings.reference_image_size, "compatibility": dict(settings.compatibility), "explicit_controls": sorted(settings.explicit), "cli_switches": switches, "h3_runs": [{"run": run.run, "pipeline": run.pipeline, **({"reference_pair": run.reference_pair} if run.reference_pair is not None else {})} for run in value.runs], "reference_pairs": [{"name": pair.name, "references": [{"kind": ref.kind, "path": ref.basename or ref.path.name, "sha256": ref.sha256, **({"audio": ref.audio_basename or ref.audio.name, "audio_sha256": ref.audio_sha256} if ref.audio else {})} for ref in pair.references]} for pair in value.reference_pairs], "color_policy": {"version": H3_COLOR_POLICY_VERSION, **job.color.as_dict()}}

    def snapshot_config(self, job: JobDefinition, params_directory: Path, max_bytes: int) -> str:
        return ""

    def finishing_seconds(self, job: JobDefinition) -> float:
        data = cast(H3JobData, job.backend_data)
        asset_count = sum(1 + (reference.audio is not None) for pair in data.reference_pairs for reference in pair.references)
        # Structural checks, separate color probes, preferred conversion and its
        # fallback, two drift decodes, and handoff all have bounded deadlines.
        correction = 600.0 if job.color.corrects and job.color.strength > 0 else 0.0
        return 900.0 + correction + asset_count * 40.0

    def estimated_output_bytes(self, job: JobDefinition, runs: int) -> int:
        """Conservative planning bound for retained and in-flight H3 MP4 copies and a PNG handoff."""
        if runs <= 0:
            return 0
        data = cast(H3JobData, job.backend_data)
        settings = data.settings
        pixels = settings.width * settings.height
        # RGB24 per video frame exceeds the usual H.264 payload. Add a MiB
        # per MP4 for AAC and container overhead, then count staged copies.
        mp4 = pixels * 3 * settings.effective_frames + 1024 * 1024 + int(settings.effective_seconds * 24_000)
        correcting = job.color.corrects and job.color.strength > 0
        return sum(((4 if correcting and run.pipeline == "i2av" else 2) * mp4 + pixels * 4 + 1024 * 1024) for run in data.runs[-runs:])

    def check_tools(self, job: JobDefinition, executable: str, media: MediaTools, find_executable: Callable[[str], str | None]) -> None:
        if os.environ.get("H3_FFMPEG") or os.environ.get("H3_FFPROBE"):
            raise InputError("H3_FFMPEG and H3_FFPROBE must not be set for h3-metal-cli jobs")
        if not self.runtime.executable.is_file() or not os.access(self.runtime.executable, os.X_OK):
            raise InputError(f"H3 executable is missing or not executable: {self.runtime.executable}")
        if not self.runtime.model_directory.is_dir():
            raise InputError(f"H3 model directory is missing: {self.runtime.model_directory}")
        ffmpeg = find_executable("ffmpeg")
        ffprobe = find_executable("ffprobe")
        if ffmpeg is None:
            raise InputError("ffmpeg is missing for h3-metal-cli")
        if ffprobe is None:
            raise InputError("ffprobe is missing for h3-metal-cli")
        self._ffmpeg, self._ffprobe = ffmpeg, ffprobe
        try:
            result = run_process((str(self.runtime.executable), "-d", str(self.runtime.model_directory), "--info"), capture_output=True, text=True, errors="replace", timeout=H3_INFO_TIMEOUT_SECONDS, check=False, cwd=self.runtime.executable.parent)
        except (OSError, subprocess.SubprocessError) as error:
            raise InputError(f"H3 installation probe failed: {error}") from error
        if result.returncode != 0:
            raise InputError(f"H3 installation probe failed for model directory {self.runtime.model_directory}: {result.stderr.strip() or 'exit code ' + str(result.returncode)}")
        info = result.stdout + "\n" + result.stderr
        data = cast(H3JobData, job.backend_data)
        inventory: dict[str, dict[str, int]] = {}
        for label in ("Qwen3-VL encoder", "FL2VA DiT", "Ref2VA DiT", "video VAE", "audio VAE"):
            match = re.search(rf"^\s*{re.escape(label)}\s+(\d+) files\s+(\d+) tensors\b", info, re.MULTILINE)
            inventory[label] = {"files": int(match.group(1)), "tensors": int(match.group(2))} if match else {"files": 0, "tensors": 0}
        fl2va_available = all(inventory["FL2VA DiT"].values())
        ref2va_available = all(inventory["Ref2VA DiT"].values())
        metal_4_capable = re.search(r"^\s*Metal 4\s+yes\s*$", info, re.MULTILINE | re.IGNORECASE) is not None
        has_r2av = any(run.pipeline == "r2av" for run in data.runs)
        if has_r2av and not ref2va_available:
            raise InputError("H3 Ref2VA checkpoint is not available in the model directory")
        if any(run.pipeline == "i2av" for run in data.runs) and not fl2va_available:
            raise InputError("H3 FL2VA checkpoint is not available in the model directory")
        if data.settings.int8_row_fc2 and not metal_4_capable:
            raise InputError("backend_settings.int8_row_fc2 requires an M5-class Metal 4 GPU")
        digest = hashlib.sha256()
        with self.runtime.executable.open("rb") as executable_file:
            for block in iter(lambda: executable_file.read(1024 * 1024), b""):
                digest.update(block)
        version_match = re.search(r"^h3-metal\s+(\S+)", info, re.MULTILINE)
        self._installation = {"version": version_match.group(1) if version_match else None, "executable_sha256": digest.hexdigest(), "checkpoint_inventory": inventory, "ref2va_available": ref2va_available, "fl2va_available": fl2va_available, "metal_4_capable": metal_4_capable}
        job.backend_settings["h3_installation"] = dict(self._installation)

    def output_size(self, job: JobDefinition) -> tuple[int, int] | None:
        """The size this run's video and handoff come out at."""
        settings = cast(H3JobData, job.backend_data).settings
        return settings.width, settings.height

    def plan_input(self, job: JobDefinition, artifact: Path) -> ResizePlan:
        """H3 centre-crops any image it is given to its profile's width and height, as it does the job's first image."""
        settings = cast(H3JobData, job.backend_data).settings
        size, orientation = read_image_info(artifact)
        return _centre_crop_plan(size, orientation, settings.width, settings.height)

    def prepare_job(self, job: JobDefinition) -> tuple[JobDefinition, Callable[[], None]]:
        """Freeze direct and queue-owned references once more for this execution, then clean up on every exit."""
        data = cast(H3JobData, job.backend_data)
        if not data.reference_pairs:
            return job, lambda: None
        if all(path.parent.name.startswith("h3-references-") for pair in data.reference_pairs for reference in pair.references for path in (reference.path, reference.audio) if path is not None):
            return job, lambda: None
        from framecontrol.jobs.backends.h3_references import cleanup_snapshot, snapshot_references

        temporary_root = Path(tempfile.mkdtemp(prefix="framecontrol-h3-references-"))
        try:
            _root, descriptors = snapshot_references(job, temporary_root, data.max_reference_file_bytes, data.max_reference_bytes)
            lookup = {(str(item["pair"]), int(item["ordinal"]), str(item["field"])): Path(str(item["snapshot_path"])) for item in descriptors}
            pairs: list[H3ReferencePair] = []
            for pair in data.reference_pairs:
                refs = tuple(H3Reference(reference.kind, lookup[(pair.name, ordinal, "path")], lookup.get((pair.name, ordinal, "audio")), reference.media_facts, reference.sha256, reference.audio_sha256, reference.basename, reference.audio_basename) for ordinal, reference in enumerate(pair.references))
                pairs.append(H3ReferencePair(pair.name, refs))
            prepared = replace(job, backend_data=replace(data, reference_pairs=tuple(pairs)))
        except BaseException:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise

        def cleanup() -> None:
            cleanup_snapshot(descriptors)
            shutil.rmtree(temporary_root, ignore_errors=True)

        return prepared, cleanup

    def output_paths(self, job: JobDefinition, output: Path) -> OutputPaths:
        return OutputPaths(output.with_name(f"{output.stem}-last-frame.png"))

    def stage_parser(self) -> StageParser:
        """Reads H3's `<phase> n/N` lines as stage readings."""
        return H3StageParser()

    def timing_workload(self, job: JobDefinition, run_number: int) -> dict[str, Any]:
        """The features a run's time is fitted on: its pixels, frames, and steps, its pipeline and references, and the rendering knobs."""
        data = cast(H3JobData, job.backend_data)
        settings = data.settings
        run = h3_run(data, run_number)
        selected = next((pair for pair in data.reference_pairs if pair.name == run.reference_pair), None)
        kinds = Counter(reference.kind for reference in selected.references) if selected is not None else Counter()
        workload: dict[str, Any] = {"pipeline": run.pipeline, "width": settings.width, "height": settings.height, "pixels": settings.width * settings.height, "frames": settings.effective_frames, "steps": settings.steps, "seconds": settings.effective_seconds, "references": dict(kinds), "reference_count": sum(kinds.values())}
        if run.pipeline == "i2av" and job.color.corrects and job.color.strength > 0:
            workload["color_corrects"] = True
        if any(value for _, value in settings.compatibility):
            workload["compatibility"] = dict(settings.compatibility)
        for name in ("render_width", "render_height", "denoise_reuse", "dit_layers", "core_reuse", "token_reduction", "ssd_streaming", "int8_row_fc2", "reference_rope", "reference_image_size"):
            workload[name] = getattr(settings, name)
        return workload

    def timing_environment(self, job: JobDefinition, executable: str | None) -> dict[str, Any]:
        """The build a run was timed on: H3's size and modification time, and the model directory's name."""
        return {**executable_facts(self.runtime.executable), "model": self.runtime.model_directory.name}

    def child_output(self, run: PlannedRun) -> Path:
        return _partial_path(run.output)

    def arguments(self, job: JobDefinition, pair: Any, run_input: Path | None, output: Path, seed: int, executable: str, run_number: int = 1) -> H3Arguments:
        data = cast(H3JobData, job.backend_data)
        settings = data.settings
        if self._ffmpeg is None or self._ffprobe is None:
            raise ValueError("H3 media tools were not resolved")
        run = h3_run(data, run_number)
        selected = next((item for item in data.reference_pairs if item.name == run.reference_pair), None)
        if run.pipeline == "r2av" and selected is None:
            raise ValueError(f"H3 run {run_number} has no selected reference pair")
        refs = tuple((item.kind, item.path, item.audio) for item in selected.references) if selected is not None else ()
        if selected is not None:
            for item in selected.references:
                self._reference_labels[str(item.path)] = item.basename or item.path.name
                if item.audio is not None:
                    self._reference_labels[str(item.audio)] = item.audio_basename or item.audio.name
        return H3Arguments(self.runtime.executable, self.runtime.model_directory, pair.positive, run.pipeline, refs, run_input if run.pipeline == "i2av" else None, _partial_path(output), replace(settings, seed=seed), self._ffmpeg, self._ffprobe)

    def prepare_run(self, job: JobDefinition, context: RunContext) -> RunPreparation:
        data = cast(H3JobData, job.backend_data)
        if h3_run(data, context.number).pipeline == "r2av":
            return RunPreparation(finish_state=H3ColorState(None, None, "r2av boundary"), chain_anchor=None)
        previous_r2av = len(data.runs) > 1 and context.number > 1 and data.runs[context.number - 2].pipeline == "r2av"
        changed_pair = context.previous_pair is not None and context.previous_pair.name != context.pair.name
        if context.chain_anchor is None or previous_r2av:
            anchor = context.first_image if context.number == 1 and context.first_image is not None else context.input
            reason = "initial i2av input" if context.number == 1 else "after r2av boundary"
        elif changed_pair and job.color.reanchor == "prompt_pair":
            anchor, reason = context.input, "prompt pair changed"
        else:
            anchor, reason = context.chain_anchor, None
        return RunPreparation(finish_state=H3ColorState(context.input, anchor, reason), record_anchor=anchor, chain_anchor=anchor)

    def redact(self, command: Sequence[str]) -> tuple[str, ...]:
        reference_flags = {"--ref-image", "--ref-video", "--ref-silent-video", "--ref-audio", "--ref-video-audio"}
        redacted: list[str] = []
        redact_next = 0
        for token in command:
            if token in reference_flags:
                redact_next = 2 if token == "--ref-video-audio" else 1
                redacted.append(token)
                continue
            if redact_next:
                redacted.append(self._reference_labels.get(token, Path(token).name))
                redact_next -= 1
            elif token == str(self.runtime.executable):
                redacted.append("<h3>")
            elif token == str(self.runtime.model_directory):
                redacted.append("<h3-model>")
            elif Path(token).name in {"ffmpeg", "ffprobe"}:
                redacted.append("<media-tool>")
            else:
                redacted.append(token)
        return tuple(redacted)

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None], finish_state: object | None, media: MediaTools, report: Callable[[int, MediaCheck], None] | None, timer: StageTimer = NO_TIMER) -> tuple[RunStatus, int] | None:
        data = cast(H3JobData, job.backend_data)
        settings = data.settings
        selected_run = h3_run(data, run.number)
        selected = next((item for item in data.reference_pairs if item.name == selected_run.reference_pair), None)
        references = tuple({"ordinal": ordinal, "kind": item.kind, "basename": item.basename or item.path.name, "sha256": item.sha256, **({"audio_basename": item.audio_basename or item.audio.name, "audio_sha256": item.audio_sha256} if item.audio else {})} for ordinal, item in enumerate(selected.references, 1)) if selected is not None else ()
        color = finish_state if isinstance(finish_state, H3ColorState) else H3ColorState(run.input, None, None)
        request = H3FinishRequest(
            executable=self.runtime.executable,
            ffmpeg=self._ffmpeg,
            ffprobe=self._ffprobe,
            output_spec=H3OutputSpec(settings.width, settings.height, settings.effective_frames),
            pipeline=selected_run.pipeline,
            reference_pair=selected_run.reference_pair,
            references=references,
            job=job,
            run=run,
            record=record,
            stop_requested=stop_requested,
            color_state=H3FinishColorState(color.run_input, color.segment_anchor, color.reanchor_reason),
            media=media,
            report=report,
            timer=timer,
        )
        return finish_h3(request)


def _centre_crop_plan(size: tuple[int, int], orientation: int | None, width: int, height: int) -> ResizePlan:
    source_width, source_height = size
    scale = max(width / source_width, height / source_height)
    crop_width, crop_height = width / scale, height / scale
    left, top = (source_width - crop_width) / 2, (source_height - crop_height) / 2
    scaled = (round(source_width * scale), round(source_height * scale))
    crop = 100 * (1 - crop_width * crop_height / (source_width * source_height))
    return ResizePlan(width, height, None, size, orientation, (width, height), "crop", scaled, crop, (left, top, left + crop_width, top + crop_height))
