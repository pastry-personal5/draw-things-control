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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from framecontrol.core.errors import InputError
from framecontrol.core.exit_codes import exit_code_for_signal
from framecontrol.core.numbers import is_int, is_number, setting_number
from framecontrol.core.paths import linked_component
from framecontrol.core.process.spawn import run as run_process
from framecontrol.jobs.backend import H3_BACKEND, OutputPaths, RunContext, RunPreparation
from framecontrol.jobs.definition import COLOR_ANCHORS, REANCHOR_RULES, ColorPolicy, GenerationMode, JobDefinition
from framecontrol.jobs.inputs.size import ResizePlan, decode_image, read_image_info
from framecontrol.jobs.media.checks import MediaCheck
from framecontrol.jobs.media.h3_color import DELIVERY, DELIVERY_TO_SRGB_FILTER, H3_RGB_CONVENTION_SHA256, NATIVE, NATIVE_TO_SRGB_FILTER, H3ColorCancelled, convert_h3_delivery, declarations_match, inspect_h3_declarations, measure_h3_drift, packet_facts
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.media.video_color import read_colr, tag_video_color_codes
from framecontrol.jobs.output_naming import raw_output_path, uncorrected_output_path
from framecontrol.jobs.overrides import MAX_SEED

if TYPE_CHECKING:
    from framecontrol.jobs.events import RunStatus
    from framecontrol.jobs.parsing import JobParser
    from framecontrol.jobs.planning import PlannedRun
    from framecontrol.jobs.records import RunRecord

H3_FPS = 24
H3_INFO_TIMEOUT_SECONDS = 30
H3_COLOR_POLICY_VERSION = 1
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


def _inspect_reference(kind: str, path: Path, audio_path: Path | None) -> dict[str, Any]:
    """Decode a declared reference and validate the streams H3 will consume."""
    if kind == "image":
        size, orientation = read_image_info(path)
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

    def decode(source: Path, selector: str) -> None:
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
        decode(path, "0:v:0")
        embedded_audio = None
        if kind == "video" and audio_streams:
            decode(path, "0:a:0")
            embedded_audio = float(audio_streams[0].get("duration") or video_duration)
            if not 2 <= embedded_audio <= 15:
                raise ValueError("embedded audio duration must be from 2 to 15 seconds")
            embedded_audio = min(embedded_audio, video_duration, 362 / H3_FPS)
        separate_audio = None
        if kind == "video_audio":
            assert audio_path is not None
            separate_audio = _inspect_audio(audio_path, ffprobe, ffmpeg)
            separate_audio = min(separate_audio, video_duration, 362 / H3_FPS)
        return {"video": video_duration, "audio": embedded_audio if kind == "video" else separate_audio}
    audio_duration = _inspect_audio(path, ffprobe, ffmpeg)
    return {"video": None, "audio": audio_duration}


