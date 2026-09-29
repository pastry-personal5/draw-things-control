"""Run every generation a job describes, as one chain of runs."""

from __future__ import annotations

import random
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from draw_things_control.core.arguments import redact_command
from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.cooldown import CooldownWait
from draw_things_control.core.errors import InputError
from draw_things_control.core.exit_codes import EXIT_PARKED, exit_code_for_signal, signal_for_exit_code
from draw_things_control.core.process.output import MessageCallback, ProcessMessage
from draw_things_control.core.process.runner import ChildStartCallback, RunnerFactory, StoppableRunner
from draw_things_control.core.process.signals import CancelToken, install_signal_handlers, restore_signal_handlers
from draw_things_control.jobs.definition import JobDefinition, PromptPair
from draw_things_control.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobObserver, JobStarted, JobStatus, RunFinished, RunOutput, RunStarted, RunStatus, notify
from draw_things_control.jobs.launcher import RunLauncher
from draw_things_control.jobs.log_writer import JobLogWriter
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.output_naming import RandomNumber, random_four_digits
from draw_things_control.jobs.planning import JobPlanner, JobPreview, PlannedRun
from draw_things_control.jobs.records import JobManifest, JobRecords, RunRecord
from draw_things_control.jobs.run_finisher import RunFinisher
from draw_things_control.jobs.text import report_ignored_config, seconds_text

if TYPE_CHECKING:
    from draw_things_control.jobs.inputs.resize import TemporaryInput

# Waits up to the given seconds between runs and returns the seconds actually waited. It may return early; the executor
# waits again, for the rest, only when a park ended the wait and was withdrawn before it took effect.
Cooldown = Callable[[float], float]


@dataclass(frozen=True)
class ResumePoint:
    """Where a resumed chain starts: the first unfinished run, its input (the last succeeded run's last frame or
    output), and the seed to keep. ``resumes_execution`` names the execution (E0012) this one continues, for the
    manifest and JobStarted; the executor itself does not read it."""

    first_run: int
    input: Path | None
    seed: int
    resumes_execution: str | None = None


@dataclass(frozen=True)
class JobRunOptions:
    """How one run of a job is done, besides the job itself."""

    executable: str
    shutdown_grace: float
    # Save a JSON manifest and a log file beside the outputs.
    write_records: bool = False
    # Receives a JobEvent for each step, on the running thread; one that raises is logged and ignored.
    observer: JobObserver | None = None
    # Receives each run's child PID and executable name, for the run lock.
    on_child_start: ChildStartCallback | None = None
    # Gives the execution its ID (E0012) from the state store, which this layer cannot reach. It is called once, after the
    # checks that can refuse the job and before the output directory, the manifest, or the log exist; when it raises,
    # the job does not start and the error propagates. The ID goes into JobStarted, the manifest, and the log.
    reserve_execution_id: Callable[[], str] | None = None
    # Set to continue an interrupted, failed, or cancelled chain from its last succeeded run instead of from run 1.
    resume: ResumePoint | None = None


@dataclass(frozen=True)
class JobOutcome:
    """The result of running a job."""

    exit_code: int
    completed_runs: int
    total_runs: int
    manifest: Path | None
    log: Path | None


class _ParkedInCooldown:
    """What ``_cool_down`` returns when a park ended the wait and took effect."""


@dataclass
class _Chain:
    """One call of ``JobExecutor.run``: what every step of the chain reads, passed as one value instead of a long argument list."""

    job: JobDefinition
    records: JobRecords
    options: JobRunOptions
    # Run 1's resized or upright copy of the input, removed after run 1.
    temporary_input: TemporaryInput | None
    schedule: tuple[PromptPair, ...]

    @property
    def total(self) -> int:
        return len(self.schedule)


