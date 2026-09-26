"""Run every generation a job describes, as one chain of runs."""

from __future__ import annotations

import json
import os
import random
import shlex
import signal
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from draw_things_control.core.configuration import load_config
from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.core.draw_things_runner import install_signal_handlers, interruptible_wait, restore_signal_handlers
from draw_things_control.core.generation_config import build_config_json
from draw_things_control.core.generation_service import ChildStartCallback, GenerationService, Runner
from draw_things_control.core.global_config import CooldownWait
from draw_things_control.core.process_output import MessageCallback, ProcessMessage
from draw_things_control.jobs.job_definition import JobDefinition, PromptPair
from draw_things_control.jobs.job_events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobObserver, JobStarted, RunFinished, RunOutput, RunStarted, notify
from draw_things_control.jobs.job_log import add_job_log, remove_job_log
from draw_things_control.jobs.job_manifest import JobManifest, RunRecord, write_manifest
from draw_things_control.jobs.job_report import auto_wait_text, cooldown_summary, report_ignored_config, seconds_text
from draw_things_control.jobs.media_info import MediaInfo
from draw_things_control.jobs.output_naming import Clock, RandomNumber, job_file_stem, last_frame_path, next_output_path, random_four_digits

if TYPE_CHECKING:
    from draw_things_control.jobs.input_resize import TemporaryInput


class StoppableRunner(Runner, Protocol):
    """A runner the job can ask to stop when it receives a signal."""

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None: ...


class RunnerFactory(Protocol):
    """Creates a run's runner; ``on_message`` receives each line the child prints, or is None when nothing observes the job.

    ``on_start``, when given, receives the child's PID and executable name once it has started.
    """

    def __call__(self, arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None) -> StoppableRunner: ...


FrameExtractor = Callable[[Path, Path], None]
# Writes color tags into a finished video's container; returns whether it changed the file.
VideoTagger = Callable[[Path], bool]
# Waits up to the given seconds between runs and returns the seconds actually waited.
Cooldown = Callable[[float], float]
# Measures what an output file actually holds; raises ValueError when it cannot.
OutputMeasurer = Callable[[Path], MediaInfo]


@dataclass(frozen=True)
class PlannedRun:
    """One run of a job, with its predicted input and output files."""

    number: int
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
    # Each run's command as arguments, credentials redacted, in run order.
    commands: tuple[tuple[str, ...], ...]

    @property
    def command_previews(self) -> tuple[str, ...]:
        """Each run's redacted command as one line a shell can read, as the dry run prints it."""
        return tuple(shlex.join(command) for command in self.commands)


