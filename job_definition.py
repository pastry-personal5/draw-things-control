"""Load and validate YAML job definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger

import generation_config
from global_config import GlobalConfig, load_yaml_mapping
from input_size import check_input_size, read_image_size

NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
JOB_KEYS = {"version", "name", "mode", "input", "batch_count", "prompt_pairs", "output", "config_file", "config_override", "run_timeout_seconds"}
REQUIRED_JOB_KEYS = ("version", "name", "mode", "batch_count", "prompt_pairs", "config_file")
PAIR_KEYS = {"name", "positive", "negative", "batches", "default"}
OUTPUT_KEYS = {"directory", "extension"}
# Base configuration keys each mode drops, because the job itself decides them.
IGNORED_CONFIG_KEYS = {"i2v": ("batchCount",)}
# draw-things-cli takes a UInt32 seed.
MAX_SEED = 2**32 - 1


class GenerationMode(StrEnum):
    """The kind of generation every run of a job performs."""

    I2I = "i2i"
    T2V = "t2v"
    I2V = "i2v"

    @property
    def is_video(self) -> bool:
        return self is not GenerationMode.I2I

    @property
    def requires_input(self) -> bool:
        return self is not GenerationMode.T2V

    @property
    def default_extension(self) -> str:
        return "mov" if self.is_video else "png"

    @property
    def allowed_extensions(self) -> tuple[str, ...]:
        return ("mov", "mp4") if self.is_video else ("png",)


@dataclass(frozen=True)
class PromptPair:
    """A named positive and optional negative prompt, and the batches that use it."""

    name: str
    positive: str
    negative: str | None
    batches: tuple[int, ...]
    default: bool


@dataclass(frozen=True)
class ConfigOverride:
    """Settings that override the base configuration for every run."""

    model: str | None = None
    refiner_model: str | None = None
    refiner_start: float | None = None
    steps: int | None = None
    guidance_scale: float | None = None
    shift: float | None = None
    width: int | None = None
    height: int | None = None
    frame_count: int | None = None
    strength: float | None = None
    seed: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the overrides that are set, as written in the job file."""
        return {field.name: getattr(self, field.name) for field in fields(self) if getattr(self, field.name) is not None}


OVERRIDE_KEYS = {field.name for field in fields(ConfigOverride)}


@dataclass(frozen=True)
class JobDefinition:
    """A validated job, with paths resolved against the global directories."""

    path: Path
    name: str
    mode: GenerationMode
    input: Path | None
    batch_count: int
    prompt_pairs: tuple[PromptPair, ...]
    output_directory: Path
    extension: str
    config_file: str
    base_config: dict[str, Any]
    config_override: ConfigOverride
    model: str
    run_timeout_seconds: float | None
    ignored_config: dict[str, Any] = field(default_factory=dict)

    def schedule(self) -> tuple[PromptPair, ...]:
        """Return the prompt pair used by each batch, in batch order."""
        assigned = {batch: pair for pair in self.prompt_pairs for batch in pair.batches}
        default = next((pair for pair in self.prompt_pairs if pair.default), None)
        return tuple(assigned.get(batch, default) for batch in range(1, self.batch_count + 1))  # type: ignore[misc]

    def configured_seed(self) -> tuple[int | None, str]:
        """Return the seed from the job or base configuration and where it came from."""
        if self.config_override.seed is not None:
            return self.config_override.seed, "config_override"
        seed = self.base_config.get("seed")
        if _is_int(seed) and seed >= 0:
            return seed, "config_file"
        return None, "random"