class JobExecutor:
    """Executes a job's runs in order, chaining their outputs; planning, the manifest, the log, and finishing an output are its collaborators'."""

    def __init__(
        self,
        runner_factory: RunnerFactory[StoppableRunner],
        find_executable: Callable[[str], str | None],
        media: MediaTools,
        *,
        clock: Clock = datetime.now,
        random_number: RandomNumber = random_four_digits,
        random_seed: Callable[[], int] = lambda: random.randint(0, 2**32 - 1),
        handle_signals: bool = True,
        cooldown: Cooldown | None = None,
    ) -> None:
        self._clock = clock
        self._random_number = random_number
        self._handle_signals = handle_signals
        self._planner = JobPlanner(find_executable, media, clock=clock, random_number=random_number, random_seed=random_seed)
        self._token = CancelToken(handle_signals=handle_signals)
        self._cooldown = cooldown or self._token.wait
        self._launcher = RunLauncher(runner_factory, find_executable, RunFinisher(media), self._token)
        self._observer: JobObserver | None = None
        self._log_writer = JobLogWriter()
        self._on_child_start: ChildStartCallback | None = None

    def preview(self, job: JobDefinition, *, executable: str, seed: int | None = None) -> JobPreview:
        """Validate that the job can start and describe every run without running anything (see ``JobPlanner.preview``)."""
        return self._planner.preview(job, executable=executable, seed=seed)

    def run(self, job: JobDefinition, options: JobRunOptions) -> JobOutcome:
        """Run the job's runs in order; stop at the first failed, timed-out, or interrupted run.

        Only one job runs at a time on an executor: a second concurrent call raises RuntimeError.
        """
        self._token.begin()
        self._observer, self._on_child_start = options.observer, options.on_child_start
        self._log_writer = JobLogWriter()
        try:
            return self._run(job, options)
        finally:
            self._observer = self._on_child_start = None
            self._token.end()

    def cancel(self, received_signal: signal.Signals = signal.SIGTERM) -> bool:
        """Stop the running job as ``received_signal`` would; safe from any thread.

        Returns False, and does nothing, when no job is running. True means the stop was requested, as
        with a signal: it ends the current run and any cooldown, and starts no later run. A cancel that
        lands after the last run has finished changes nothing, so the job still ends as it did.
        """
        return self._token.cancel(received_signal)

    def park(self) -> bool:
        """Ask the running job to end once its current run finishes; safe from any thread (Milestone 05).

        Returns False, and does nothing, when no job is running. The run in progress finishes, its last frame
        included; after a succeeded run that is not the last, the job ends ``parked``. A cooldown between runs ends
        at once, and the job parks. On the last run the job simply finishes. A failed run fails the job, as always.
        """
        return self._token.park()

    def unpark(self, commit: Callable[[], object] | None = None) -> bool:
        """Withdraw a park; safe from any thread. False only once the park has taken effect, when the job ends parked.
        ``commit`` runs first, with no run boundary able to pass: if it raises, the park stands as it was."""
        return self._token.unpark(commit)

    def _emit(self, event: JobEvent) -> None:
        self._log_writer(event)
        if self._observer is not None:
            notify(self._observer, event)

    def _timestamp(self) -> str:
        return local_timestamp(self._clock())

    def _run(self, job: JobDefinition, options: JobRunOptions) -> JobOutcome:
        if options.shutdown_grace < 0:
            raise InputError("--shutdown-grace must not be negative")
        self._planner.check_tools(job, options.executable)
        if options.resume is not None:
            # The chain keeps its original seed; run 1 is not run again, so it needs no resized copy of the input.
            seed, seed_source, temporary_input = options.resume.seed, "resume", None
        else:
            seed, seed_source = self._planner.seed(job, None)
            # Resize before the output directory, manifest, or log exist, so a bad image leaves nothing behind.
            temporary_input = self._temporary_input(job)
        try:
            # Taken last of the checks, so a job refused above never uses up a number; a job that cannot get one does not start.
            execution_id = options.reserve_execution_id() if options.reserve_execution_id is not None else None
            return self._run_with_records(job, options, seed, seed_source, temporary_input, execution_id)
        finally:
            if temporary_input is not None:
                temporary_input.cleanup()

    @staticmethod
    def _temporary_input(job: JobDefinition) -> TemporaryInput | None:
        plan = job.input_copy
        if job.input is None or plan is None:
            return None
        # Imported here, so jobs that never resize do not load numpy and LittleCMS.
        from draw_things_control.jobs.inputs.resize import TemporaryInput

        return TemporaryInput(job.input, plan)

    def _run_with_records(self, job: JobDefinition, options: JobRunOptions, seed: int, seed_source: str, temporary_input: TemporaryInput | None, execution_id: str | None) -> JobOutcome:
        resume = options.resume
        first_run = resume.first_run if resume is not None else 1
        resumes_execution = resume.resumes_execution if resume is not None else None
        with JobRecords.open(job, write_records=options.write_records, seed=seed, seed_source=seed_source, execution_id=execution_id, clock=self._clock, random_number=self._random_number, first_run=first_run, resumes_execution=resumes_execution) as records:
            previous_handlers = install_signal_handlers(self._token.receive) if self._handle_signals else None
            try:
                return self._run_chain(_Chain(job, records, options, temporary_input, job.schedule()))
            finally:
                restore_signal_handlers(previous_handlers)

    def _run_chain(self, chain: _Chain) -> JobOutcome:
        manifest = chain.records.manifest
        self._emit(job_started_event(chain))
        try:
            return self._run_runs(chain)
        except BaseException:
            # Every started job ends with JobFinished, so a front end never waits on one that raised.
            completed = sum(1 for record in manifest.runs if record.status == RunStatus.SUCCEEDED)
            self._emit(JobFinished(at=self._timestamp(), status=JobStatus.FAILED, exit_code=None, completed_runs=completed, total_runs=chain.total, signal=None))
            raise

    def _run_runs(self, chain: _Chain) -> JobOutcome:
        job, manifest, total = chain.job, chain.records.manifest, chain.total
        report_ignored_config(job)
        chain.records.save()
        resume = chain.options.resume
        start = resume.first_run if resume is not None else 1
        current_input = resume.input if resume is not None else job.input
        if chain.temporary_input is not None:
            logger.info("Run 1 input: temporary copy {} (removed after run 1)", chain.temporary_input.path)
            current_input = chain.temporary_input.path
        completed = 0
        exit_code = 0
        for number, pair in enumerate(chain.schedule[start - 1 :], start=start):
            if self._token.requested is not None:
                exit_code = self._stop(manifest, self._token.requested, f"before run {number}/{total}")
                break
            # A park that landed after a full cooldown, just before this run: only after a run this execution made.
            if number > start and self._token.take_park():
                exit_code = self._park(manifest, number - 1, total)
                break
            run, record, status, exit_code = self._run_step(chain, number, pair, current_input)
            if status != RunStatus.SUCCEEDED:
                manifest.status = JobStatus.INTERRUPTED if status == RunStatus.INTERRUPTED else JobStatus.FAILED
                chain.records.save()
                break
            completed += 1
            chain.records.save()
            current_input = run.last_frame or run.output
            # The park takes effect only here, after the run's finish (its last frame, measuring) and RunFinished, and
            # never after the last run. A stop that landed first wins: until the park is taken, a cancel stops the job.
            # The park count is read before the check, so a park that lands after it, and is then withdrawn, still
            # reads as a park to the cooldown, which waits out the rest.
            parks = self._token.park_count
            if number < total and self._token.requested is None and self._token.take_park():
                exit_code = self._park(manifest, number, total)
                break
            stop = self._wait_between_runs(chain, record, number, parks)
            if isinstance(stop, _ParkedInCooldown):
                exit_code = self._park(manifest, number, total)
                break
            if stop is not None:
                exit_code = self._stop(manifest, *stop)
                break
        else:
            manifest.status = JobStatus.SUCCEEDED
        manifest.finished_at = self._timestamp()
        chain.records.save()
        stopped_by = signal_for_exit_code(exit_code) if manifest.status == JobStatus.INTERRUPTED else None
        self._emit(JobFinished(at=manifest.finished_at, status=manifest.status, exit_code=exit_code, completed_runs=completed, total_runs=total, signal=stopped_by.name if stopped_by is not None else None))
        return JobOutcome(exit_code=exit_code, completed_runs=completed, total_runs=total, manifest=chain.records.manifest_path, log=chain.records.log_path)

    def _run_step(self, chain: _Chain, number: int, pair: PromptPair, current_input: Path | None) -> tuple[PlannedRun, RunRecord, RunStatus, int]:
        """Plan, start, execute, and record one run; returns it with its status and exit code."""
        job = chain.job
        run = self._planner.plan_run(job, number, pair, current_input, chain.records.manifest.seed, chain.options.executable, set())
        record = self._start_run(chain, run)
        try:
            status, exit_code = self._execute_run(chain, run, record)
        except BaseException:
            record.status = RunStatus.FAILED
            # The run raised, so a file counts as kept only if it exists; the record itself is left as the manifest has it.
            self._finish_run(number, record, None, output=record.output if run.output.exists() else None)
            raise
        record.status = status
        record.exit_code = exit_code
        if status != RunStatus.SUCCEEDED and not run.output.exists():
            record.output = None
        self._finish_run(number, record, exit_code, output=record.output)
        if number == 1 and chain.temporary_input is not None:
            chain.temporary_input.cleanup()
        return run, record, status, exit_code

    def _wait_between_runs(self, chain: _Chain, record: RunRecord, number: int, parks: int) -> tuple[signal.Signals, str] | _ParkedInCooldown | None:
        """The cooldown after run ``number``; the signal that cut it short and where, a park that ended it, or None.
        ``parks`` is the park count read before the run boundary's park check."""
        if number >= chain.total or self._token.requested is not None:
            return None
        wait = chain.job.cooldown.wait_after(record.seconds or 0.0)
        return self._cool_down(chain, record, number + 1, wait, parks) if wait.seconds > 0 else None

    def _start_run(self, chain: _Chain, run: PlannedRun) -> RunRecord:
        """Record ``run`` in the manifest and announce it; return its record."""
        pair, number, temporary_input = run.pair, run.number, chain.temporary_input
        record = RunRecord(
            pair=pair.name,
            positive=pair.positive,
            negative=pair.negative,
            # Run 1's temporary copy is gone after the run, so record the job's own input.
            input=str(chain.job.input if number == 1 else run.input) if run.input is not None else None,
            output=run.output.name,
            last_frame=None,
            command=redact_command(run.arguments.command),
            started_at=self._timestamp(),
            resized_input=str(temporary_input.path) if number == 1 and temporary_input is not None else None,
        )
        chain.records.manifest.runs.append(record)
        chain.records.save()
        self._emit(run_started_event(record, run, chain.total))
        return record

    def _cool_down(self, chain: _Chain, record: RunRecord, next_run: int, wait: CooldownWait, parks: int) -> tuple[signal.Signals, str] | _ParkedInCooldown | None:
        """Wait ``wait`` after ``record``'s run; return the signal that cut it short and where, a park that ended it, or None.
        ``parks`` is the park count from before the run boundary: a park asked since then counts as one during the wait."""
        policy, seconds, run_seconds = chain.job.cooldown, wait.seconds, record.seconds or 0.0
        until = (self._clock().astimezone() + timedelta(seconds=seconds)).strftime("%H:%M:%S")
        ratio = policy.ratio if policy.mode == "auto" else None
        self._emit(CooldownStarted(at=self._timestamp(), after_run=next_run - 1, seconds=seconds, until=until, mode=policy.mode, ratio=ratio, run_seconds=run_seconds, bound=wait.bound))
        # Saved at 0 first, so the manifest shows the job is cooling down rather than stuck.
        record.cooldown_after_seconds = 0.0
        chain.records.save()
        waited = self._cooldown(seconds)
        parked = False
        # A wait ends early for a stop or a park. A park withdrawn before it was taken leaves the rest to wait out, so a
        # job that runs on still gets its full cooldown. A wait no park was asked during is never waited again, however
        # little of it the cooldown waited.
        while waited < seconds and self._token.requested is None:
            if self._token.take_park():
                parked = True
                break
            if self._token.park_count == parks:
                break
            parks = self._token.park_count
            waited += self._cooldown(seconds - waited)
        # Only a wait that ended early was cut short; a signal after a full wait stops the job before the next run.
        stopped = self._token.requested if waited < seconds and not parked else None
        record.cooldown_after_seconds = round(waited, 1)
        chain.records.save()
        self._emit(CooldownEnded(at=self._timestamp(), waited_seconds=record.cooldown_after_seconds, cut_short=stopped is not None or parked))
        if parked:
            return _ParkedInCooldown()
        if stopped is not None:
            return stopped, f"during the cooldown before run {next_run}/{chain.total} (waited {seconds_text(round(waited, 1))} of {seconds_text(seconds)})"
        return None

    @staticmethod
    def _stop(manifest: JobManifest, received_signal: signal.Signals, where: str) -> int:
        """Mark the job interrupted by ``received_signal``; return its exit code."""
        logger.warning("Job stopped by {} {}", received_signal.name, where)
        manifest.status = JobStatus.INTERRUPTED
        return exit_code_for_signal(received_signal)

    @staticmethod
    def _park(manifest: JobManifest, number: int, total: int) -> int:
        """Mark the job parked after run ``number`` of the chain; return its exit code."""
        logger.info("Job parked after run {}/{}", number, total)
        manifest.status = JobStatus.PARKED
        return EXIT_PARKED

    def _finish_run(self, number: int, record: RunRecord, exit_code: int | None, *, output: str | None) -> None:
        self._emit(RunFinished(at=self._timestamp(), number=number, status=record.status, exit_code=exit_code, seconds=record.seconds, output=output, last_frame=record.last_frame, output_width=record.output_width, output_height=record.output_height, output_frames=record.output_frames))

    def _output_callback(self, number: int) -> MessageCallback | None:
        """A callback that turns each child line of run ``number`` into a RunOutput, or None without an observer."""
        if self._observer is None:
            return None

        def on_message(message: ProcessMessage) -> None:
            self._emit(RunOutput(at=self._timestamp(), number=number, stream=message.stream.value, text=message.text, progress=message.progress, percent=message.percent))

        return on_message

    def _execute_run(self, chain: _Chain, run: PlannedRun, record: RunRecord) -> tuple[RunStatus, int]:
        return self._launcher.launch(chain.job, run, record, shutdown_grace=chain.options.shutdown_grace, on_message=self._output_callback(run.number), on_start=self._on_child_start)


