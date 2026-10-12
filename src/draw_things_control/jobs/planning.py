"""Plan a job: its seed, and every run's arguments and file names, without running anything."""

from __future__ import annotations

import random
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from draw_things_control.core.arguments import CommandArguments
from draw_things_control.core.clock import Clock
from draw_things_control.jobs.backend import BackendRegistry
from draw_things_control.jobs.backends import DEFAULT_BACKENDS
from draw_things_control.jobs.definition import JobDefinition, PromptPair
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.output_naming import RandomNumber, next_output_path, random_four_digits


@dataclass(frozen=True)
class PlannedRun:
    """One run of a job, with its predicted input and output files."""

    number: int
    pair: PromptPair
    input: Path | None
    output: Path
    last_frame: Path | None
    arguments: CommandArguments
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
    """Expands a job into runs: names their files and asks its backend for commands. Starts nothing."""

    def __init__(
        self,
        find_executable: Callable[[str], str | None],
        media: MediaTools,
        *,
        clock: Clock = datetime.now,
        random_number: RandomNumber = random_four_digits,
        random_seed: Callable[[], int] = lambda: random.randint(0, 2**32 - 1),
        backends: BackendRegistry = DEFAULT_BACKENDS,
    ) -> None:
        self._find_executable = find_executable
        self._media = media
        self._clock = clock
        self._random_number = random_number
        self._random_seed = random_seed
        self._backends = backends

    def check_tools(self, job: JobDefinition, executable: str) -> None:
        """Raise ToolMissingError when the job cannot start for want of draw-things-cli, ffmpeg, or ffprobe."""
        self._backends.get(job.backend).check_tools(job, executable, self._media, self._find_executable)

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
        backend = self._backends.get(job.backend)
        commands = tuple(backend.redact(run.arguments.command) for run in runs)
        return JobPreview(seed=seed, seed_source=source, runs=tuple(runs), commands=commands)

    def plan_run(self, job: JobDefinition, number: int, pair: PromptPair, run_input: Path | None, seed: int, executable: str, reserved: set[Path]) -> PlannedRun:
        output = next_output_path(job.output_directory, job.name, job.extension, self._clock, self._random_number, reserved)
        backend = self._backends.get(job.backend)
        paths = backend.output_paths(job, output)
        arguments = backend.arguments(job, pair, run_input, output, seed, executable)
        return PlannedRun(number=number, pair=pair, input=run_input, output=output, last_frame=paths.handoff, arguments=arguments, raw_last_frame=paths.raw_handoff, corrected_output=paths.corrected_output)
