"""Validate and snapshot the bounded one-off generations accepted by ``dtc serve``."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, cast

from draw_things_control.core.arguments import DEFAULT_VIDEO_FORMAT, DrawThingsGenerateArguments
from draw_things_control.core.draw_things_config import find_config_file, load_config
from draw_things_control.core.errors import InputError, LimitExceededError, OutsideDirectoryError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.yaml_files import read_bounded_bytes

_FIELDS = frozenset({"model", "prompt", "negative_prompt", "steps", "cfg", "width", "height", "frames", "strength", "seed", "config_file", "image", "audio", "avc", "segment_frames", "cond_frames", "output", "video_format", "timeout"})


@dataclass(frozen=True)
class GenerationSnapshot:
    """The self-contained, already-confined request a queue worker runs once."""

    model: str
    output: str
    output_path: str
    timeout: float
    prompt: str | None = None
    negative_prompt: str | None = None
    steps: int | None = None
    cfg: float | None = None
    width: int | None = None
    height: int | None = None
    frames: int | None = None
    strength: float | None = None
    seed: int | None = None
    config_file: str | None = None
    config_text: str | None = None
    image: tuple[str, ...] = ()
    audio: str | None = None
    avc: bool = False
    segment_frames: int | None = None
    cond_frames: int | None = None
    video_format: str | None = None

    @property
    def display_name(self) -> str:
        return f"generate: {self.output}"

    def arguments(self, executable: str) -> DrawThingsGenerateArguments:
        images = tuple(Path(value) for value in self.image)
        return DrawThingsGenerateArguments(model=self.model, executable=executable, prompt=self.prompt, negative_prompt=self.negative_prompt, steps=self.steps, cfg=self.cfg, width=self.width, height=self.height, frames=self.frames, strength=self.strength, seed=self.seed, config_json=self.config_text, image=images[0] if images else None, reference_images=images[1:], audio=Path(self.audio) if self.audio is not None else None, avc=self.avc, segment_frames=self.segment_frames, cond_frames=self.cond_frames, output=Path(self.output_path), video_format=self.video_format)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "kind": "generate", "generation": asdict(self)}, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str | None) -> GenerationSnapshot:
        try:
            data = json.loads(text or "")
            generation = data["generation"]
        except (TypeError, ValueError, KeyError) as error:
            raise InputError("Generation snapshot is invalid") from error
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("kind") != "generate" or not isinstance(generation, dict) or set(generation) != {field.name for field in fields(cls)}:
            raise InputError("Generation snapshot is invalid")
        try:
            snapshot = cls(**cast(Any, {key: (tuple(value) if key == "image" and isinstance(value, list) else value) for key, value in generation.items()}))
            if not isinstance(snapshot.model, str) or not snapshot.model or not isinstance(snapshot.output, str) or not snapshot.output or not isinstance(snapshot.output_path, str) or not snapshot.output_path or isinstance(snapshot.timeout, bool) or not isinstance(snapshot.timeout, (int, float)) or snapshot.timeout <= 0 or not isinstance(snapshot.image, tuple) or not all(isinstance(path, str) for path in snapshot.image):
                raise ValueError
            snapshot.arguments("draw-things-cli")
            return snapshot
        except (TypeError, ValueError) as error:
            raise InputError("Generation snapshot is invalid") from error


def parse_generation(body: dict[str, Any], settings: GlobalConfig, params_directory: Path) -> GenerationSnapshot:
    """Turn a JSON request into a safe snapshot before any media file is opened."""
    unknown = sorted(set(body) - _FIELDS)
    if unknown:
        raise InputError(f"Unknown generation field '{unknown[0]}'", field=unknown[0])
    _no_nuls(body)
    _check_text_limits(body, settings.api_limits.max_job_file_bytes)
    timeout = _number(body.get("timeout"), "timeout")
    if timeout <= 0:
        raise InputError("'timeout' must be positive", field="timeout")
    if timeout > settings.api_limits.max_job_seconds:
        raise LimitExceededError(f"a timeout of {timeout:g} seconds is over the max_job_seconds limit of {settings.api_limits.max_job_seconds:g}", key="max_job_seconds", limit=settings.api_limits.max_job_seconds, value=timeout)
    output, output_path = _output(body.get("output"), settings.output_directory)
    images_value = body.get("image", [])
    if not isinstance(images_value, list) or not all(isinstance(value, str) for value in images_value):
        raise InputError("'image' must be an array of input-relative paths", field="image")
    images = tuple(str(_input(value, settings.input_directory, "image")) for value in images_value)
    audio_value = body.get("audio")
    audio = str(_input(audio_value, settings.input_directory, "audio")) if audio_value is not None else None
    config_file, config_text, config_model = _config(body.get("config_file"), params_directory, settings.api_limits.max_job_file_bytes)
    model = body.get("model") or config_model
    if not isinstance(model, str) or not model:
        raise InputError("A model must be set in the configuration or passed with --model", field="model")
    try:
        arguments = DrawThingsGenerateArguments(model=model, prompt=_text(body, "prompt"), negative_prompt=_text(body, "negative_prompt"), steps=_integer(body, "steps"), cfg=_optional_number(body, "cfg"), width=_integer(body, "width"), height=_integer(body, "height"), frames=_integer(body, "frames"), strength=_optional_number(body, "strength"), seed=_integer(body, "seed"), config_json=config_text, image=Path(images[0]) if images else None, reference_images=tuple(Path(value) for value in images[1:]), audio=Path(audio) if audio is not None else None, avc=_boolean(body, "avc"), segment_frames=_integer(body, "segment_frames"), cond_frames=_integer(body, "cond_frames"), output=output_path, video_format=_text(body, "video_format"))
    except ValueError as error:
        raise InputError(str(error)) from error
    video_format = arguments.video_format or (DEFAULT_VIDEO_FORMAT if output.suffix.lower() == ".mov" else None)
    return GenerationSnapshot(model=arguments.model, output=output.as_posix(), output_path=str(output_path), timeout=timeout, prompt=arguments.prompt, negative_prompt=arguments.negative_prompt, steps=arguments.steps, cfg=arguments.cfg, width=arguments.width, height=arguments.height, frames=arguments.frames, strength=arguments.strength, seed=arguments.seed, config_file=config_file, config_text=config_text, image=images, audio=audio, avc=arguments.avc, segment_frames=arguments.segment_frames, cond_frames=arguments.cond_frames, video_format=video_format)


def _no_nuls(value: Any) -> None:
    if isinstance(value, str):
        if "\0" in value:
            raise InputError("Text must not contain NUL")
    elif isinstance(value, list):
        for item in value:
            _no_nuls(item)
    elif isinstance(value, dict):
        for item in value.values():
            _no_nuls(item)


def _check_text_limits(value: Any, maximum: int) -> None:
    if isinstance(value, str) and len(value.encode("utf-8")) > maximum:
        raise LimitExceededError(f"Text is over the limit of {maximum} bytes", key="max_job_file_bytes", limit=maximum, value=len(value.encode("utf-8")))
    if isinstance(value, list):
        for item in value:
            _check_text_limits(item, maximum)
    elif isinstance(value, dict):
        for item in value.values():
            _check_text_limits(item, maximum)


def _text(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise InputError(f"'{name}' must be text", field=name)
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"'{name}' is required and must be a number", field=name)
    return float(value)


def _optional_number(body: dict[str, Any], name: str) -> float | None:
    return None if body.get(name) is None else _number(body[name], name)


def _integer(body: dict[str, Any], name: str) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"'{name}' must be a whole number", field=name)
    return value


def _boolean(body: dict[str, Any], name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise InputError(f"'{name}' must be true or false", field=name)
    return value


def _input(value: Any, directory: Path, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise InputError(f"'{field}' must be an input-relative path", field=field)
    path = _confined(value, directory, field)
    if not path.is_file():
        raise InputError(f"{field.capitalize()} does not exist or is not a file: {value}", field=field)
    return path


def _output(value: Any, directory: Path) -> tuple[Path, Path]:
    if not isinstance(value, str) or not value:
        raise InputError("'output' is required and must be an output-relative path", field="output")
    relative = Path(value)
    path = _confined(value, directory, "output")
    if not path.parent.is_dir():
        raise InputError(f"Output directory does not exist: {relative.parent}", field="output")
    return relative, path


def _confined(value: str, directory: Path, field: str) -> Path:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise OutsideDirectoryError(f"'{field}' must be relative to {directory}", field=field)
    root = directory.resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise OutsideDirectoryError(f"'{field}' is outside {directory}", field=field) from error
    return path


def _config(value: Any, directory: Path, maximum: int) -> tuple[str | None, str | None, str | None]:
    if value is None:
        return None, None, None
    if not isinstance(value, str):
        raise InputError("'config_file' must be a file name", field="config_file")
    try:
        path = find_config_file(value, directory)
        read_bounded_bytes(path, maximum, "Configuration", key="config_file").decode("utf-8")
        data = load_config(path, max_bytes=maximum)
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError(str(error), field="config_file") from error
    return value, json.dumps(data, ensure_ascii=False, separators=(",", ":")), data.get("model") if isinstance(data.get("model"), str) else None
