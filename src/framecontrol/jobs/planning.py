"""Plan a job: its seed, and every run's arguments and file names, without running anything."""

from __future__ import annotations

import random
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from framecontrol.core.arguments import CommandArguments
from framecontrol.core.clock import Clock
from framecontrol.jobs.backend import H3_BACKEND, BackendRegistry, OutputPaths
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.jobs.definition import JobDefinition, PromptPair
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.output_naming import RandomNumber, next_output_path, random_four_digits


@dataclass(frozen=True)
class PlannedRun:
    """One run of a job, with its predicted input and output files."""

    number: int
    pair: PromptPair
    input: Path | None
    output: Path
    last_frame: Path | None
    arguments: CommandArguments
    # A correcting job's uncorrected last frame and corrected copy; None for any other.
    raw_last_frame: Path | None = None
    corrected_output: Path | None = None
    # The seed this run's command uses: its own ``seed`` knob in a version-2 job, else the execution seed.
    seed: int | None = None


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

    def check_tools(self, job: JobDefinition, executable: str, *, first_run: int = 1) -> None:
        """Raise ToolMissingError when the job cannot start for want of draw-things-cli, ffmpeg, or ffprobe.

        A version-2 job checks each backend that a run from ``first_run`` on selects, once per distinct run view; a
        backend that only finished runs used is not required.
        """
        if not job.effective_runs:
            self._backends.get(job.backend).check_tools(job, executable, self._media, self._find_executable)
            return
        checked: set[tuple[str, str, str]] = set()
        for run in job.effective_runs[first_run - 1 :]:
            # Each adapter checks what its pipeline needs (a checkpoint, the media tools), so a profile shared by two pipelines is checked for each.
            key = (run.backend, run.settings_profile, run.pipeline)
            if key not in checked:
                self._backends.get(run.backend).check_tools(run.backend_job, executable, self._media, self._find_executable)
                checked.add(key)
            # What the installation probe learned is the execution's, as in version 1: a resume compares its fingerprint.
            installation = run.backend_job.backend_settings.get("h3_installation")
            if installation is not None:
                job.backend_settings["h3_installation"] = dict(installation)

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
            run_input = current_input if job.run_needs_visual_input(number) or not job.effective_runs else None
            run = self.plan_run(job, number, pair, run_input, seed, executable, reserved)
            reserved.update(path for path in (run.output, run.last_frame, run.raw_last_frame, run.corrected_output) if path is not None)
            runs.append(run)
            current_input = run.last_frame or run.output
        # The job's timeout was checked when it was loaded, so each command only needs its credentials redacted.
        commands = tuple(self._backends.get(job.run_backend(run.number)).redact(run.arguments.command) for run in runs)
        return JobPreview(seed=seed, seed_source=source, runs=tuple(runs), commands=commands)

    def plan_run(self, job: JobDefinition, number: int, pair: PromptPair, run_input: Path | None, seed: int, executable: str, reserved: set[Path]) -> PlannedRun:
        """Plan run ``number``. ``seed`` is the execution seed; a version-2 run with its own ``seed`` knob uses that."""
        backend_job = job.job_for_run(number)
        backend_id = job.run_backend(number)
        effective = job.effective_run(number)
        run_seed = effective.seed if effective is not None and effective.seed is not None else seed
        output = next_output_path(job.output_directory, job.name, backend_job.extension, self._clock, self._random_number, reserved)
        backend = self._backends.get(backend_id)
        paths = backend.output_paths(backend_job, output)
        if backend_id == H3_BACKEND and not job.run_needs_visual_input(number + 1):
            paths = OutputPaths()
        # A version-2 view is a one-run job, so its adapter numbers it 1.
        arguments = backend.arguments(backend_job, pair, run_input, output, run_seed, executable, 1 if effective is not None else number)
        recorded_input = run_input
        if effective is not None:
            if not effective.requires_visual_input:
                recorded_input = None
        elif backend_id == H3_BACKEND and not job.run_needs_visual_input(number):
            recorded_input = None
        return PlannedRun(number=number, pair=pair, input=recorded_input, output=output, last_frame=paths.handoff, arguments=arguments, raw_last_frame=paths.raw_handoff, corrected_output=paths.corrected_output, seed=run_seed)
