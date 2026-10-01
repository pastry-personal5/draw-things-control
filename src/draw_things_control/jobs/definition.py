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
    # Draw Things' CFG-Zero* guidance, the steps it zeroes, and its color calibration (none or lab); config-only keys.
    cfg_zero_star: bool | None = None
    cfg_zero_init_steps: int | None = None
    color_calibration: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the overrides that are set, as written in the job file."""
        return {field.name: getattr(self, field.name) for field in fields(self) if getattr(self, field.name) is not None}


OVERRIDE_KEYS = {field.name for field in fields(ConfigOverride)}

# What a correcting job's colors are held to, and when the anchor moves (Milestone 09).
COLOR_ANCHORS = ("none", "previous", "first", "blend")
REANCHOR_RULES = ("prompt_pair", "never")
# The correction's time limit per run (proposed, Milestone 09; set from the first measurements): 10 seconds plus 1 per
# frame. Running out of it is a failed correction; the API's max_job_seconds worst case adds it to each run.
CORRECTION_BASE_SECONDS = 10.0
CORRECTION_SECONDS_PER_FRAME = 1.0
# The frames assumed for the worst case when neither the job nor its configuration states a count.
ASSUMED_FRAME_COUNT = 257
# How long a run's color_drift check may decode its frames (media/drift.py), as every ffmpeg call of a check has a
# limit; an 81-frame 832x448 ProRes clip takes a few seconds. The API's max_job_seconds worst case adds it to each video
# run, correcting ones too: a correction that stops before its first pass is done leaves the check to run after it.
DRIFT_SECONDS = 300.0


def correction_limit(frames: int) -> float:
    """The correction's time limit for a clip of ``frames`` frames."""
    return CORRECTION_BASE_SECONDS + CORRECTION_SECONDS_PER_FRAME * frames


@dataclass(frozen=True)
class ColorPolicy:
    """A video job's color correction (Milestone 09), from its ``color`` block; ``none`` corrects nothing, and every
    video run still measures its drift.

    Each run is corrected back to its own input; ``first`` also pulls it all the way toward the anchor, and ``blend``
    by ``first_weight`` of the gap. The anchor is the first image, or, with ``reanchor: prompt_pair``, the input of the
    last run whose prompt pair differs from the run before it. ``strength`` scales the whole correction toward none.
    """

    anchor: str = "none"
    strength: float = 1.0
    first_weight: float = 0.25
    reanchor: str = "prompt_pair"
    # People and skin corrected apart from the background (Apple Vision); false: the whole frame as one.
    regions: bool = True

    @property
    def corrects(self) -> bool:
        return self.anchor != "none"

    @property
    def holds_to_anchor(self) -> bool:
        """Whether the job pulls toward an anchor file: ``first`` and ``blend``."""
        return self.anchor in ("first", "blend")

    @property
    def pull(self) -> float:
        """The share of the gap to the anchor each run closes: all of it for ``first``, ``first_weight`` for ``blend``."""
        return 1.0 if self.anchor == "first" else self.first_weight if self.anchor == "blend" else 0.0

    def as_dict(self) -> dict[str, Any]:
        """The policy as ``GET /jobs/{job}`` shows it: only the keys that apply to its anchor."""
        data: dict[str, Any] = {"anchor": self.anchor}
        if self.corrects:
            data.update(strength=self.strength, regions=self.regions)
        if self.anchor == "blend":
            data["first_weight"] = self.first_weight
        if self.holds_to_anchor:
            data["reanchor"] = self.reanchor
        return data


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
    # The scale-1 plan of a job with an input and no desired_input_*: run 1 still reads an 8-bit sRGB copy. Kept apart
    # from input_resize, which sets the job's size and is what the manifest records.
    copy_plan: ResizePlan | None = None
    # (source, key, value) for each width or height that the desired size replaces.
    ignored_size: tuple[tuple[str, str, Any], ...] = ()
    # The color correction (Milestone 09); ``none`` unless the job has a ``color`` block.
    color: ColorPolicy = ColorPolicy()
    # The wait between runs, and where it came from: job, global_config, or default.
    cooldown: CooldownPolicy = DEFAULT_COOLDOWN
    cooldown_source: str = "default"
    # The job file's text as it was loaded, so a record shows exactly what ran. Job files reject unknown keys, so it cannot hold a credential.
    # Left out of equality and repr: comments must not make two jobs differ, and a repr must not dump the file.
    source_text: str = field(default="", repr=False, compare=False)

    @property
    def input_copy(self) -> ResizePlan | None:
        """How run 1's copy of the input is made; every job with an input gets one (owner decision, Milestone 09)."""
        if self.input is None:
            return None
        return self.input_resize or self.copy_plan

    def schedule(self) -> tuple[PromptPair, ...]:
        """Return the prompt pair used by each run, in run order."""
        assigned = {number: pair for pair in self.prompt_pairs for number in pair.runs}
        default = next((pair for pair in self.prompt_pairs if pair.default), None)
        return tuple(assigned.get(number, default) for number in range(1, self.run_count + 1))  # type: ignore[misc]

    def correction_seconds(self) -> float:
        """The longest one run's color correction may take; 0 for a job that does not correct."""
        if not (self.mode.is_video and self.color.corrects):
            return 0.0
        frames = self.config_override.frame_count if self.config_override.frame_count is not None else self.base_config.get("numFrames")
        return correction_limit(frames if is_int(frames) and frames > 0 else ASSUMED_FRAME_COUNT)

    def drift_seconds(self) -> float:
        """The longest one run's ``color_drift`` check may take when it runs on its own; 0 for an image job."""
        return DRIFT_SECONDS if self.mode.is_video else 0.0

    def configured_seed(self) -> tuple[int | None, str]:
        """Return the seed from the job or base configuration and where it came from."""
        if self.config_override.seed is not None:
            return self.config_override.seed, "config_override"
        seed = self.base_config.get("seed")
        if is_int(seed) and seed >= 0:
            return seed, "config_file"
        return None, "random"
