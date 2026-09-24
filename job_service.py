"""Run every generation a job describes, as one chain of runs."""

from __future__ import annotations

import json
import random
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from configuration import load_config
from draw_things_arguments import DrawThingsGenerateArguments
from draw_things_runner import install_signal_handlers, restore_signal_handlers
from generation_config import build_config_json
from generation_service import GenerationService, Runner
from job_definition import JobDefinition, PromptPair, report_ignored_config
from job_log import add_job_log, remove_job_log
from job_manifest import JobManifest, RunRecord, write_manifest
from output_naming import Clock, RandomNumber, job_file_stem, last_frame_path, next_output_path, random_four_digits

if TYPE_CHECKING:
    from input_resize import TemporaryInput


class StoppableRunner(Runner, Protocol):
    """A runner the job can ask to stop when it receives a signal."""

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None: ...


RunnerFactory = Callable[[DrawThingsGenerateArguments, float | None, float], StoppableRunner]
FrameExtractor = Callable[[Path, Path], None]


@dataclass(frozen=True)
class PlannedRun:
    """One run of a job, with its predicted input and output files."""

    number: int
    batch: int
    pair: PromptPair
    input: Path | None
    output: Path
    last_frame: Path | None
    arguments: DrawThingsGenerateArguments


@dataclass(frozen=True)
class JobPreview:
    """What a dry run would do: the seed and every run in order."""

    seed: int
    seed_source: str
    runs: tuple[PlannedRun, ...]
    command_previews: tuple[str, ...]


@dataclass(frozen=True)
class JobOutcome:
    """The result of running a job."""

    exit_code: int
    completed_runs: int
    total_runs: int
    manifest: Path | None
    log: Path | None


