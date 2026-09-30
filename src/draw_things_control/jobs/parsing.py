"""Read a job file, or its text, and validate everything that can be checked before running."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from draw_things_control.core import draw_things_config
from draw_things_control.core.arguments import DEFAULT_VIDEO_FORMAT, VIDEO_FORMATS
from draw_things_control.core.cooldown import DEFAULT_COOLDOWN, CooldownPolicy, parse_cooldown, replaced_cooldown_message
from draw_things_control.core.errors import InputError
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.numbers import is_int, is_number
from draw_things_control.core.yaml_files import parse_yaml_mapping, read_yaml_file
from draw_things_control.jobs.definition import ConfigOverride, GenerationMode, JobDefinition
from draw_things_control.jobs.inputs.size import MAX_DESIRED_SIZE, ResizePlan, check_input_size, decode_image, read_image_info, resize_plan
from draw_things_control.jobs.overrides import MAX_SEED, parse_config_override
from draw_things_control.jobs.prompt_pairs import NAME_PATTERN, parse_prompt_pairs

SIZE_KEYS = ("desired_input_width", "desired_input_height")
JOB_KEYS = {"version", "name", "mode", "input", "run_count", "prompt_pairs", "output", "config_file", "config_override", "run_timeout_seconds", *SIZE_KEYS, "max_input_crop_percent", "cooldown"}
REQUIRED_JOB_KEYS = ("version", "name", "mode", "run_count", "prompt_pairs", "config_file")
OUTPUT_KEYS = {"directory", "extension", "video_format"}
# Base configuration keys each mode drops, because the job itself decides them.
IGNORED_CONFIG_KEYS = {"i2v": ("batchCount",)}
# The desired_input_* keys and max_input_crop_percent, as (width, height, crop percent).
DesiredSize = tuple[int | None, int | None, float | None]


def load_job(path: Path, global_config: GlobalConfig, params_directory: Path, *, decode_input: bool = True) -> JobDefinition:
    """Parse a job file and validate everything that can be checked before running.

    When run 1 needs a resized copy, the input is fully decoded to catch broken pixel data. A caller that
    writes the copy right away passes ``decode_input=False``, since writing it decodes the input anyway.
    """
    path = path.expanduser().resolve()
    data, text = read_yaml_file(path, "Job file", show_source=True)
    return JobParser(path, global_config, params_directory, decode_input=decode_input).parse(data, text)


def load_job_text(text: str, path: Path, global_config: GlobalConfig, params_directory: Path, *, decode_input: bool = True, base_config_text: str | None = None) -> JobDefinition:
    """Parse a job from its text, as a queue keeps it; ``path`` names the job file in messages and in the definition, and is not read.

    With ``base_config_text``, the base configuration is parsed from this text instead of read from
    ``params_directory``: a queued job's snapshot, so editing or deleting the base configuration after
    submission changes nothing about what runs.
    """
    data = parse_yaml_mapping(text, path, "Job file", show_source=True)
    return JobParser(path, global_config, params_directory, decode_input=decode_input, base_config_text=base_config_text).parse(data, text)


class JobParser:
    """Validates one job file's mapping, one method per part of the file; every problem is an InputError naming the file and the field."""

    def __init__(self, path: Path, global_config: GlobalConfig, params_directory: Path, *, decode_input: bool = True, base_config_text: str | None = None) -> None:
        self._path = path
        self._global_config = global_config
        self._params_directory = params_directory
        self._decode_input = decode_input
        self._base_config_text = base_config_text

    def parse(self, data: dict[str, Any], source_text: str) -> JobDefinition:
        self._check_top_level(data)
        name = self._name(data["name"])
        mode = self._mode(data["mode"])
        input_path = self._input_path(data, mode)
        run_count = self._run_count(data["run_count"])
        prompt_pairs = parse_prompt_pairs(data["prompt_pairs"], run_count, self._fail, self._check_keys)
        output_directory, extension, video_format = self._output(data.get("output", {}), mode, name)
        config_file = self._config_file(data["config_file"])
        base_config = self._base_config(config_file)
        ignored_config = {key: base_config.pop(key) for key in IGNORED_CONFIG_KEYS.get(mode, ()) if key in base_config}
        override = parse_config_override(data.get("config_override", {}), mode, self._fail, self._check_keys)
        model = self._model(override, base_config, config_file)
        timeout = self._timeout(data.get("run_timeout_seconds"))
        cooldown, cooldown_source = self._cooldown(data)
        desired = self._desired_size(data, mode)
        plan, ignored_size = self._input_size(input_path, desired, override, base_config, config_file)
        return JobDefinition(
            path=self._path,
            name=name,
            mode=mode,
            input=input_path,
            run_count=run_count,
            prompt_pairs=prompt_pairs,
            output_directory=output_directory,
            extension=extension,
            video_format=video_format,
            config_file=config_file,
            base_config=base_config,
            config_override=override,
            model=model,
            run_timeout_seconds=timeout,
            cooldown=cooldown,
            cooldown_source=cooldown_source,
            ignored_config=ignored_config,
            size=plan.target_size if plan is not None else None,
            input_resize=plan,
            ignored_size=ignored_size,
            source_text=source_text,
        )

    def _fail(self, field: str, problem: str) -> NoReturn:
        raise InputError(f"{self._path}: '{field}' {problem}", field=field, path=self._path)

    def _wrap(self, error: ValueError) -> InputError:
        """An error from a lower layer, with the job file's name in front."""
        return InputError(f"{self._path}: {error}", path=self._path)

    def _check_keys(self, data: dict[str, Any], allowed: set[str], prefix: str) -> None:
        for key in data:
            if key not in allowed:
                self._fail(f"{prefix}{key}", "is not a known key")

    def _check_top_level(self, data: dict[str, Any]) -> None:
        if "cooldown_seconds" in data:
            raise InputError(f"{self._path}: {replaced_cooldown_message(data['cooldown_seconds'])}", field="cooldown_seconds", path=self._path)
        self._check_keys(data, JOB_KEYS, "")
        for key in REQUIRED_JOB_KEYS:
            if key not in data:
                self._fail(key, "is required")
        if data["version"] != 1 or isinstance(data["version"], bool):
            self._fail("version", "must be 1")

    def _name(self, name: Any) -> str:
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            self._fail("name", "must be 1 to 64 lowercase letters, digits, and hyphens, starting and ending with a letter or digit")
        return name

    def _mode(self, value: Any) -> GenerationMode:
        try:
            return GenerationMode(value)
        except ValueError:
            self._fail("mode", "must be one of i2i, t2v, or i2v")

    def _input_path(self, data: dict[str, Any], mode: GenerationMode) -> Path | None:
        value = data.get("input")
        if not mode.requires_input:
            if value is not None:
                self._fail("input", "is not allowed in t2v jobs; run 1 generates from text alone")
            return None
        if value is None:
            self._fail("input", f"is required in {mode} jobs")
        if not isinstance(value, str) or not value.strip():
            self._fail("input", "must be a file path")
        # Leading or trailing spaces and tabs are never part of the intended file name.
        path = Path(value.strip()).expanduser()
        path = (path if path.is_absolute() else self._global_config.input_directory / path).resolve()
        if not path.is_file():
            self._fail("input", f"file does not exist: {path}")
        return path

    def _run_count(self, run_count: Any) -> int:
        if not is_int(run_count) or run_count < 1:
            self._fail("run_count", "must be an integer >= 1")
        return run_count

    def _config_file(self, config_file: Any) -> str:
        if not isinstance(config_file, str):
            self._fail("config_file", "must be a file name")
        return config_file

    def _base_config(self, config_file: str) -> dict[str, Any]:
        try:
            if self._base_config_text is not None:
                base_config = parse_yaml_mapping(self._base_config_text, self._params_directory / config_file, "Configuration", require_json=True)
            else:
                base_config = draw_things_config.load_base_config(config_file, self._params_directory)
        except ValueError as error:
            raise self._wrap(error) from error
        base_seed = base_config.get("seed")
        if is_int(base_seed) and base_seed > MAX_SEED:
            self._fail("config_file", f"{config_file} sets seed {base_seed}, above the largest seed {MAX_SEED}")
        return base_config

    def _model(self, override: ConfigOverride, base_config: dict[str, Any], config_file: str) -> str:
        model = override.model or base_config.get("model")
        if not isinstance(model, str) or not model:
            self._fail("config_override.model", f"is required because {config_file} sets no model")
        if override.refiner_start is not None and not (override.refiner_model or base_config.get("refinerModel")):
            self._fail("config_override.refiner_start", f"requires a refiner model in config_override.refiner_model or {config_file}")
        return model

    def _timeout(self, timeout: Any) -> float | None:
        if timeout is not None and (not is_number(timeout) or timeout <= 0):
            self._fail("run_timeout_seconds", "must be a positive number of seconds")
        return float(timeout) if timeout is not None else None

    def _cooldown(self, data: dict[str, Any]) -> tuple[CooldownPolicy, str]:
        """The job's cooldown and its source: the job's mapping as a whole, else the global configuration's, else auto."""
        if "cooldown" in data:
            try:
                return parse_cooldown(data["cooldown"], "cooldown"), "job"
            except ValueError as error:
                raise self._wrap(error) from error
        if self._global_config.cooldown is not None:
            return self._global_config.cooldown, "global_config"
        return DEFAULT_COOLDOWN, "default"

    def _desired_size(self, data: dict[str, Any], mode: GenerationMode) -> DesiredSize | None:
        """Validate the desired_input_* keys and max_input_crop_percent; None when no size key is set."""
        given = [key for key in SIZE_KEYS if key in data]
        for key in given:
            if not mode.requires_input:
                self._fail(key, f"is not allowed in {mode} jobs, which have no input image")
            if not is_int(data[key]) or not 1 <= data[key] <= MAX_DESIRED_SIZE:
                self._fail(key, f"must be an integer from 1 to {MAX_DESIRED_SIZE}")
        max_crop = data.get("max_input_crop_percent")
        if max_crop is not None:
            if len(given) != 1:
                self._fail("max_input_crop_percent", "requires exactly one of desired_input_width or desired_input_height")
            if not is_number(max_crop) or not 0 <= max_crop <= 100:
                self._fail("max_input_crop_percent", "must be a number from 0 to 100")
        if not given:
            return None
        return data.get("desired_input_width"), data.get("desired_input_height"), max_crop

    def _input_size(self, input_path: Path | None, desired: DesiredSize | None, override: ConfigOverride, base_config: dict[str, Any], config_file: str) -> tuple[ResizePlan | None, tuple[tuple[str, str, Any], ...]]:
        """Check the input image against the job's size, or plan its resize to the desired size.

        Returns the resize plan (None without a desired size or input) and each width or height the desired size replaces.
        """
        if input_path is None:
            return None, ()
        image_size, orientation = read_image_info(input_path)
        if desired is None:
            job_size, source = self._job_size(override, base_config, config_file)
            check_input_size(input_path, image_size, job_size, source)
            return None, ()
        desired_width, desired_height, max_crop_percent = desired
        try:
            plan = resize_plan(input_path.name, image_size, orientation, desired_width, desired_height, max_crop_percent)
        except ValueError as error:
            raise self._wrap(error) from error
        if self._decode_input and plan.needs_copy:
            decode_image(input_path)
        ignored: list[tuple[str, str, Any]] = []
        for key in ("width", "height"):
            if getattr(override, key) is not None:
                ignored.append(("config_override", key, getattr(override, key)))
            if key in base_config:
                ignored.append(("config_file", key, base_config[key]))
        return plan, tuple(ignored)

    def _job_size(self, override: ConfigOverride, base_config: dict[str, Any], config_file: str) -> tuple[tuple[int, int], str]:
        sources = []
        size = []
        for key in ("width", "height"):
            from_override = getattr(override, key)
            if from_override is not None:
                size.append(from_override)
                sources.append("config_override")
            elif is_int(base_config.get(key)) and base_config[key] > 0:
                size.append(base_config[key])
                sources.append(f"config_file {config_file}")
            else:
                self._fail(f"config_override.{key}", f"is required because {config_file} sets no {key}; the input image is checked against it")
        source = f"width and height from {sources[0]}" if sources[0] == sources[1] else f"width from {sources[0]}, height from {sources[1]}"
        return (size[0], size[1]), source

    def _output(self, value: Any, mode: GenerationMode, name: str) -> tuple[Path, str, str | None]:
        if not isinstance(value, dict):
            self._fail("output", "must be a mapping")
        self._check_keys(value, OUTPUT_KEYS, "output.")
        directory = value.get("directory")
        if directory is None:
            output_directory = self._global_config.output_directory / name
        elif isinstance(directory, str) and directory:
            path = Path(directory).expanduser()
            output_directory = path if path.is_absolute() else self._global_config.output_directory / path
        else:
            self._fail("output.directory", "must be a directory path")
        if output_directory.exists() and not output_directory.is_dir():
            self._fail("output.directory", f"is not a directory: {output_directory}")
        extension = value.get("extension", mode.default_extension)
        if extension not in mode.allowed_extensions:
            self._fail("output.extension", f"must be {' or '.join(mode.allowed_extensions)} in {mode} jobs")
        return output_directory.resolve(), extension, self._video_format(value.get("video_format"), mode, extension)

    def _video_format(self, value: Any, mode: GenerationMode, extension: str) -> str | None:
        if not mode.is_video:
            if value is not None:
                self._fail("output.video_format", "only in video jobs")
            return None
        # ProRes 4444 by default, whatever the extension (owner decision): an mp4 must then name h264 or hevc.
        if value is None:
            value = DEFAULT_VIDEO_FORMAT
        if value not in VIDEO_FORMATS:
            self._fail("output.video_format", f"must be {', '.join(VIDEO_FORMATS)}")
        if value.startswith("prores") and extension != "mov":
            self._fail("output.video_format", f"{value} requires extension mov; set video_format to h264 or hevc for {extension}")
        return value