@dataclass
class _Execution:
    """One run of ``JobService.run``: what every step of the chain reads, passed as one value instead of a long argument list."""

    job: JobDefinition
    manifest: JobManifest
    # Both None unless records are written beside the outputs.
    manifest_path: Path | None
    log_path: Path | None
    executable: str
    shutdown_grace: float
    # Run 1's resized or upright copy of the input, removed after run 1.
    temporary_input: TemporaryInput | None
    schedule: tuple[PromptPair, ...]

    @property
    def total(self) -> int:
        return len(self.schedule)

    def save(self) -> None:
        """Write the manifest, when records are written."""
        if self.manifest_path is not None:
            write_manifest(self.manifest_path, self.manifest)


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
        cooldown: Cooldown | None = None,
        video_tagger: VideoTagger | None = None,
        require_ffprobe: Callable[[], object] | None = None,
        output_measurer: OutputMeasurer | None = None,
    ) -> None:
        self._runner_factory = runner_factory
        self._find_executable = find_executable
        self._frame_extractor = frame_extractor
        self._require_ffmpeg = require_ffmpeg
        self._clock = clock
        self._random_number = random_number
        self._random_seed = random_seed
        self._handle_signals = handle_signals
        self._cooldown = cooldown or self._wait_for_cooldown
        self._video_tagger = video_tagger
        self._require_ffprobe = require_ffprobe
        self._output_measurer = output_measurer
        self._generation = GenerationService(runner_factory=self._create_runner, find_executable=find_executable, config_loader=load_config)
        self._current_runner: StoppableRunner | None = None
        self._interrupt: signal.Signals | None = None
        # Guards the running flag and the wake-up pipe, so cancel() from another thread never writes to a closed or reused descriptor.
        # The signal handler takes no lock: it only sets a flag and asks the runner to stop.
        self._state_lock = threading.Lock()
        self._running = False
        self._observer: JobObserver | None = None
        self._on_child_start: ChildStartCallback | None = None
        self._wake_read: int | None = None
        self._wake_write: int | None = None

    def preview(self, job: JobDefinition, *, executable: str, seed: int | None = None) -> JobPreview:
        """Validate that the job can start and describe every run without running anything.

        A job with no configured seed draws a random one, unless ``seed`` is given to use in its place.
        """
        self._check_tools(job, executable)
        seed, source = self._seed(job, seed)
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
        # The job's timeout was checked when it was loaded, so each command only needs its credentials redacted.
        commands = tuple(tuple(GenerationService.redact_command(run.arguments.command)) for run in runs)
        return JobPreview(seed=seed, seed_source=source, runs=tuple(runs), commands=commands)

    def run(self, job: JobDefinition, *, executable: str, shutdown_grace: float, write_records: bool = False, observer: JobObserver | None = None, on_child_start: ChildStartCallback | None = None, reserve_execution_id: Callable[[], str] | None = None) -> JobOutcome:
        """Run the job's runs in order; stop at the first failed, timed-out, or interrupted run.

        ``reserve_execution_id`` gives the execution its ID (E0012) from the state store, which this layer cannot reach.
        It is called once, after the checks that can refuse the job and before the output directory, the manifest, or
        the log exist; when it raises, the job does not start and the error propagates. The ID goes into JobStarted, the
        manifest, and the log.

        With ``write_records``, a JSON manifest and a log file are saved beside the outputs. ``observer``
        receives a JobEvent for each step, on this thread; one that raises is logged and ignored.
        ``on_child_start`` receives each run's child PID and executable name, for the run lock. Only one
        job runs at a time on a service: a second concurrent call raises RuntimeError.
        """
        self._begin(observer, on_child_start)
        try:
            return self._run(job, executable=executable, shutdown_grace=shutdown_grace, write_records=write_records, reserve_execution_id=reserve_execution_id)
        finally:
            self._end()

    def cancel(self, received_signal: signal.Signals = signal.SIGTERM) -> bool:
        """Stop the running job as ``received_signal`` would; safe from any thread.

        Returns False, and does nothing, when no job is running. True means the stop was requested, as
        with a signal: it ends the current run and any cooldown, and starts no later run. A cancel that
        lands after the last run has finished changes nothing, so the job still ends as it did.
        """
        with self._state_lock:
            if not self._running:
                return False
            self._interrupt = received_signal
            if self._wake_write is not None:
                try:
                    os.write(self._wake_write, b"\0")
                except OSError:
                    # A full pipe already holds a byte, and one is enough.
                    pass
            runner = self._current_runner
        # The flag is set before the runner is read; _create_runner stores the runner before it reads the flag, so a cancel landing between the two still reaches the runner.
        if runner is not None:
            runner.request_shutdown(received_signal)
        return True

    def _begin(self, observer: JobObserver | None, on_child_start: ChildStartCallback | None) -> None:
        with self._state_lock:
            if self._running:
                raise RuntimeError("This JobService is already running a job")
            read_end, write_end = os.pipe()
            os.set_blocking(read_end, False)
            os.set_blocking(write_end, False)
            self._wake_read, self._wake_write = read_end, write_end
            self._running = True
            self._interrupt = None
            self._observer = observer
            self._on_child_start = on_child_start

    def _end(self) -> None:
        with self._state_lock:
            self._running = False
            self._observer = None
            self._on_child_start = None
            for descriptor in (self._wake_read, self._wake_write):
                if descriptor is not None:
                    os.close(descriptor)
            self._wake_read = self._wake_write = None

    def _emit(self, event: JobEvent) -> None:
        if self._observer is not None:
            notify(self._observer, event)

    def _run(self, job: JobDefinition, *, executable: str, shutdown_grace: float, write_records: bool, reserve_execution_id: Callable[[], str] | None) -> JobOutcome:
        if shutdown_grace < 0:
            raise ValueError("--shutdown-grace must not be negative")
        self._check_tools(job, executable)
        seed, seed_source = self._seed(job, None)
        # Resize before the output directory, manifest, or log exist, so a bad image leaves nothing behind.
        temporary_input: TemporaryInput | None = None
        plan = job.input_copy
        if job.input is not None and plan is not None:
            # Imported here, so jobs that never resize do not load numpy and LittleCMS.
            from draw_things_control.jobs.input_resize import TemporaryInput

            temporary_input = TemporaryInput(job.input, plan)
        try:
            # Taken last of the checks, so a job refused above never uses up a number; a job that cannot get one does not start.
            execution_id = reserve_execution_id() if reserve_execution_id is not None else None
            return self._run_with_records(job, executable=executable, shutdown_grace=shutdown_grace, write_records=write_records, seed=seed, seed_source=seed_source, temporary_input=temporary_input, execution_id=execution_id)
        finally:
            if temporary_input is not None:
                temporary_input.cleanup()

    def _run_with_records(self, job: JobDefinition, *, executable: str, shutdown_grace: float, write_records: bool, seed: int, seed_source: str, temporary_input: TemporaryInput | None, execution_id: str | None) -> JobOutcome:
        job.output_directory.mkdir(parents=True, exist_ok=True)
        manifest_path: Path | None = None
        log_path: Path | None = None
        log_sink: int | None = None
        manifest: JobManifest | None = None
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
                cooldown_seconds=job.cooldown.fixed_seconds,
                cooldown_source=job.cooldown_source,
                cooldown=job.cooldown.as_dict(),
                started_at=self._timestamp(),
                log_file=log_path.name if log_path is not None else None,
                execution_id=execution_id,
                input_resize=job.input_resize.as_manifest() if job.input_resize is not None else None,
            )
            execution = _Execution(job, manifest, manifest_path, log_path, executable, shutdown_grace, temporary_input, job.schedule())
            return self._run_chain(execution)
        except BaseException:
            if manifest is not None and manifest.status == "running":
                manifest.status = "failed"
                manifest.finished_at = self._timestamp()
                if manifest_path is not None:
                    write_manifest(manifest_path, manifest)
            raise
        finally:
            restore_signal_handlers(previous_handlers)
            if log_sink is not None:
                remove_job_log(log_sink)

    def _run_chain(self, execution: _Execution) -> JobOutcome:
        job, manifest = execution.job, execution.manifest
        self._emit(
            JobStarted(
                at=manifest.started_at,
                job_name=job.name,
                job_file=manifest.job_file,
                source_text=job.source_text,
                mode=manifest.mode,
                total_runs=execution.total,
                output_directory=str(job.output_directory),
                input=str(job.input) if job.input is not None else None,
                model=job.model,
                seed=manifest.seed,
                seed_source=manifest.seed_source,
                cooldown=job.cooldown,
                cooldown_source=job.cooldown_source,
                manifest=str(execution.manifest_path) if execution.manifest_path is not None else None,
                log=str(execution.log_path) if execution.log_path is not None else None,
                config_file=manifest.config_file,
                config_override=manifest.config_override,
                input_resize=manifest.input_resize,
                execution_id=manifest.execution_id,
            )
        )
        try:
            return self._run_runs(execution)
        except BaseException:
            # Every started job ends with JobFinished, so a front end never waits on one that raised.
            completed = sum(1 for record in manifest.runs if record.status == "succeeded")
            self._emit(JobFinished(at=self._timestamp(), status="failed", exit_code=None, completed_runs=completed, total_runs=execution.total, signal=None))
            raise

    def _run_runs(self, execution: _Execution) -> JobOutcome:
        job, manifest, total = execution.job, execution.manifest, execution.total
        records = f"; manifest {execution.manifest_path}; log {execution.log_path}" if execution.manifest_path is not None else ""
        execution_id = f", execution {manifest.execution_id}" if manifest.execution_id is not None else ""
        logger.info("Job {} ({}{}): {} runs, seed {} (from {}), {}{}", job.name, job.mode, execution_id, total, manifest.seed, manifest.seed_source, cooldown_summary(job, "from "), records)
        report_ignored_config(job)
        execution.save()
        current_input = job.input
        if execution.temporary_input is not None:
            logger.info("Run 1 input: temporary copy {} (removed after run 1)", execution.temporary_input.path)
            current_input = execution.temporary_input.path
        completed = 0
        exit_code = 0
        for number, pair in enumerate(execution.schedule, start=1):
            if self._interrupt is not None:
                exit_code = self._stop(manifest, self._interrupt, f"before run {number}/{total}")
                break
            run = self._plan_run(job, number, pair, current_input, manifest.seed, execution.executable, set())
            record = self._start_run(execution, run)
            try:
                status, exit_code = self._execute_run(job, run, record, execution.shutdown_grace, number)
            except BaseException:
                record.status = "failed"
                # The run raised, so a file counts as kept only if it exists; the record itself is left as the manifest has it.
                self._finish_run(number, record, None, output=record.output if run.output.exists() else None)
                raise
            record.status = status
            record.exit_code = exit_code
            if status != "succeeded" and not run.output.exists():
                record.output = None
            self._finish_run(number, record, exit_code, output=record.output)
            if number == 1 and execution.temporary_input is not None:
                execution.temporary_input.cleanup()
            if status != "succeeded":
                partial = f"; partial output kept: {run.output}" if run.output.exists() else ""
                logger.error("Run {}/{} {} with exit code {}{}", number, total, status.replace("_", " "), exit_code, partial)
                manifest.status = "interrupted" if status == "interrupted" else "failed"
                execution.save()
                break
            completed += 1
            execution.save()
            current_input = run.last_frame or run.output
            wait = job.cooldown.wait_after(record.seconds or 0.0) if number < total and self._interrupt is None else None
            stop = self._cool_down(execution, record, number + 1, wait) if wait is not None and wait.seconds > 0 else None
            if stop is not None:
                exit_code = self._stop(manifest, *stop)
                break
        else:
            manifest.status = "succeeded"
        manifest.finished_at = self._timestamp()
        execution.save()
        logger.info("Job {} {}: {}/{} runs completed{}", job.name, manifest.status, completed, total, records)
        stopped_by = self._exit_signal(exit_code) if manifest.status == "interrupted" else None
        self._emit(JobFinished(at=manifest.finished_at, status=manifest.status, exit_code=exit_code, completed_runs=completed, total_runs=total, signal=stopped_by))
        return JobOutcome(exit_code=exit_code, completed_runs=completed, total_runs=total, manifest=execution.manifest_path, log=execution.log_path)

    def _start_run(self, execution: _Execution, run: PlannedRun) -> RunRecord:
        """Record ``run`` in the manifest, log it, and announce it; return its record."""
        pair, number, temporary_input = run.pair, run.number, execution.temporary_input
        record = RunRecord(
            pair=pair.name,
            positive=pair.positive,
            negative=pair.negative,
            # Run 1's temporary copy is gone after the run, so record the job's own input.
            input=str(execution.job.input if number == 1 else run.input) if run.input is not None else None,
            output=run.output.name,
            last_frame=None,
            command=GenerationService.redact_command(run.arguments.command),
            started_at=self._timestamp(),
            resized_input=str(temporary_input.path) if number == 1 and temporary_input is not None else None,
        )
        execution.manifest.runs.append(record)
        execution.save()
        logger.info("Run {}/{} (pair {}): input={}, output={}", number, execution.total, pair.name, run.input or "(none, text only)", run.output)
        self._emit(
            RunStarted(
                at=record.started_at,
                number=number,
                total=execution.total,
                pair=pair.name,
                positive=pair.positive,
                negative=pair.negative,
                input=record.input,
                resized_input=record.resized_input,
                output=run.output.name,
                last_frame=run.last_frame.name if run.last_frame is not None else None,
                command=tuple(record.command),
            )
        )
        return record

    def _cool_down(self, execution: _Execution, record: RunRecord, next_run: int, wait: CooldownWait) -> tuple[signal.Signals, str] | None:
        """Wait ``wait`` after ``record``'s run; return the signal that cut it short and where, or None."""
        policy, seconds, total, run_seconds = execution.job.cooldown, wait.seconds, execution.total, record.seconds or 0.0
        until = (self._clock().astimezone() + timedelta(seconds=seconds)).strftime("%H:%M:%S")
        if policy.mode == "auto":
            logger.info("Cooldown: waiting {} before run {}/{} (until {})", auto_wait_text(seconds, policy.ratio, next_run - 1, run_seconds, wait.bound, commas=True), next_run, total, until)
        else:
            logger.info("Cooldown: waiting {} before run {}/{} (until {})", seconds_text(seconds), next_run, total, until)
        ratio = policy.ratio if policy.mode == "auto" else None
        self._emit(CooldownStarted(at=self._timestamp(), after_run=next_run - 1, seconds=seconds, until=until, mode=policy.mode, ratio=ratio, run_seconds=run_seconds, bound=wait.bound))
        # Saved at 0 first, so the manifest shows the job is cooling down rather than stuck.
        record.cooldown_after_seconds = 0.0
        execution.save()
        waited = self._cooldown(seconds)
        # Only a wait that ended early was cut short; a signal after a full wait stops the job before the next run.
        stopped = self._interrupt if waited < seconds else None
        record.cooldown_after_seconds = round(waited, 1)
        execution.save()
        self._emit(CooldownEnded(at=self._timestamp(), waited_seconds=record.cooldown_after_seconds, cut_short=stopped is not None))
        if stopped is not None:
            return stopped, f"during the cooldown before run {next_run}/{total} (waited {seconds_text(round(waited, 1))} of {seconds_text(seconds)})"
        logger.info("Cooldown finished; starting run {}/{}", next_run, total)
        return None

    def _wait_for_cooldown(self, seconds: float) -> float:
        """The default cooldown: a wait that the job's signal handler ends at once."""
        return interruptible_wait(seconds, lambda: self._interrupt is not None, wake_on_signal=self._handle_signals, wake_fd=self._wake_read)

    @staticmethod
    def _stop(manifest: JobManifest, received_signal: signal.Signals, where: str) -> int:
        """Mark the job interrupted by ``received_signal``; return its exit code."""
        logger.warning("Job stopped by {} {}", received_signal.name, where)
        manifest.status = "interrupted"
        return 128 + received_signal.value

    def _finish_run(self, number: int, record: RunRecord, exit_code: int | None, *, output: str | None) -> None:
        self._emit(RunFinished(at=self._timestamp(), number=number, status=record.status, exit_code=exit_code, seconds=record.seconds, output=output, last_frame=record.last_frame, output_width=record.output_width, output_height=record.output_height, output_frames=record.output_frames))

    @staticmethod
    def _exit_signal(exit_code: int) -> str | None:
        """The name of the signal a 128+N exit code stands for, or None."""
        try:
            return signal.Signals(exit_code - 128).name if exit_code > 128 else None
        except ValueError:
            return None

    def _output_callback(self, number: int) -> MessageCallback | None:
        """A callback that turns each child line of run ``number`` into a RunOutput, or None without an observer."""
        if self._observer is None:
            return None

        def on_message(message: ProcessMessage) -> None:
            self._emit(RunOutput(at=self._timestamp(), number=number, stream=message.stream.value, text=message.text, progress=message.progress, percent=message.percent))

        return on_message

    def _execute_run(self, job: JobDefinition, run: PlannedRun, record: RunRecord, shutdown_grace: float, number: int) -> tuple[str, int]:
        started = time.monotonic()
        try:
            outcome = self._generation.execute(run.arguments, dry_run=False, timeout=job.run_timeout_seconds, shutdown_grace=shutdown_grace, on_message=self._output_callback(number), on_start=self._on_child_start)
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
        if self._video_tagger is not None and job.mode.is_video:
            self._tag_video(run.output)
        if run.last_frame is not None:
            try:
                self._frame_extractor(run.output, run.last_frame)
            except ValueError as error:
                if self._interrupt is not None:
                    return "interrupted", 128 + self._interrupt.value
                logger.error("{}", error)
                return "failed", 1
            record.last_frame = run.last_frame.name
        self._measure(run.output, record)
        return "succeeded", 0

    def _measure(self, output: Path, record: RunRecord) -> None:
        """Record the output's actual size and frame count; a file that cannot be measured is a warning, never a failed run."""
        if self._output_measurer is None:
            return
        try:
            info = self._output_measurer(output)
        except (ValueError, OSError) as error:
            logger.warning("Could not measure {}; its size and frame count are not recorded: {}", output.name, error)
            return
        record.output_width, record.output_height, record.output_frames = info.width, info.height, info.frames

    def _tag_video(self, video: Path) -> None:
        """Label the video's colors; a video that cannot be tagged is kept as Draw Things wrote it, and the run still succeeds."""
        assert self._video_tagger is not None
        try:
            self._video_tagger(video)
        except (ValueError, OSError, struct.error) as error:
            logger.warning("Could not write color tags into {}; it keeps the tags Draw Things wrote: {}", video.name, error)

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
        return PlannedRun(number=number, pair=pair, input=run_input, output=output, last_frame=last_frame, arguments=arguments)

    def _check_tools(self, job: JobDefinition, executable: str) -> None:
        if self._find_executable(executable) is None:
            raise ValueError(f"Could not find '{executable}' on PATH. Install Draw Things CLI or pass --executable with its path.")
        if job.mode.is_video:
            self._require_ffmpeg()
            # A video job that could not measure its outputs would record no actual size, so it does not start.
            if self._require_ffprobe is not None:
                self._require_ffprobe()

    def _seed(self, job: JobDefinition, placeholder: int | None) -> tuple[int, str]:
        seed, source = job.configured_seed()
        if seed is not None:
            return seed, source
        return (placeholder if placeholder is not None else self._random_seed()), "random"

    def _create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None) -> StoppableRunner:
        runner = self._runner_factory(arguments, timeout, shutdown_grace, on_message, on_start)
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