def job_started_event(chain: _Chain) -> JobStarted:
    job, manifest, records = chain.job, chain.records.manifest, chain.records
    return JobStarted(
        at=manifest.started_at,
        job_name=job.name,
        job_file=manifest.job_file,
        source_text=job.source_text,
        mode=manifest.mode,
        total_runs=chain.total,
        output_directory=str(job.output_directory),
        input=str(job.input) if job.input is not None else None,
        model=job.model,
        seed=manifest.seed,
        seed_source=manifest.seed_source,
        cooldown=job.cooldown,
        cooldown_source=job.cooldown_source,
        manifest=str(records.manifest_path) if records.manifest_path is not None else None,
        log=str(records.log_path) if records.log_path is not None else None,
        config_file=manifest.config_file,
        config_override=manifest.config_override,
        input_resize=manifest.input_resize,
        execution_id=manifest.execution_id,
        first_run=manifest.first_run,
        resumes_execution=manifest.resumes_execution,
    )


def run_started_event(record: RunRecord, run: PlannedRun, total: int) -> RunStarted:
    return RunStarted(
        at=record.started_at,
        number=run.number,
        total=total,
        pair=run.pair.name,
        positive=run.pair.positive,
        negative=run.pair.negative,
        input=record.input,
        resized_input=record.resized_input,
        output=run.output.name,
        last_frame=run.last_frame.name if run.last_frame is not None else None,
        command=tuple(record.command),
    )
