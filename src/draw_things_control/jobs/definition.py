"""The validated form of a job: what a job file means once it is read."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from draw_things_control.core.cooldown import DEFAULT_COOLDOWN, CooldownPolicy
from draw_things_control.core.numbers import is_int
from draw_things_control.jobs.inputs.size import ResizePlan


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
    """A named positive and optional negative prompt, and the runs that use it."""

    name: str
    positive: str
    negative: str | None
    runs: tuple[int, ...]
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
    run_count: int
    prompt_pairs: tuple[PromptPair, ...]
    output_directory: Path
    extension: str
    config_file: str
    base_config: dict[str, Any]
    config_override: ConfigOverride
    model: str
    run_timeout_seconds: float | None
    # draw-things-cli's --video-format for every run of a video job (prores4444 unless the job names another); None for an image job.
    video_format: str | None = None
    ignored_config: dict[str, Any] = field(default_factory=dict)
    # Set only when a desired_input_* key is: the size of every run, and how the first input gets there.
    size: tuple[int, int] | None = None
    input_resize: ResizePlan | None = None
    # (source, key, value) for each width or height that the desired size replaces.
    ignored_size: tuple[tuple[str, str, Any], ...] = ()
    # The wait between runs, and where it came from: job, global_config, or default.
    cooldown: CooldownPolicy = DEFAULT_COOLDOWN
    cooldown_source: str = "default"
    # The job file's text as it was loaded, so a record shows exactly what ran. Job files reject unknown keys, so it cannot hold a credential.
    # Left out of equality and repr: comments must not make two jobs differ, and a repr must not dump the file.
    source_text: str = field(default="", repr=False, compare=False)

    @property
    def input_copy(self) -> ResizePlan | None:
        """The resize plan when run 1 needs a resized or upright copy of the input, otherwise None."""
        if self.input is not None and self.input_resize is not None and self.input_resize.needs_copy:
            return self.input_resize
        return None

    def schedule(self) -> tuple[PromptPair, ...]:
        """Return the prompt pair used by each run, in run order."""
        assigned = {number: pair for pair in self.prompt_pairs for number in pair.runs}
        default = next((pair for pair in self.prompt_pairs if pair.default), None)
        return tuple(assigned.get(number, default) for number in range(1, self.run_count + 1))  # type: ignore[misc]

    def configured_seed(self) -> tuple[int | None, str]:
        """Return the seed from the job or base configuration and where it came from."""
        if self.config_override.seed is not None:
            return self.config_override.seed, "config_override"
        seed = self.base_config.get("seed")
        if is_int(seed) and seed >= 0:
            return seed, "config_file"
        return None, "random"