def load_job(path: Path, global_config: GlobalConfig, dt_config_directory: Path | None = None) -> JobDefinition:
    """Parse a job file and validate everything that can be checked before running."""
    dt_config_directory = dt_config_directory or generation_config.DT_CONFIG_DIRECTORY
    path = path.expanduser().resolve()
    data = load_yaml_mapping(path, "Job file")
    fail = _Failure(path)
    _check_keys(fail, data, JOB_KEYS, "")
    for key in REQUIRED_JOB_KEYS:
        if key not in data:
            fail(key, "is required")
    if data["version"] != 1 or isinstance(data["version"], bool):
        fail("version", "must be 1")

    name = data["name"]
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        fail("name", "must be 1 to 64 lowercase letters, digits, and hyphens, starting and ending with a letter or digit")
    try:
        mode = GenerationMode(data["mode"])
    except ValueError:
        fail("mode", "must be one of i2i, t2v, or i2v")

    input_path = _input_path(fail, data, mode, global_config)
    batch_count = data["batch_count"]
    if not _is_int(batch_count) or batch_count < 1:
        fail("batch_count", "must be an integer >= 1")
    prompt_pairs = _prompt_pairs(fail, data["prompt_pairs"], batch_count)
    output_directory, extension = _output(fail, data.get("output", {}), mode, name, global_config)

    config_file = data["config_file"]
    if not isinstance(config_file, str):
        fail("config_file", "must be a file name")
    try:
        base_config = generation_config.load_base_config(config_file, dt_config_directory)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    base_seed = base_config.get("seed")
    if _is_int(base_seed) and base_seed > MAX_SEED:
        fail("config_file", f"{config_file} sets seed {base_seed}, above the largest seed {MAX_SEED}")
    ignored_config = {key: base_config.pop(key) for key in IGNORED_CONFIG_KEYS.get(mode, ()) if key in base_config}
    override = _config_override(fail, data.get("config_override", {}), mode)
    model = override.model or base_config.get("model")
    if not isinstance(model, str) or not model:
        fail("config_override.model", f"is required because {config_file} sets no model")
    if override.refiner_start is not None and not (override.refiner_model or base_config.get("refinerModel")):
        fail("config_override.refiner_start", f"requires a refiner model in config_override.refiner_model or {config_file}")

    timeout = data.get("run_timeout_seconds")
    if timeout is not None and (not _is_number(timeout) or timeout <= 0):
        fail("run_timeout_seconds", "must be a positive number of seconds")

    if input_path is not None:
        job_size, source = _job_size(fail, override, base_config, config_file)
        check_input_size(input_path, read_image_size(input_path), job_size, source)

    return JobDefinition(
        path=path,
        name=name,
        mode=mode,
        input=input_path,
        batch_count=batch_count,
        prompt_pairs=prompt_pairs,
        output_directory=output_directory,
        extension=extension,
        config_file=config_file,
        base_config=base_config,
        config_override=override,
        model=model,
        run_timeout_seconds=float(timeout) if timeout is not None else None,
        ignored_config=ignored_config,
    )


def report_ignored_config(job: JobDefinition) -> None:
    """Tell the user which base configuration keys the job's mode ignores."""
    for key, value in job.ignored_config.items():
        logger.info("Ignoring {} ({}) from config_file {}: not used in {} jobs; the job's batch_count sets the number of runs", key, value, job.config_file, job.mode)


