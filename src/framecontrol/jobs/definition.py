"""The validated form of a job: what a job file means once it is read."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from framecontrol.core.cooldown import DEFAULT_COOLDOWN, CooldownPolicy
from framecontrol.core.numbers import is_int
from framecontrol.jobs.backend import DRAW_THINGS_BACKEND, H3_BACKEND
from framecontrol.jobs.inputs.size import ResizePlan


class GenerationMode(StrEnum):
    """The kind of generation every run of a job performs."""

    I2I = "i2i"
    T2V = "t2v"
    I2V = "i2v"
    # The H3 backend's modes: ``i2av``, and ``h3av`` for an ``r2av`` run.
    I2AV = "i2av"
    H3AV = "h3av"

    @property
    def is_video(self) -> bool:
        return self is not GenerationMode.I2I

    @property
    def requires_input(self) -> bool:
        return self not in (GenerationMode.T2V, GenerationMode.H3AV)

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
class EffectiveRun:
    """One immutable, fully resolved run in a version-2 job.

    ``backend_job`` is the adapter-owned compatibility view used to execute
    this one run.  The remaining fields are the portable compiled plan kept in
    snapshots, records, and public summaries.
    """

    number: int
    backend: str
    pipeline: str
    prompt_pair: str
    settings_profile: str
    pair: PromptPair
    knobs: dict[str, Any]
    knob_sources: dict[str, str]
    settings: dict[str, Any]
    backend_job: JobDefinition = field(repr=False, compare=False)

    @property
    def seed(self) -> int | None:
        value = self.knobs.get("seed")
        return value if is_int(value) else None

    @property
    def timeout_seconds(self) -> float | None:
        value = self.knobs.get("timeout_seconds")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def requires_visual_input(self) -> bool:
        return self.pipeline in ("i2i", "i2v", "i2av")

    @property
    def is_video(self) -> bool:
        return self.pipeline != "i2i"

    def public_plan(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "backend": self.backend,
            "pipeline": self.pipeline,
            "prompt_pair": self.prompt_pair,
            "settings_profile": self.settings_profile,
            "knobs": dict(self.knobs),
            "knob_sources": dict(self.knob_sources),
            "settings": dict(self.settings),
        }


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

# What a correcting job's colors are held to, and when the anchor moves.
COLOR_ANCHORS = ("none", "previous", "first", "blend")
REANCHOR_RULES = ("prompt_pair", "never")
# The correction's time limit per run (as proposed; accepted, owner decision, 2026-10-01): 10 seconds plus 1 per
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
    """A video job's color correction, from its ``color`` block; ``none`` corrects nothing, and every
    video run still measures its drift.

    Each run is corrected back to its own input; ``first`` also pulls it all the way toward the anchor, and ``blend``
    by ``first_weight`` of the gap. The anchor is the first image, or, with ``reanchor: prompt_pair``, the input of the
    last run whose prompt pair differs from the run before it. ``strength`` scales the whole correction toward none.
    """

    anchor: str = "none"
    strength: float = 1.0
    first_weight: float = 0.25
    reanchor: str = "prompt_pair"
    # People, their skin, and the background corrected apart (Apple Vision); false: the whole frame as one.
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
    config_file: str = ""
    base_config: dict[str, Any] = field(default_factory=dict)
    config_override: ConfigOverride = ConfigOverride()
    model: str = ""
    run_timeout_seconds: float | None = None
    # The validated backend's configured seed and its provenance; None means draw a seed when planning.
    requested_seed: int | None = None
    requested_seed_source: str = "random"
    backend: str = DRAW_THINGS_BACKEND
    # Adapter-provided effective summary, safe to serialize; raw validated options live in backend_data.
    backend_settings: dict[str, Any] = field(default_factory=dict)
    backend_data: object | None = field(default=None, repr=False, compare=False)
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
    # The color correction; ``none`` unless the job has a ``color`` block.
    color: ColorPolicy = ColorPolicy()
    # The wait between runs, and where it came from: job, global_config, or default.
    cooldown: CooldownPolicy = DEFAULT_COOLDOWN
    cooldown_source: str = "default"
    # The job file's text as it was loaded, so a record shows exactly what ran. Job files reject unknown keys, so it cannot hold a credential.
    # Left out of equality and repr: comments must not make two jobs differ, and a repr must not dump the file.
    source_text: str = field(default="", repr=False, compare=False)
    # Version 2 compiles every run before queue insertion.  Empty for legacy
    # version-1 snapshots, whose job-wide fields remain authoritative.
    version: int = 1
    effective_runs: tuple[EffectiveRun, ...] = field(default=(), repr=False)

    @property
    def input_copy(self) -> ResizePlan | None:
        """How run 1's copy of the input is made; every job with an input gets one (owner decision)."""
        if self.input is None:
            return None
        return self.input_resize or self.copy_plan

    def schedule(self) -> tuple[PromptPair, ...]:
        """Return the prompt pair used by each run, in run order."""
        if self.effective_runs:
            return tuple(run.pair for run in self.effective_runs)
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
        """Return the backend-validated seed and where it came from."""
        return self.requested_seed, self.requested_seed_source

    def effective_run(self, number: int) -> EffectiveRun | None:
        """Return version 2's compiled run, or ``None`` for a legacy job."""
        return self.effective_runs[number - 1] if self.effective_runs and 1 <= number <= len(self.effective_runs) else None

    def job_for_run(self, number: int) -> JobDefinition:
        """Return the adapter-owned view for one run."""
        run = self.effective_run(number)
        return run.backend_job if run is not None else self

    def effective_plan(self) -> tuple[dict[str, Any], ...]:
        """Return the safe, serializable frozen run plan."""
        return tuple(run.public_plan() for run in self.effective_runs)

    @property
    def mode_label(self) -> str:
        """The job's mode as records and front ends show it: a version-2 job's one pipeline, or ``mixed``."""
        if not self.effective_runs:
            return str(self.mode)
        pipelines = {run.pipeline for run in self.effective_runs}
        return next(iter(pipelines)) if len(pipelines) == 1 else "mixed"

    def run_backend(self, number: int) -> str:
        """The backend ID of the adapter that runs ``number``."""
        run = self.effective_run(number)
        return run.backend if run is not None else self.backend

    def run_needs_visual_input(self, number: int) -> bool:
        """Whether run ``number`` is conditioned on an image: the job's input or the previous run's handoff.

        A version-1 job answers from its mode and, for H3, its run plan; a version-2 job from the run's pipeline. A run
        past the end needs nothing, so ``run_needs_visual_input(number + 1)`` says whether run ``number`` must hand off.
        """
        if number < 1 or number > self.run_count:
            return False
        run = self.effective_run(number)
        if run is not None:
            return run.requires_visual_input
        if self.backend == H3_BACKEND:
            from framecontrol.jobs.backends.h3 import H3JobData

            if isinstance(self.backend_data, H3JobData):
                return self.backend_data.runs[number - 1].pipeline == "i2av"
        return number > 1 or self.mode.requires_input

    def run_checks_input(self, number: int) -> bool:
        """Whether the media checks examine run ``number``'s input: an image-conditioned video run's."""
        run = self.effective_run(number)
        if run is not None:
            return run.pipeline in ("i2v", "i2av")
        if self.mode not in (GenerationMode.I2V, GenerationMode.I2AV, GenerationMode.H3AV):
            return False
        return self.run_needs_visual_input(number)