class JobService:
    """Expand a job into runs, execute them in order, and chain their outputs."""

    def __init__(
        self,
        runner_factory: RunnerFactory,
        find_executable: Callable[[str], str | None],
        frame_extractor: FrameExtractor,
        require_ffmpeg: Callable[[], object],
        *,
        clock: Clock = datetime.now,
        random_number: RandomNumber = random_four_digits,
        random_seed: Callable[[], int] = lambda: random.randint(0, 2**32 - 1),
        handle_signals: bool = True,
    ) -> None:
        self._runner_factory = runner_factory
        self._find_executable = find_executable
        self._frame_extractor = frame_extractor
        self._require_ffmpeg = require_ffmpeg
        self._clock = clock
        self._random_number = random_number
        self._random_seed = random_seed
        self._handle_signals = handle_signals
        self._generation = GenerationService(runner_factory=self._create_runner, find_executable=find_executable, config_loader=load_config)
        self._current_runner: StoppableRunner | None = None
        self._interrupt: signal.Signals | None = None

    def preview(self, job: JobDefinition, *, executable: str) -> JobPreview:
        """Validate that the job can start and describe every run without running anything."""
        self._check_tools(job, executable)
        seed, source = self._seed(job)
        runs: list[PlannedRun] = []
        reserved: set[Path] = set()
        current_input = job.input
        plan = job.input_copy
        if job.input is not None and plan is not None:
            width, height = plan.target_size
            current_input = Path(f"<{job.input.name} resized to {width}x{height}>")
        for number, pair in enumerate(job.schedule(), start=1):
            run = self._plan_run(job, number, pair, current_input, seed, executable, reserved)
            reserved.update(path for path in (run.output, run.last_frame) if path is not None)
            runs.append(run)
            current_input = run.last_frame or run.output
        previews = tuple(self._generation.execute(run.arguments, dry_run=True, timeout=job.run_timeout_seconds, shutdown_grace=0).command_preview or "" for run in runs)
        return JobPreview(seed=seed, seed_source=source, runs=tuple(runs), command_previews=previews)

    def run(self, job: JobDefinition, *, executable: str, shutdown_grace: float, write_records: bool = False) -> JobOutcome:
        """Run every batch in order; stop at the first failed, timed-out, or interrupted run.

        With ``write_records``, a JSON manifest and a log file are saved beside the outputs.
        """
        if shutdown_grace < 0:
            raise ValueError("--shutdown-grace must not be negative")
        self._check_tools(job, executable)
        seed, seed_source = self._seed(job)
        # Resize before the output directory, manifest, or log exist, so a bad image leaves nothing behind.
        temporary_input: TemporaryInput | None = None
        plan = job.input_copy
        if job.input is not None and plan is not None:
            # Imported here, so jobs that never resize do not load numpy and LittleCMS.
            from input_resize import TemporaryInput

            temporary_input = TemporaryInput(job.input, plan)
        try:
            return self._run_with_records(job, executable=executable, shutdown_grace=shutdown_grace, write_records=write_records, seed=seed, seed_source=seed_source, temporary_input=temporary_input)
        finally:
            if temporary_input is not None:
                temporary_input.cleanup()

    def _run_with_records(self, job: JobDefinition, *, executable: str, shutdown_grace: float, write_records: bool, seed: int, seed_source: str, temporary_input: TemporaryInput | None) -> JobOutcome:
        job.output_directory.mkdir(parents=True, exist_ok=True)
        manifest_path: Path | None = None
        log_path: Path | None = None
        log_sink: int | None = None
        manifest: JobManifest | None = None
        self._interrupt = None
        previous_handlers = None
        try:
            if write_records:
                stem = job_file_stem(job.output_directory, job.name, self._clock, self._random_number)
                manifest_path = job.output_directory / f"{stem}.json"
                log_path = job.output_directory / f"{stem}.log"
                log_sink = add_job_log(log_path)
            if self._handle_signals:
                previous_handlers = install_signal_handlers(self._handle_signal)
            manifest = JobManifest(
                job_file=str(job.path),
                name=job.name,
                mode=str(job.mode),
                config_file=job.config_file,
                config_override=job.config_override.as_dict(),
                seed=seed,
                seed_source=seed_source,
                started_at=self._timestamp(),
                log_file=log_path.name if log_path is not None else None,
                input_resize=job.input_resize.as_manifest() if job.input_resize is not None else None,
            )
            return self._run_chain(job, manifest, manifest_path, log_path, executable=executable, shutdown_grace=shutdown_grace, temporary_input=temporary_input)
        except BaseException:
            if manifest is not None and manifest.status == "running":
                manifest.status = "failed"
                manifest.finished_at = self._timestamp()
                self._save(manifest_path, manifest)
            raise
        finally:
            restore_signal_handlers(previous_handlers)
            if log_sink is not None:
                remove_job_log(log_sink)

    def _run_chain(self, job: JobDefinition, manifest: JobManifest, manifest_path: Path | None, log_path: Path | None, *, executable: str, shutdown_grace: float, temporary_input: TemporaryInput | None) -> JobOutcome:
        schedule = job.schedule()
        total = len(schedule)
        records = f"; manifest {manifest_path}; log {log_path}" if manifest_path is not None else ""
        logger.info("Job {} ({}): {} runs, seed {} (from {}){}", job.name, job.mode, total, manifest.seed, manifest.seed_source, records)
        report_ignored_config(job)
        self._save(manifest_path, manifest)
        current_input = job.input
        if temporary_input is not None:
            logger.info("Run 1 input: temporary copy {} (removed after run 1)", temporary_input.path)
            current_input = temporary_input.path
        completed = 0
        exit_code = 0
        for number, pair in enumerate(schedule, start=1):
            if self._interrupt is not None:
                logger.warning("Job stopped by {} before run {}/{}", self._interrupt.name, number, total)
                manifest.status = "interrupted"
                exit_code = 128 + self._interrupt.value
                break
            run = self._plan_run(job, number, pair, current_input, manifest.seed, executable, set())
            record = RunRecord(
                batch=run.batch,
                pair=pair.name,
                positive=pair.positive,
                negative=pair.negative,
                # Run 1's temporary copy is gone after the run, so record the job's own input.
                input=str(job.input if number == 1 else run.input) if run.input is not None else None,
                output=run.output.name,
                last_frame=None,
                command=GenerationService.redact_command(run.arguments.command),
                started_at=self._timestamp(),
                resized_input=str(temporary_input.path) if number == 1 and temporary_input is not None else None,
            )
            manifest.runs.append(record)
            self._save(manifest_path, manifest)
            logger.info("Run {}/{} (batch {}, pair {}): input={}, output={}", number, total, run.batch, pair.name, run.input or "(none, text only)", run.output)
            try:
                status, exit_code = self._execute_run(job, run, record, shutdown_grace)
            except BaseException:
                record.status = "failed"
                raise
            if number == 1 and temporary_input is not None:
                temporary_input.cleanup()
            record.status = status
            record.exit_code = exit_code
            if status != "succeeded":
                partial = f"; partial output kept: {run.output}" if run.output.exists() else ""
                logger.error("Run {}/{} {} with exit code {}{}", number, total, status.replace("_", " "), exit_code, partial)
                if not run.output.exists():
                    record.output = None
                manifest.status = "interrupted" if status == "interrupted" else "failed"
                self._save(manifest_path, manifest)
                break
            completed += 1
            self._save(manifest_path, manifest)
            current_input = run.last_frame or run.output
        else:
            manifest.status = "succeeded"
        manifest.finished_at = self._timestamp()
        self._save(manifest_path, manifest)
        logger.info("Job {} {}: {}/{} runs completed{}", job.name, manifest.status, completed, total, records)
        return JobOutcome(exit_code=exit_code, completed_runs=completed, total_runs=total, manifest=manifest_path, log=log_path)

    @staticmethod
    def _save(manifest_path: Path | None, manifest: JobManifest) -> None:
        if manifest_path is not None:
            write_manifest(manifest_path, manifest)

    def _execute_run(self, job: JobDefinition, run: PlannedRun, record: RunRecord, shutdown_grace: float) -> tuple[str, int]:
        started = time.monotonic()
        try:
            outcome = self._generation.execute(run.arguments, dry_run=False, timeout=job.run_timeout_seconds, shutdown_grace=shutdown_grace)
        finally:
            self._current_runner = None
            record.seconds = round(time.monotonic() - started, 1)
        if outcome.timed_out:
            return "timed_out", outcome.exit_code
        if outcome.termination_signal is not None:
            return "interrupted", outcome.exit_code
        if outcome.exit_code != 0:
            return "failed", outcome.exit_code
        if not run.output.is_file():
            logger.error("draw-things-cli exited with 0 but did not write {}", run.output)
            return "failed", 1
        if run.last_frame is not None:
            try:
                self._frame_extractor(run.output, run.last_frame)
            except ValueError as error:
                if self._interrupt is not None:
                    return "interrupted", 128 + self._interrupt.value
                logger.error("{}", error)
                return "failed", 1
            record.last_frame = run.last_frame.name
        return "succeeded", 0

    def _plan_run(self, job: JobDefinition, number: int, pair: PromptPair, run_input: Path | None, seed: int, executable: str, reserved: set[Path]) -> PlannedRun:
        output = next_output_path(job.output_directory, job.name, job.extension, self._clock, self._random_number, reserved)
        last_frame = last_frame_path(output) if job.mode.is_video else None
        override = job.config_override
        config = build_config_json(job.base_config, override.as_dict())
        config["model"] = job.model
        width, height = override.width, override.height
        if job.size is not None:
            width, height = job.size
            config["width"], config["height"] = job.size
        arguments = DrawThingsGenerateArguments(
            model=job.model,
            executable=executable,
            prompt=pair.positive,
            negative_prompt=pair.negative,
            steps=override.steps,
            cfg=override.guidance_scale,
            width=width,
            height=height,
            frames=override.frame_count,
            strength=override.strength,
            seed=seed,
            config_json=json.dumps(config, separators=(",", ":")),
            image=run_input,
            output=output,
        )
        return PlannedRun(number=number, batch=number, pair=pair, input=run_input, output=output, last_frame=last_frame, arguments=arguments)

    def _check_tools(self, job: JobDefinition, executable: str) -> None:
        if self._find_executable(executable) is None:
            raise ValueError(f"Could not find '{executable}' on PATH. Install Draw Things CLI or pass --executable with its path.")
        if job.mode.is_video:
            self._require_ffmpeg()

    def _seed(self, job: JobDefinition) -> tuple[int, str]:
        seed, source = job.configured_seed()
        return (seed, source) if seed is not None else (self._random_seed(), "random")

    def _create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float) -> StoppableRunner:
        runner = self._runner_factory(arguments, timeout, shutdown_grace)
        self._current_runner = runner
        if self._interrupt is not None:
            runner.request_shutdown(self._interrupt)
        return runner

    def _timestamp(self) -> str:
        return self._clock().astimezone().isoformat(timespec="seconds")

    def _handle_signal(self, received_signal: signal.Signals) -> None:
        self._interrupt = received_signal
        if self._current_runner is not None:
            self._current_runner.request_shutdown(received_signal)