def _inspect_audio(path: Path, ffprobe: str, ffmpeg: str) -> float:
    result = run_process((ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)), capture_output=True, text=True, errors="replace", timeout=20, check=False)
    if result.returncode != 0:
        raise ValueError(f"cannot inspect audio: {result.stderr.strip() or 'ffprobe failed'}")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    duration = float(audio[0].get("duration") or payload.get("format", {}).get("duration") or 0.0) if len(audio) == 1 else 0.0
    if len(audio) != 1 or not math.isfinite(duration) or not 2 <= duration <= 15:
        raise ValueError("must contain exactly one audio stream with a duration from 2 to 15 seconds")
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
            parser.fail("mode", "is not supported by h3")
        for key in ("config_file", "config_override", "desired_input_width", "desired_input_height", "max_input_crop_percent"):
            if key in data:
                parser.fail(key, "is not supported by h3")
        for index, pair in enumerate(data["prompt_pairs"]):
            if isinstance(pair, dict) and "negative" in pair:
                parser.fail(f"prompt_pairs[{index}].negative", "is not supported by h3")
        settings = self._settings(parser, data.get("backend_settings"))
        if common.mode is GenerationMode.H3AV:
            runs = self._runs(parser, data.get("h3_runs"), common.run_count)
            reference_pairs = self._reference_pairs(parser, data.get("reference_pairs"), runs, settings.effective_frames)
        else:
            if "h3_runs" in data or "reference_pairs" in data:
                parser.fail("h3_runs", "and reference_pairs are only supported in h3av jobs")
            runs, reference_pairs = tuple(H3Run(run, "i2av") for run in range(1, common.run_count + 1)), ()
        color = _color_policy(parser, data["color"], any(run.pipeline == "i2av" for run in runs)) if "color" in data else ColorPolicy(regions=False)
        if settings.reference_image_size == "max" and not any(run.pipeline == "r2av" for run in runs):
            parser.fail("backend_settings.reference_image_size", "max requires at least one r2av run")
        output = data.get("output", {})
        directory = parser.output_directory(output, common.name, {"directory", "extension"})
        extension = output.get("extension", "mp4")
        if extension != "mp4":
            parser.fail("output.extension", "must be mp4 for h3")
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
        return JobDefinition(path=parser.path, name=common.name, mode=common.mode, input=common.input, run_count=common.run_count, prompt_pairs=common.prompt_pairs, output_directory=directory.resolve(), extension="mp4", run_timeout_seconds=common.run_timeout_seconds, requested_seed=settings.seed, requested_seed_source="backend_settings", backend=self.id, backend_data=H3JobData(settings, runs, reference_pairs, parser._global_config.input_directory.resolve(), parser._global_config.api_limits.max_reference_file_bytes, parser._global_config.api_limits.max_reference_bytes), input_resize=plan, color=color, cooldown=common.cooldown, cooldown_source=common.cooldown_source, source_text=source_text)

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
                paths: dict[str, Path] = {}
                for key in ("path", "audio") if kind == "video_audio" else ("path",):
                    raw = entry[key]
                    key_field = f"{entry_field}.{key}"
                    if not isinstance(raw, str) or not raw.strip():
                        parser.fail(key_field, "must be a nonempty file path")
                    path = parser.reference_override(name, entry_index, key)
                    snapshotted = path is not None
                    if parser.require_reference_snapshots and not snapshotted:
                        parser.fail(key_field, "has no saved reference snapshot")
                    if path is None:
                        input_directory = parser._global_config.input_directory
                        lexical = Path(raw.strip()).expanduser()
                        lexical = lexical if lexical.is_absolute() else input_directory / lexical
                        link = linked_component(lexical, input_directory)
                        if link is not None:
                            parser.fail(key_field, f"must not use symbolic links: {link}")
                        path = parser._confined_path(raw.strip(), parser._global_config.input_directory, key_field)
                    if not inactive and (not path.is_file() or path.is_symlink()):
                        parser.fail(key_field, "must name an existing regular file and must not be a symbolic link")
                    if not snapshotted:
                        size = path.stat().st_size
                        file_limit = parser._global_config.api_limits.max_reference_file_bytes
                        if size > file_limit:
                            parser.fail(key_field, f"is {size} bytes, over the max_reference_file_bytes limit of {file_limit}")
                        aggregate_bytes += size
                        total_limit = parser._global_config.api_limits.max_reference_bytes
                        if aggregate_bytes > total_limit:
                            parser.fail(key_field, f"makes reference assets exceed the max_reference_bytes limit of {total_limit}")
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
                        duration = _inspect_reference(kind, paths["path"], audio_path)
                    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
                        media_field = "audio" if kind == "video_audio" and audio_path is not None and "audio" in str(error).lower() else "path"
                        parser.fail(f"{entry_field}.{media_field}", str(error))
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

    def child_output(self, run: PlannedRun) -> Path:
        return _partial_path(run.output)

    def arguments(self, job: JobDefinition, pair: Any, run_input: Path | None, output: Path, seed: int, executable: str, run_number: int = 1) -> H3Arguments:
        data = cast(H3JobData, job.backend_data)
        settings = data.settings
        # The job seed is fixed by validated H3 settings; an input seed supplied
        # by the common chain must agree, including resume.
        if seed != settings.seed:
            raise ValueError("H3 chain seed differs from backend_settings.seed")
        if self._ffmpeg is None or self._ffprobe is None:
            raise ValueError("H3 media tools were not resolved")
        run = data.runs[run_number - 1]
        selected = next((item for item in data.reference_pairs if item.name == run.reference_pair), None)
        if run.pipeline == "r2av" and selected is None:
            raise ValueError(f"H3 run {run_number} has no selected reference pair")
        refs = tuple((item.kind, item.path, item.audio) for item in selected.references) if selected is not None else ()
        if selected is not None:
            for item in selected.references:
                self._reference_labels[str(item.path)] = item.basename or item.path.name
                if item.audio is not None:
                    self._reference_labels[str(item.audio)] = item.audio_basename or item.audio.name
        return H3Arguments(self.runtime.executable, self.runtime.model_directory, pair.positive, run.pipeline, refs, run_input if run.pipeline == "i2av" else None, _partial_path(output), settings, self._ffmpeg, self._ffprobe)

    def prepare_run(self, job: JobDefinition, context: RunContext) -> RunPreparation:
        data = cast(H3JobData, job.backend_data)
        if data.runs[context.number - 1].pipeline == "r2av":
            return RunPreparation(finish_state=H3ColorState(None, None, "r2av boundary"), chain_anchor=None)
        previous_r2av = context.number > 1 and data.runs[context.number - 2].pipeline == "r2av"
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

    def finish(self, job: JobDefinition, run: PlannedRun, record: RunRecord, stop_requested: Callable[[], signal.Signals | None], finish_state: object | None, media: MediaTools, report: Callable[[int, MediaCheck], None] | None) -> tuple[RunStatus, int] | None:
        partial = _partial_path(run.output)
        staged_handoff = run.last_frame.with_name(f".{run.last_frame.name}.partial.png") if run.last_frame is not None else None
        staged_delivery = run.output.with_name(f".{run.output.name}.delivery.partial.mp4")
        staged_native = run.output.with_name(f".{run.output.name}.native.partial.mp4")
        staged_raw = run.output.with_name(f".{run.output.name}.raw.partial.mp4")
        staged_corrected = run.output.with_name(f".{run.output.name}.corrected.partial.mp4")
        from framecontrol.jobs.events import RunStatus

        try:
            if self._ffmpeg is None or self._ffprobe is None:
                raise ValueError("H3 media tools were not resolved")
            if (stop := stop_requested()) is not None:
                partial.unlink(missing_ok=True)
                return RunStatus.INTERRUPTED, exit_code_for_signal(stop)
            facts = _verify_h3_mp4(partial, cast(H3JobData, job.backend_data).settings, self._ffprobe)
        except (OSError, ValueError, subprocess.SubprocessError):
            partial.unlink(missing_ok=True)
            return RunStatus.FAILED, 1

        warnings: list[str] = []
        color_facts: dict[str, Any] = {"policy_version": H3_COLOR_POLICY_VERSION, "policy": job.color.as_dict(), "source_executable_sha256": None, "source_declarations": None, "source_interpretation": "unavailable", "delivery": "original"}
        color_state = finish_state if isinstance(finish_state, H3ColorState) else H3ColorState(run.input, None, None)
        color_facts["segment_anchor"] = color_state.segment_anchor.name if color_state.segment_anchor is not None else None
        color_facts["reanchor_reason"] = color_state.reanchor_reason
        delivery = partial
        main_output = partial
        known_color = False
        h3_pipeline = cast(H3JobData, job.backend_data).runs[run.number - 1].pipeline
        raw_output = raw_output_path(run.output) if job.color.corrects and job.color.strength > 0 and h3_pipeline == "i2av" else None
        uncorrected_output = uncorrected_output_path(run.output) if raw_output is not None else None
        try:
            try:
                fingerprint = _file_sha256(self.runtime.executable)
                color_facts["source_executable_sha256"] = fingerprint
                declarations = inspect_h3_declarations(partial, self._ffmpeg, self._ffprobe)
                color_facts["source_declarations"] = declarations.facts()
                declared = ", ".join(part for part in (f"container {declarations.container.text()}" if declarations.container else f"container {declarations.unsupported_container_kind}" if declarations.unsupported_container_kind else "", f"H.264 {declarations.bitstream.text()}" if declarations.bitstream else "") if part)
                if fingerprint != H3_RGB_CONVENTION_SHA256:
                    warnings.append("H3 color interpretation is unavailable for this executable fingerprint; original MP4 retained" + (f" with existing {declared}" if declared else " without new tags"))
                elif not declarations_match(declarations, NATIVE, require_explicit=False):
                    warnings.append(f"H3 child has conflicting color declarations ({declared or 'stream tags'}); original MP4 and those tags retained, with no safe handoff")
                else:
                    known_color = True
                    color_facts["source_interpretation"] = "assumed sRGB transfer/BT.709 primaries, measured BT.601 limited matrix/range"
            except (OSError, ValueError, subprocess.SubprocessError):
                warnings.append("H3 color interpretation is unavailable: executable or declaration inspection failed; original MP4 retained")

            if raw_output is not None:
                shutil.copyfile(partial, staged_raw)
                if _file_sha256(staged_raw) != _file_sha256(partial):
                    raise ValueError("H3 raw child copy did not match the child MP4")
                color_facts["raw_output"] = raw_output.name

            if known_color:
                try:
                    convert_h3_delivery(partial, staged_delivery, self._ffmpeg, stop_requested)
                    converted_facts = _verify_h3_mp4(staged_delivery, cast(H3JobData, job.backend_data).settings, self._ffprobe)
                    converted_tags = inspect_h3_declarations(staged_delivery, self._ffmpeg, self._ffprobe)
                    if not declarations_match(converted_tags, DELIVERY, require_explicit=True):
                        raise ValueError("Rec.709 delivery declarations are absent or conflicting")
                    if packet_facts(partial, self._ffprobe, "a:0") != packet_facts(staged_delivery, self._ffprobe, "a:0"):
                        raise ValueError("Rec.709 delivery changed native AAC packets or timing")
                    delivery, facts = staged_delivery, converted_facts
                    color_facts["delivery"] = "converted_rec709"
                    color_facts["delivery_declarations"] = converted_tags.facts()
                except (OSError, ValueError, subprocess.SubprocessError):
                    staged_delivery.unlink(missing_ok=True)
                    warnings.append("H3 Rec.709 delivery conversion or verification failed")
                    try:
                        shutil.copyfile(partial, staged_native)
                        added_tag = tag_video_color_codes(staged_native, primaries=1, transfer=13, matrix=5, full_range=False)
                        if not added_tag and read_colr(staged_native) != NATIVE:
                            raise ValueError("H3 native color tag could not be added")
                        native_tags = inspect_h3_declarations(staged_native, self._ffmpeg, self._ffprobe)
                        if native_tags.container != NATIVE or not declarations_match(native_tags, NATIVE, require_explicit=False):
                            raise ValueError("H3 native color tag failed verification")
                        if any(packet_facts(partial, self._ffprobe, stream) != packet_facts(staged_native, self._ffprobe, stream) for stream in ("v:0", "a:0")):
                            raise ValueError("H3 native tagging changed media packets or timing")
                        delivery = staged_native
                        color_facts["delivery"] = "native_tagged"
                        color_facts["delivery_declarations"] = native_tags.facts()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        staged_native.unlink(missing_ok=True)
                        warnings.append("H3 native color tagging or verification failed; original MP4 retained")

            main_output = delivery
            drift_warning: str | None = None
            if known_color:
                try:
                    color_facts["drift"] = measure_h3_drift(delivery, facts["frames"], facts["width"], facts["height"], self._ffmpeg, rec709_delivery=delivery == staged_delivery, run_input=color_state.run_input, segment_anchor=color_state.segment_anchor, stop_requested=stop_requested, regions=job.color.regions, segmenter_factory=media.segmenter_factory)
                except (OSError, ValueError, subprocess.SubprocessError):
                    drift_warning = "H3 color drift measurement is unavailable: frame decode or measurement failed"
                    color_facts["drift"] = {"unavailable": "frame decode or measurement failed"}
            else:
                drift_warning = "H3 color drift measurement is unavailable: source pixel interpretation is unknown"
                color_facts["drift"] = {"unavailable": "source pixel interpretation is unknown"}
            if drift_warning is not None:
                warnings.append(drift_warning)

            color_facts["correction"] = {"status": "not_applicable", "reason": "r2av has no first-frame input"} if h3_pipeline == "r2av" else {"status": "off"}
            if h3_pipeline == "i2av" and job.color.corrects and job.color.strength == 0:
                color_facts["correction"] = {"status": "measured_identity", "strength": 0}
            if raw_output is not None:
                if not known_color:
                    color_facts["correction"] = {"status": "skipped", "reason": "unknown source pixel interpretation"}
                    warnings.append("H3 color correction was skipped: source pixel interpretation is unknown")
                elif color_state.run_input is None:
                    color_facts["correction"] = {"status": "skipped", "reason": "prepared i2av input unavailable"}
                    warnings.append("H3 color correction was skipped: prepared i2av input unavailable")
                else:
                    try:
                        # Import numerical correction only for an opt-in correcting run.
                        from framecontrol.jobs.media.h3_correction import correct_h3_video

                        correction_facts = correct_h3_video(delivery, staged_corrected, self._ffmpeg, frames=facts["frames"], width=facts["width"], height=facts["height"], run_input=color_state.run_input, segment_anchor=color_state.segment_anchor, pull=job.color.pull, strength=job.color.strength, rec709_delivery=delivery == staged_delivery, stop_requested=stop_requested, regions=job.color.regions, segmenter_factory=media.segmenter_factory)
                        corrected_facts = _verify_h3_mp4(staged_corrected, cast(H3JobData, job.backend_data).settings, self._ffprobe)
                        corrected_tags = inspect_h3_declarations(staged_corrected, self._ffmpeg, self._ffprobe)
                        if not declarations_match(corrected_tags, DELIVERY, require_explicit=True):
                            raise ValueError("corrected H3 MP4 has missing or conflicting Rec.709 declarations")
                        if packet_facts(delivery, self._ffprobe, "a:0") != packet_facts(staged_corrected, self._ffprobe, "a:0"):
                            raise ValueError("corrected H3 MP4 changed native AAC packets or timing")
                        main_output, facts = staged_corrected, corrected_facts
                        color_facts["correction"] = {"status": "succeeded", **correction_facts}
                        color_facts["corrected_declarations"] = corrected_tags.facts()
                        if correction_facts.get("region_fallback") is not None:
                            warnings.append("H3 region correction is unavailable; verified whole-frame fallback used")
                        color_facts["corrected_output"] = run.output.name
                        if uncorrected_output is not None:
                            color_facts["uncorrected_output"] = uncorrected_output.name
                    except (OSError, ValueError, subprocess.SubprocessError):
                        staged_corrected.unlink(missing_ok=True)
                        color_facts["correction"] = {"status": "failed", "reason": "correction or audiovisual verification failed"}
                        warnings.append("H3 color correction failed; verified uncorrected delivery retained")

            if staged_handoff is not None and known_color:
                try:
                    _extract_h3_frame(main_output, staged_handoff, self._ffmpeg)
                    decode_image(staged_handoff)
                except (OSError, ValueError, subprocess.SubprocessError):
                    warnings.append("H3 handoff is unavailable: extraction or image validation failed")
                    staged_handoff.unlink(missing_ok=True)
            if (stop := stop_requested()) is not None:
                return RunStatus.INTERRUPTED, exit_code_for_signal(stop)
            if raw_output is not None:
                os.replace(staged_raw, raw_output)
                record.raw_output = raw_output.name
            if main_output == staged_corrected and uncorrected_output is not None:
                os.replace(delivery, uncorrected_output)
                record.uncorrected_output = uncorrected_output.name
                record.corrected_output = run.output.name
            os.replace(main_output, run.output)
            if staged_handoff is not None and staged_handoff.is_file() and run.last_frame is not None:
                try:
                    os.replace(staged_handoff, run.last_frame)
                    record.last_frame = run.last_frame.name
                except OSError:
                    warnings.append("H3 handoff is unavailable: promotion failed")
                    staged_handoff.unlink(missing_ok=True)
            corrected = record.corrected_output is not None
            color_facts["artifacts"] = {"main": {"basename": run.output.name, "role": "corrected" if corrected else "uncorrected"}, "raw_child": raw_output.name if record.raw_output is not None and raw_output is not None else None, "uncorrected_delivery": uncorrected_output.name if record.uncorrected_output is not None and uncorrected_output is not None else None, "handoff": record.last_frame}
            color_facts["handoff_source"] = "corrected_main" if corrected and record.last_frame is not None else "uncorrected_main" if record.last_frame is not None else None
            record.output_width, record.output_height, record.output_frames = facts["width"], facts["height"], facts["frames"]
            record.output_frame_rate, record.output_duration = facts["frame_rate"], facts["video_duration"]
            record.audio_codec, record.audio_sample_rate, record.audio_channels, record.audio_duration = facts["audio_codec"], facts["audio_sample_rate"], facts["audio_channels"], facts["audio_duration"]
            if report is not None:
                data = cast(H3JobData, job.backend_data)
                h3_run = data.runs[run.number - 1]
                selected = next((item for item in data.reference_pairs if item.name == h3_run.reference_pair), None)
                facts["pipeline"] = h3_run.pipeline
                facts["reference_pair"] = h3_run.reference_pair
                facts["references"] = [{"ordinal": ordinal, "kind": item.kind, "basename": item.basename or item.path.name, "sha256": item.sha256, **({"audio_basename": item.audio_basename or item.audio.name, "audio_sha256": item.audio_sha256} if item.audio else {})} for ordinal, item in enumerate(selected.references, 1)] if selected is not None else []
                facts["handoff_for_run"] = run.number + 1 if record.last_frame is not None else None
                facts["color"] = color_facts
                role_notes = tuple(f"{label}: {name}" for label, name in (("raw child", record.raw_output), ("uncorrected delivery", record.uncorrected_output), ("main corrected output", run.output.name if corrected else None), ("next handoff", record.last_frame)) if name is not None)
                summary = "H3 corrected H.264/AAC MP4 verified" if corrected else "H3 H.264/AAC MP4 verified"
                report(run.number, MediaCheck("output", run.output.name, summary, warnings=tuple(warnings), notes=role_notes, facts=facts))
                report(run.number, MediaCheck("color_drift", run.output.name, "H3 within-run and segment color drift", warnings=(drift_warning,) if drift_warning is not None else (), facts={"drift": color_facts["drift"], "segment_anchor": color_facts["segment_anchor"], "reanchor_reason": color_facts["reanchor_reason"]}))
                if job.color.corrects:
                    correction_status = color_facts["correction"]["status"]
                    correction_warnings = tuple(warning for warning in warnings if "color correction" in warning or "region correction" in warning)
                    report(run.number, MediaCheck("color_correction", run.output.name, f"H3 color correction {correction_status}", warnings=correction_warnings, facts={"correction": color_facts["correction"], "artifacts": color_facts["artifacts"], "handoff_source": color_facts["handoff_source"]}))
            return None
        except H3ColorCancelled as cancelled:
            return RunStatus.INTERRUPTED, exit_code_for_signal(cancelled.received_signal)
        except (OSError, ValueError, subprocess.SubprocessError):
            run.output.unlink(missing_ok=True)
            if raw_output is not None:
                raw_output.unlink(missing_ok=True)
            if uncorrected_output is not None:
                uncorrected_output.unlink(missing_ok=True)
            if run.last_frame is not None:
                run.last_frame.unlink(missing_ok=True)
            return (RunStatus.FAILED, 1)
        finally:
            for staged in (partial, staged_delivery, staged_native, staged_raw, staged_corrected):
                staged.unlink(missing_ok=True)
            if staged_handoff is not None:
                staged_handoff.unlink(missing_ok=True)


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
    result = run_process(command, capture_output=True, text=True, errors="replace", timeout=30, check=False)
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
    tag = read_colr(video)
    color_filter = DELIVERY_TO_SRGB_FILTER if tag == DELIVERY else NATIVE_TO_SRGB_FILTER
    color_filter += ",setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc"
    result = run_process([ffmpeg, "-v", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-vf", color_filter, str(target)], capture_output=True, text=True, errors="replace", timeout=30, check=False)
    if result.returncode or not target.is_file():
        raise ValueError(result.stderr.strip() or "could not extract H3 final frame")