class _Failure:
    """Raise validation errors that name the job file and the field."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, field: str, problem: str) -> Any:
        raise ValueError(f"{self._path}: '{field}' {problem}")


def _check_keys(fail: _Failure, data: dict[str, Any], allowed: set[str], prefix: str) -> None:
    for key in data:
        if key not in allowed:
            fail(f"{prefix}{key}", "is not a known key")


def _input_path(fail: _Failure, data: dict[str, Any], mode: GenerationMode, global_config: GlobalConfig) -> Path | None:
    value = data.get("input")
    if not mode.requires_input:
        if value is not None:
            fail("input", "is not allowed in t2v jobs; run 1 generates from text alone")
        return None
    if value is None:
        fail("input", f"is required in {mode} jobs")
    if not isinstance(value, str) or not value.strip():
        fail("input", "must be a file path")
    # Leading or trailing spaces and tabs are never part of the intended file name.
    path = Path(value.strip()).expanduser()
    path = (path if path.is_absolute() else global_config.input_directory / path).resolve()
    if not path.is_file():
        fail("input", f"file does not exist: {path}")
    return path


def _prompt_pairs(fail: _Failure, value: Any, batch_count: int) -> tuple[PromptPair, ...]:
    if not isinstance(value, list) or not value:
        fail("prompt_pairs", "must be a list with at least one pair")
    pairs: list[PromptPair] = []
    for index, item in enumerate(value):
        field = f"prompt_pairs[{index}]"
        if not isinstance(item, dict):
            fail(field, "must be a mapping")
        _check_keys(fail, item, PAIR_KEYS, f"{field}.")
        name = item.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            fail(f"{field}.name", "must be lowercase letters, digits, and hyphens")
        if any(pair.name == name for pair in pairs):
            fail(f"{field}.name", f"duplicates the pair name '{name}'")
        positive = item.get("positive")
        if not isinstance(positive, str) or not positive.strip():
            fail(f"{field}.positive", "is required and must be non-empty text")
        negative = item.get("negative")
        if negative is not None and not isinstance(negative, str):
            fail(f"{field}.negative", "must be text")
        batches = item.get("batches", [])
        if not isinstance(batches, list) or not all(_is_int(batch) for batch in batches):
            fail(f"{field}.batches", "must be a list of batch numbers")
        default = item.get("default", False)
        if not isinstance(default, bool):
            fail(f"{field}.default", "must be true or false")
        pairs.append(PromptPair(name=name, positive=positive, negative=negative, batches=tuple(batches), default=default))

    if sum(pair.default for pair in pairs) > 1:
        fail("prompt_pairs", "may mark at most one pair as default")
    if len(pairs) == 1 and not pairs[0].default:
        only = pairs[0]
        pairs[0] = PromptPair(name=only.name, positive=only.positive, negative=only.negative, batches=only.batches, default=True)
    assigned: dict[int, str] = {}
    for index, pair in enumerate(pairs):
        for batch in pair.batches:
            if not 1 <= batch <= batch_count:
                fail(f"prompt_pairs[{index}].batches", f"lists batch {batch}, outside 1..{batch_count}")
            if batch in assigned:
                fail(f"prompt_pairs[{index}].batches", f"lists batch {batch}, already assigned to pair '{assigned[batch]}'")
            assigned[batch] = pair.name
    if not any(pair.default for pair in pairs):
        for batch in range(1, batch_count + 1):
            if batch not in assigned:
                fail("prompt_pairs", f"assign batch {batch} to a pair, or mark one pair 'default: true'")
    return tuple(pairs)


def _output(fail: _Failure, value: Any, mode: GenerationMode, name: str, global_config: GlobalConfig) -> tuple[Path, str]:
    if not isinstance(value, dict):
        fail("output", "must be a mapping")
    _check_keys(fail, value, OUTPUT_KEYS, "output.")
    directory = value.get("directory")
    if directory is None:
        output_directory = global_config.output_directory / name
    elif isinstance(directory, str) and directory:
        path = Path(directory).expanduser()
        output_directory = path if path.is_absolute() else global_config.output_directory / path
    else:
        fail("output.directory", "must be a directory path")
    if output_directory.exists() and not output_directory.is_dir():
        fail("output.directory", f"is not a directory: {output_directory}")
    extension = value.get("extension", mode.default_extension)
    if extension not in mode.allowed_extensions:
        fail("output.extension", f"must be {' or '.join(mode.allowed_extensions)} in {mode} jobs")
    return output_directory.resolve(), extension


def _config_override(fail: _Failure, value: Any, mode: GenerationMode) -> ConfigOverride:
    if not isinstance(value, dict):
        fail("config_override", "must be a mapping")
    _check_keys(fail, value, OVERRIDE_KEYS, "config_override.")

    def field(key: str) -> str:
        return f"config_override.{key}"

    for key in ("model", "refiner_model"):
        if key in value and (not isinstance(value[key], str) or not value[key]):
            fail(field(key), "must be a non-empty model name")
    for key, low, high in (("refiner_start", 0, 1), ("strength", 0, 1)):
        if key in value and (not _is_number(value[key]) or not low <= value[key] <= high):
            fail(field(key), f"must be a number from {low} to {high}")
    for key, minimum in (("steps", 1), ("frame_count", 1)):
        if key in value and (not _is_int(value[key]) or value[key] < minimum):
            fail(field(key), f"must be an integer >= {minimum}")
    if "seed" in value and (not _is_int(value["seed"]) or not 0 <= value["seed"] <= MAX_SEED):
        fail(field("seed"), f"must be an integer from 0 to {MAX_SEED}")
    if "guidance_scale" in value and (not _is_number(value["guidance_scale"]) or value["guidance_scale"] < 0):
        fail(field("guidance_scale"), "must be a number >= 0")
    if "shift" in value and (not _is_number(value["shift"]) or value["shift"] <= 0):
        fail(field("shift"), "must be a number > 0")
    for key in ("width", "height"):
        if key in value and (not _is_int(value[key]) or value[key] <= 0 or value[key] % 64):
            fail(field(key), "must be a positive multiple of 64")
    if "frame_count" in value and not mode.is_video:
        fail(field("frame_count"), "is not allowed in i2i jobs")
    return ConfigOverride(**value)


def _job_size(fail: _Failure, override: ConfigOverride, base_config: dict[str, Any], config_file: str) -> tuple[tuple[int, int], str]:
    sources = []
    size = []
    for key in ("width", "height"):
        from_override = getattr(override, key)
        if from_override is not None:
            size.append(from_override)
            sources.append("config_override")
        elif _is_int(base_config.get(key)) and base_config[key] > 0:
            size.append(base_config[key])
            sources.append(f"config_file {config_file}")
        else:
            fail(f"config_override.{key}", f"is required because {config_file} sets no {key}; the input image is checked against it")
    source = f"width and height from {sources[0]}" if sources[0] == sources[1] else f"width from {sources[0]}, height from {sources[1]}"
    return (size[0], size[1]), source


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
