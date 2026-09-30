"""Plan a job: its seed, and every run's arguments and file names, without running anything."""

from __future__ import annotations

import json
import random
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from draw_things_control.core.arguments import DrawThingsGenerateArguments, override_arguments, redact_command
from draw_things_control.core.clock import Clock
from draw_things_control.core.draw_things_config import build_config_json
from draw_things_control.core.generation import require_executable
from draw_things_control.jobs.definition import JobDefinition, PromptPair
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.output_naming import RandomNumber, corrected_output_path, last_frame_path, next_output_path, random_four_digits, raw_last_frame_path


@dataclass(frozen=True)
class PlannedRun:
    """One run of a job, with its predicted input and output files."""

    number: int
    pair: PromptPair
    input: Path | None
    output: Path
    last_frame: Path | None
    arguments: DrawThingsGenerateArguments
    # A correcting job's uncorrected last frame and corrected copy (Milestone 09); None for any other.
    raw_last_frame: Path | None = None
    corrected_output: Path | None = None


@dataclass(frozen=True)
class JobPreview:
    """What a dry run would do: the seed and every run in order."""

    seed: int
    seed_source: str
    runs: tuple[PlannedRun, ...]
    # Each run's command as arguments, credentials redacted, in run order.
    commands: tuple[tuple[str, ...], ...]

    @property
    def command_previews(self) -> tuple[str, ...]:
        """Each run's redacted command as one line a shell can read, as the dry run prints it."""
        return tuple(shlex.join(command) for command in self.commands)


class JobPlanner:
    """Expands a job into runs: names their files and builds their draw-things-cli arguments. Starts nothing."""

    def __init__(
        self,
        find_executable: Callable[[str], str | None],
        media: MediaTools,
        *,
        clock: Clock = datetime.now,
        random_number: RandomNumber = random_four_digits,
        random_seed: Callable[[], int] = lambda: random.randint(0, 2**32 - 1),
    ) -> None:
        self._find_executable = find_executable
        self._media = media
        self._clock = clock
        self._random_number = random_number
        self._random_seed = random_seed

    def check_tools(self, job: JobDefinition, executable: str) -> None:
        """Raise ToolMissingError when the job cannot start for want of draw-things-cli, ffmpeg, or ffprobe."""
        require_executable(self._find_executable, executable)
        if job.mode.is_video:
            self._media.require_ffmpeg()
            # A video job that could not measure its outputs would record no actual size, so it does not start.
            if self._media.require_ffprobe is not None:
                self._media.require_ffprobe()

    def seed(self, job: JobDefinition, placeholder: int | None) -> tuple[int, str]:
        """The job's seed and where it came from; a job with none draws a random one, unless ``placeholder`` is given."""
        seed, source = job.configured_seed()
        if seed is not None:
            return seed, source
        return (placeholder if placeholder is not None else self._random_seed()), "random"

    def preview(self, job: JobDefinition, *, executable: str, seed: int | None = None) -> JobPreview:
        """Validate that the job can start and describe every run without running anything.

        A job with no configured seed draws a random one, unless ``seed`` is given to use in its place.
        """
        self.check_tools(job, executable)
        seed, source = self.seed(job, seed)
        runs: list[PlannedRun] = []
        reserved: set[Path] = set()
        current_input = job.input
        plan = job.input_copy
        if job.input is not None and plan is not None:
            width, height = plan.target_size
            made = "copied as 8-bit sRGB at" if plan.fit in ("none", "rotate") else "resized to"
            current_input = Path(f"<{job.input.name} {made} {width}x{height}>")
        for number, pair in enumerate(job.schedule(), start=1):
            run = self.plan_run(job, number, pair, current_input, seed, executable, reserved)
            reserved.update(path for path in (run.output, run.last_frame, run.raw_last_frame, run.corrected_output) if path is not None)
            runs.append(run)
            current_input = run.last_frame or run.output
        # The job's timeout was checked when it was loaded, so each command only needs its credentials redacted.
        commands = tuple(tuple(redact_command(run.arguments.command)) for run in runs)
        return JobPreview(seed=seed, seed_source=source, runs=tuple(runs), commands=commands)

    def plan_run(self, job: JobDefinition, number: int, pair: PromptPair, run_input: Path | None, seed: int, executable: str, reserved: set[Path]) -> PlannedRun:
        output = next_output_path(job.output_directory, job.name, job.extension, self._clock, self._random_number, reserved)
        last_frame = last_frame_path(output) if job.mode.is_video else None
        override = job.config_override
        config = build_config_json(job.base_config, override.as_dict())
        config["model"] = job.model
        width, height = override.width, override.height
        if job.size is not None:
            width, height = job.size
            config["width"], config["height"] = job.size
        flags = {**override_arguments(override.as_dict()), "model": job.model, "width": width, "height": height, "seed": seed}
        # A job's output is captured, never shown, so the live sampling preview is only extra work.
        arguments = DrawThingsGenerateArguments(executable=executable, prompt=pair.positive, negative_prompt=pair.negative, config_json=json.dumps(config, separators=(",", ":")), image=run_input, output=output, video_format=job.video_format, disable_preview=True, **flags)
        corrects = job.mode.is_video and job.color.corrects
        raw, copy = (raw_last_frame_path(output), corrected_output_path(output)) if corrects else (None, None)
        return PlannedRun(number=number, pair=pair, input=run_input, output=output, last_frame=last_frame, arguments=arguments, raw_last_frame=raw, corrected_output=copy)
