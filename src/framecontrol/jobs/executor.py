"""Run every generation a job describes, as one chain of runs."""

from __future__ import annotations

import random
import shutil
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from framecontrol.core.arguments import CommandArguments
from framecontrol.core.clock import Clock, local_timestamp
from framecontrol.core.cooldown import CooldownWait
from framecontrol.core.errors import InputError
from framecontrol.core.exit_codes import EXIT_PARKED, exit_code_for_signal, signal_for_exit_code
from framecontrol.core.generation import GenerationOutcome
from framecontrol.core.process.output import MessageCallback, ProcessMessage
from framecontrol.core.process.runner import ChildStartCallback, JobRunnerFactory, StoppableRunner
from framecontrol.core.process.signals import CancelToken, install_signal_handlers, restore_signal_handlers
from framecontrol.jobs.backend import H3_BACKEND, BackendRegistry, RunContext
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.jobs.definition import GenerationMode, JobDefinition, PromptPair
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, FirstImageDropped, JobEvent, JobFinished, JobObserver, JobStarted, JobStatus, MediaChecked, RunFinished, RunOutput, RunStarted, RunStatus, notify
from framecontrol.jobs.launcher import RunLauncher
from framecontrol.jobs.log_writer import JobLogWriter
from framecontrol.jobs.media.checks import MediaCheck
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.media.tools import find_ffmpeg
from framecontrol.jobs.output_naming import RandomNumber, random_four_digits
from framecontrol.jobs.planning import JobPlanner, JobPreview, PlannedRun
from framecontrol.jobs.records import JobManifest, JobRecords, RunRecord
from framecontrol.jobs.run_timing import BeforeStart, RunDescription, RunTiming
from framecontrol.jobs.text import report_ignored_config, seconds_text
from framecontrol.jobs.timing.environment import HostLoad
from framecontrol.jobs.timing.environment import host_load as read_host_load
from framecontrol.jobs.timing.model import Reading
from framecontrol.jobs.timing.parsers import StageParser

if TYPE_CHECKING:
    from framecontrol.jobs.inputs.resize import TemporaryInput

# Waits up to the given seconds between runs and returns the seconds actually waited. It may return early; the executor
# waits again, for the rest, only when a park ended the wait and was withdrawn before it took effect.
Cooldown = Callable[[float], float]
# Work done for a run before it starts that took less than this gets no span of its own.
MIN_BEFORE_START_SECONDS = 0.001


@dataclass(frozen=True)
class ResumePoint:
    """Where a resumed chain starts: the first unfinished run, its input (the last succeeded run's last frame or
    output), and the seed to keep. ``resumes_execution`` names the execution (E0012) this one continues, for the
    manifest and JobStarted; the executor itself does not read it."""

    first_run: int
    input: Path | None
    seed: int
    resumes_execution: str | None = None
    # The resumed execution's first image, and the anchor of the run this one continues after; None
    # for an execution recorded before them, or without one.
    first_image: Path | None = None
    anchor: Path | None = None
    backend_fingerprint: str | None = None


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
    # Run 1's copy of the input (upright, 8-bit sRGB, the job's size), removed after run 1.
    temporary_input: TemporaryInput | None
    schedule: tuple[PromptPair, ...]
    # The chain's first image once it exists, and why the color checks go without one.
    first_image: Path | None = None
    first_image_notes: tuple[str, ...] = field(default=())
    # The file a correcting job's first and blend anchors hold its colors to: the first image, or the input of the run
    # that last re-anchored; a resume starts from the anchor of the run it continues after.
    anchor: Path | None = None
    # Version 2 only: a run that needs no handoff (``r2av``, ``t2v``) after run 1 starts an independent segment, which
    # resets the anchor and is held to its own first image: the input of its first handoff-consuming run.
    segmented: bool = False
    segment_first_image: Path | None = None
    # Version 2: a run that consumes a handoff across a backend or settings-profile boundary reads a copy its own profile
    # prepared (``boundary_copy``) from the producer's file (``boundary_source``), removed after the run.
    boundary_copy: TemporaryInput | None = None
    boundary_source: Path | None = None

    @property
    def total(self) -> int:
        return len(self.schedule)


class JobExecutor:
    """Executes a job's runs in order, chaining their outputs; planning, the manifest, the log, and finishing an output are its collaborators'."""

    def __init__(
        self,
        runner_factory: JobRunnerFactory[StoppableRunner],
        find_executable: Callable[[str], str | None],
        media: MediaTools,
        *,
        clock: Clock = datetime.now,
        random_number: RandomNumber = random_four_digits,
        random_seed: Callable[[], int] = lambda: random.randint(0, 2**32 - 1),
        handle_signals: bool = True,
        cooldown: Cooldown | None = None,
        backends: BackendRegistry = DEFAULT_BACKENDS,
        monotonic: Callable[[], float] = time.monotonic,
        host_load: Callable[[], HostLoad | None] = read_host_load,
    ) -> None:
        self._clock = clock
        # The clock every run's stage timeline reads, apart from the wall clock above and the run timer the launcher keeps.
        self._monotonic = monotonic
        self._host_load = host_load
        self._find_executable = find_executable
        self._random_number = random_number
        self._handle_signals = handle_signals
        self._backends = backends
        self._planner = JobPlanner(find_executable, media, clock=clock, random_number=random_number, random_seed=random_seed, backends=backends)
        self._token = CancelToken(handle_signals=handle_signals)
        self._cooldown = cooldown or self._token.wait
        self._media = media
        self._launcher = RunLauncher(runner_factory, find_executable, media, self._token, backends, self._report_check)
        self._observer: JobObserver | None = None
        self._log_writer = JobLogWriter()
        self._on_child_start: ChildStartCallback | None = None
        # The run in progress's timing, and work done for the runs to come before they have started (resizing the input, staging
        # references), by the number of the run it serves.
        self._timing: RunTiming | None = None
        self._timing_read_failed = False
        self._before_start: dict[int, list[BeforeStart]] = {}

    def preview(self, job: JobDefinition, *, executable: str, seed: int | None = None) -> JobPreview:
        """Validate that the job can start and describe every run without running anything (see ``JobPlanner.preview``)."""
        return self._planner.preview(job, executable=executable, seed=seed)

    @property
    def backends(self) -> BackendRegistry:
        """The installed adapters shared by parsing, previews, and execution."""
        return self._backends

    def run(self, job: JobDefinition, options: JobRunOptions) -> JobOutcome:
        """Run the job's runs in order; stop at the first failed, timed-out, or interrupted run.

        Only one job runs at a time on an executor: a second concurrent call raises RuntimeError.
        """
        self._token.begin()
        self._observer, self._on_child_start = options.observer, options.on_child_start
        self._log_writer = JobLogWriter()
        self._timing, self._before_start = None, {}
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

    def run_generation(self, arguments: CommandArguments, *, timeout: float, shutdown_grace: float, on_begin: Callable[[], None] | None = None, on_child_start: ChildStartCallback | None = None) -> GenerationOutcome:
        """Run a one-off generation request with the same cancellable runner a job uses."""
        self._token.begin()
        try:
            if on_begin is not None:
                on_begin()
            return self._launcher.generate(arguments, timeout=timeout, shutdown_grace=shutdown_grace, on_start=on_child_start)
        finally:
            self._token.end()

    def park(self) -> bool:
        """Ask the running job to end once its current run finishes; safe from any thread.

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

    def _report_check(self, run: int, check: MediaCheck) -> None:
        self._emit(MediaChecked(at=self._timestamp(), run=run, stage=check.stage, file=check.file, summary=check.summary, verdict=check.verdict, notes=check.warnings + check.notes, facts=check.facts))

    def _check_input(self, chain: _Chain, run: int, run_input: Path | None) -> None:
        """An image-conditioned video run's input, and its resized copy, checked before the run that reads them; a
        resume's input, the chain's own last frame, is checked as a handoff."""
        checker = self._media.checker
        if checker is None or run_input is None or not chain.job.run_checks_input(run):
            return
        if chain.options.resume is not None:
            self._report_check(run, checker.handoff(run_input))
            return
        source = chain.job.input if chain.temporary_input is not None else run_input
        if source is None:
            return
        self._report_check(run, checker.input(source))
        plan = chain.job.input_copy
        if chain.temporary_input is not None and plan is not None:
            self._report_check(run, checker.resized_input(source, chain.temporary_input.path, plan))

    def _timestamp(self) -> str:
        return local_timestamp(self._clock())

    @contextmanager
    def _before_start_stage(self, stage: str, run: int) -> Iterator[None]:
        """Time the block as work done for run ``run`` before it starts, which that run's timing takes up, unless it took no time worth a span."""
        started = self._monotonic()
        yield
        ended = self._monotonic()
        if ended - started >= MIN_BEFORE_START_SECONDS:
            self._before_start.setdefault(run, []).append((stage, started, ended))

    def _run(self, job: JobDefinition, options: JobRunOptions) -> JobOutcome:
        if options.shutdown_grace < 0:
            raise InputError("--shutdown-grace must not be negative")
        self._planner.check_tools(job, options.executable, first_run=options.resume.first_run if options.resume is not None else 1)
        first_run = options.resume.first_run if options.resume is not None else 1
        # Only an H3 run still to come depends on the H3 executable, so a Draw Things tail resumes whatever H3 did.
        runs_h3 = any(job.run_backend(number) == H3_BACKEND for number in range(first_run, job.run_count + 1))
        if options.resume is not None and options.resume.backend_fingerprint is not None and runs_h3:
            current = getattr(self._backends.get(H3_BACKEND), "_installation", {}).get("executable_sha256")
            if current != options.resume.backend_fingerprint:
                raise InputError("H3 executable changed since the previous execution; refusing resume")

        cleanups: list[Callable[[], None]] = []
        if job.effective_runs:
            prepared_runs = []
            for effective in job.effective_runs:
                prepare_job = getattr(self._backends.get(effective.backend), "prepare_job", None)
                backend_job = effective.backend_job
                if prepare_job is not None:
                    with self._before_start_stage("stage_references", effective.number):
                        backend_job, cleanup = prepare_job(backend_job)
                    cleanups.append(cleanup)
                prepared_runs.append(replace(effective, backend_job=backend_job))
            job = replace(job, effective_runs=tuple(prepared_runs))
        else:
            prepare_job = getattr(self._backends.get(job.backend), "prepare_job", None)
            if prepare_job is not None:
                with self._before_start_stage("stage_references", first_run):
                    job, cleanup = prepare_job(job)
                cleanups.append(cleanup)
        temporary_input: TemporaryInput | None = None
        try:
            if options.resume is not None:
                # The chain keeps its original seed; run 1 is not run again, so it needs no resized copy of the input.
                seed, seed_source, temporary_input = options.resume.seed, "resume", None
            else:
                seed, seed_source = self._planner.seed(job, None)
                # Resize before the output directory, manifest, or log exist, so a bad image leaves nothing behind.
                # A job with no copy to make returns at once, which is under the span threshold.
                with self._before_start_stage("prepare_input", first_run):
                    temporary_input = self._temporary_input(job)
            # Taken last of the checks, so a job refused above never uses up a number; a job that cannot get one does not start.
            execution_id = options.reserve_execution_id() if options.reserve_execution_id is not None else None
            return self._run_with_records(job, options, seed, seed_source, temporary_input, execution_id)
        finally:
            if temporary_input is not None:
                temporary_input.cleanup()
            for cleanup in reversed(cleanups):
                cleanup()

    @staticmethod
    def _temporary_input(job: JobDefinition) -> TemporaryInput | None:
        plan = job.input_copy
        if job.input is None or plan is None:
            return None
        # Imported here, so a t2v job, which has no input, does not load numpy and LittleCMS.
        from framecontrol.jobs.inputs.resize import TemporaryInput

        # ffmpeg reads a 16-bit RGB input with every bit; an image job, which does not need it, reads high bytes without it.
        return TemporaryInput(job.input, plan, find_ffmpeg())

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
        self._keep_first_image(chain)
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
        with self._before_start_stage("check_input", start):
            self._check_input(chain, start, current_input)
        completed = 0
        exit_code = 0
        # A resume that lands after an independent segment start holds to the anchor it was given, not to run 1's image.
        chain.segmented = any(not job.run_needs_visual_input(earlier) for earlier in range(2, start)) if job.effective_runs else False
        for number, pair in enumerate(chain.schedule[start - 1 :], start=start):
            if self._token.requested is not None:
                exit_code = self._stop(manifest, self._token.requested, f"before run {number}/{total}")
                break
            # A park that landed after a full cooldown, just before this run: only after a run this execution made.
            if number > start and self._token.take_park():
                exit_code = self._park(manifest, number - 1, total)
                break
            run_input = current_input if job.run_needs_visual_input(number) or not job.effective_runs else None
            try:
                run_input = self._prepare_boundary(chain, number, run_input)
            except (ValueError, OSError) as error:
                logger.error("Run {} cannot take the handoff from run {}: {}", number, number - 1, error)
                manifest.status = JobStatus.FAILED
                exit_code = 1
                break
            try:
                run, record, status, exit_code = self._run_step(chain, number, pair, run_input)
            finally:
                self._drop_boundary(chain)
            if status != RunStatus.SUCCEEDED:
                manifest.status = JobStatus.INTERRUPTED if status == RunStatus.INTERRUPTED else JobStatus.FAILED
                chain.records.save()
                break
            completed += 1
            chain.records.save()
            if number < total and job.run_backend(number) == H3_BACKEND and job.run_needs_visual_input(number + 1) and record.last_frame is None and run.last_frame is not None:
                logger.error("Run {} succeeded, but run {} requires a verified visual handoff that is unavailable", number, number + 1)
                manifest.status = JobStatus.FAILED
                exit_code = 1
                break
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

    def _keep_first_image(self, chain: _Chain) -> None:
        """Keep the chain's first image for a video job: run 1's copy of the input, named from the
        manifest's stem, with or without records; a t2v job's is run 1's last frame, written after run 1; a resume uses
        the resumed execution's."""
        job, manifest, resume = chain.job, chain.records.manifest, chain.options.resume
        if not job.mode.is_video:
            return
        if job.mode is GenerationMode.H3AV and job.backend_data is not None:
            from framecontrol.jobs.backends.h3 import H3JobData

            if isinstance(job.backend_data, H3JobData) and job.backend_data.runs[0].pipeline == "r2av":
                return
        if resume is not None:
            if resume.anchor is not None and resume.anchor.is_file():
                chain.anchor = resume.anchor
            elif resume.anchor is not None:
                # As an anchor that was never recorded: the chain is held to the first image, when it is kept.
                logger.warning("The anchor {} of the run this resume continues after is gone; the chain is held to the first image", resume.anchor)
            if resume.first_image is not None and resume.first_image.is_file():
                chain.first_image = resume.first_image
                manifest.first_image = str(resume.first_image)
            else:
                chain.first_image_notes = ("The execution it resumes kept no first image, so drift since the first image is left out.",)
            return
        path = chain.records.first_image_path
        if path is None:
            return
        if chain.temporary_input is None:
            # A t2v job's, which JobStarted names before run 1 writes it.
            manifest.first_image = str(path)
            return
        if _copy_first_image(chain, chain.temporary_input.path, path):
            manifest.first_image = str(path)

    def _keep_t2v_first_image(self, chain: _Chain, run: PlannedRun) -> None:
        """A t2v job's first image is run 1's last frame, the first frame any of its runs is given."""
        planned = chain.records.manifest.first_image
        if planned is None:
            return
        if run.last_frame is None or not run.last_frame.is_file():
            chain.first_image_notes = ("Run 1 left no last frame, so no first image is kept, and drift since the first image is left out.",)
            self._drop_t2v_first_image(chain, chain.first_image_notes[0])
        elif not _copy_first_image(chain, run.last_frame, Path(planned)):
            self._drop_t2v_first_image(chain, chain.first_image_notes[0])

    def _drop_t2v_first_image(self, chain: _Chain, reason: str) -> None:
        """Name no first image once a t2v job's is not kept after all, in the manifest and, through FirstImageDropped,
        in the state store, as the i2v branch names none that was never written; nothing when none is named."""
        manifest = chain.records.manifest
        if manifest.first_image is None:
            return
        manifest.first_image = None
        self._emit(FirstImageDropped(at=self._timestamp(), reason=reason))

    def _run_step(self, chain: _Chain, number: int, pair: PromptPair, current_input: Path | None) -> tuple[PlannedRun, RunRecord, RunStatus, int]:
        """Plan, start, execute, and record one run; returns it with its status and exit code."""
        job = chain.job
        run = self._planner.plan_run(job, number, pair, current_input, chain.records.manifest.seed, chain.options.executable, set())
        durable_input = chain.boundary_source or current_input
        first_image = self._segment_first_image(chain, number, durable_input)
        context = RunContext(number, pair, durable_input, first_image, chain.anchor, chain.schedule[number - 2] if number > 1 else None, chain.first_image_notes, _fresh_t2v(chain, number))
        prepared = self._backends.get(job.run_backend(number)).prepare_run(job.job_for_run(number), context)
        chain.anchor = prepared.chain_anchor
        record = self._start_run(chain, run, prepared.record_anchor)
        try:
            status, exit_code = self._execute_run(chain, run, record, prepared.finish_state)
        except BaseException:
            record.status = RunStatus.FAILED
            if _fresh_t2v(chain, number):
                self._drop_t2v_first_image(chain, "Run 1 failed, so no first image is kept.")
            self._finish_timing(complete=False)
            # The run raised, so a file counts as kept only if it exists; the record itself is left as the manifest has it.
            self._finish_run(number, record, None, output=record.output if run.output.exists() else None)
            raise
        record.status = status
        record.exit_code = exit_code
        if status != RunStatus.SUCCEEDED and not run.output.exists():
            record.output = None
        if _fresh_t2v(chain, number):
            if status == RunStatus.SUCCEEDED:
                self._keep_t2v_first_image(chain, run)
            else:
                self._drop_t2v_first_image(chain, f"Run 1 {status.replace('_', ' ')}, so no first image is kept.")
        self._finish_timing(complete=status == RunStatus.SUCCEEDED)
        self._finish_run(number, record, exit_code, output=record.output)
        if number == 1 and chain.temporary_input is not None:
            chain.temporary_input.cleanup()
        return run, record, status, exit_code

    def _prepare_boundary(self, chain: _Chain, number: int, current_input: Path | None) -> Path | None:
        """The input run ``number`` reads: ``current_input`` as it is, or, when the run before it was another backend or
        profile, a copy its own profile prepares from that file (see ``plan_input`` on the adapters)."""
        job = chain.job
        if not job.effective_runs or number < 2 or current_input is None or not job.run_needs_visual_input(number):
            return current_input
        producer, consumer = job.effective_runs[number - 2], job.effective_runs[number - 1]
        if producer.backend == consumer.backend and producer.settings_profile == consumer.settings_profile:
            return current_input
        plan_input = getattr(self._backends.get(consumer.backend), "plan_input", None)
        if plan_input is None:
            return current_input
        from framecontrol.jobs.inputs.resize import TemporaryInput

        with self._before_start_stage("handoff_copy", number):
            copy = TemporaryInput(current_input, plan_input(consumer.backend_job, current_input), find_ffmpeg())
        chain.boundary_copy, chain.boundary_source = copy, current_input
        logger.info("Run {} input: {}'s handoff prepared by profile {} as {} (removed after run {})", number, producer.settings_profile, consumer.settings_profile, copy.path, number)
        return copy.path

    @staticmethod
    def _drop_boundary(chain: _Chain) -> None:
        if chain.boundary_copy is not None:
            chain.boundary_copy.cleanup()
        chain.boundary_copy = chain.boundary_source = None

    @staticmethod
    def _input_origin(job: JobDefinition, number: int) -> tuple[int | None, str | None]:
        """Where run ``number``'s image comes from, for its record: the earlier run and role, or the job's own input."""
        run = job.effective_run(number)
        if run is None or not run.requires_visual_input:
            return None, None
        if number == 1:
            return None, "initial"
        return number - 1, "output" if job.effective_runs[number - 2].pipeline == "i2i" else "handoff"

    @staticmethod
    def _segment_first_image(chain: _Chain, number: int, current_input: Path | None) -> Path | None:
        """The first image run ``number``'s colors are measured against: the chain's, until a version-2 run that needs
        no handoff starts a new segment; from then on the input of that segment's first handoff-consuming run."""
        job = chain.job
        if not job.effective_runs:
            return chain.first_image
        if number > 1 and not job.run_needs_visual_input(number):
            chain.segmented, chain.segment_first_image, chain.anchor = True, None, None
        if not chain.segmented:
            return chain.first_image
        if chain.segment_first_image is None and current_input is not None and job.run_needs_visual_input(number):
            chain.segment_first_image = current_input
        return chain.segment_first_image

    def _wait_between_runs(self, chain: _Chain, record: RunRecord, number: int, parks: int) -> tuple[signal.Signals, str] | _ParkedInCooldown | None:
        """The cooldown after run ``number``; the signal that cut it short and where, a park that ended it, or None.
        ``parks`` is the park count read before the run boundary's park check."""
        if number >= chain.total or self._token.requested is not None:
            return None
        wait = chain.job.cooldown.wait_after(record.seconds or 0.0)
        return self._cool_down(chain, record, number + 1, wait, parks) if wait.seconds > 0 else None

    def _start_run(self, chain: _Chain, run: PlannedRun, anchor: Path | None = None) -> RunRecord:
        """Record ``run`` in the manifest and announce it, with the file its colors are held to; return its record."""
        pair, number, temporary_input = run.pair, run.number, chain.temporary_input
        effective = chain.job.effective_run(number)
        record = RunRecord(
            pair=pair.name,
            positive=pair.positive,
            negative=pair.negative,
            # Run 1's temporary copy is gone after the run, so record the job's own input.
            input=str(chain.job.input if number == 1 else chain.boundary_source or run.input) if run.input is not None else None,
            output=run.output.name,
            last_frame=None,
            command=list(self._backends.get(chain.job.run_backend(number)).redact(run.arguments.command)),
            started_at=self._timestamp(),
            resized_input=str(temporary_input.path) if number == 1 and temporary_input is not None else str(chain.boundary_copy.path) if chain.boundary_copy is not None else None,
            anchor=str(anchor) if anchor is not None else None,
            backend=effective.backend if effective is not None else None,
            pipeline=effective.pipeline if effective is not None else None,
            settings_profile=effective.settings_profile if effective is not None else None,
            knobs=dict(effective.knobs) if effective is not None else {},
            knob_sources=dict(effective.knob_sources) if effective is not None else {},
        )
        record.input_run, record.input_role = self._input_origin(chain.job, number)
        if effective is not None and "seed" not in record.knobs and run.seed is not None:
            # The plan froze no seed for this run; the record keeps the one the execution actually used.
            record.knobs["seed"], record.knob_sources["seed"] = run.seed, "execution_seed"
        chain.records.manifest.runs.append(record)
        chain.records.save()
        self._timing_read_failed = False
        timing = self._timing = RunTiming(run.number, self._monotonic, self._stage_parser(chain.job, run.number), self._emit, self._timestamp, self._host_load)
        self._emit(run_started_event(record, run, chain.total))
        before_start = self._before_start.pop(run.number, [])
        # Under the backend's own id, whichever spelling the file used, so runs of one backend group together.
        timing.begin(self._backends.get(chain.job.run_backend(run.number)).id, record.pipeline or chain.job.mode.value, *self._timing_facts(chain, run.number), before_start)
        return record

    def _stage_parser(self, job: JobDefinition, number: int) -> StageParser | None:
        """The parser of run ``number``'s output into stage readings, or None for a backend without one."""
        try:
            make = getattr(self._backends.get(job.run_backend(number)), "stage_parser", None)
            return make() if make is not None else None
        except Exception:
            # Timing never stops a run.
            logger.exception("Run {}'s stage parser could not be made; its output is read for no stages", number)
            return None

    def describe_run(self, job: JobDefinition, number: int, *, executable: str) -> RunDescription:
        """What run ``number`` will be timed as: its backend, pipeline, workload, and environment, from the job's compiled plan
        and the backend's own description, as ``TimingStarted`` will carry them. A forecast prices a run that has not started
        by the signature of this, so ``_timing_facts`` takes the run's timing from here and the two cannot differ."""
        backend = self._backends.get(job.run_backend(number))
        effective = job.effective_run(number)
        workload_of = getattr(backend, "timing_workload", None)
        environment_of = getattr(backend, "timing_environment", None)
        run_job = job.job_for_run(number)
        workload = workload_of(run_job, number) if workload_of is not None else {}
        knobs = {name: value for name, value in (effective.knobs if effective is not None else {}).items() if name != "seed"}
        if knobs:
            workload = {**workload, "knobs": knobs}
        environment = environment_of(run_job, self._find_executable(executable)) if environment_of is not None else {}
        pipeline = (effective.pipeline if effective is not None else None) or job.mode.value
        # Under the backend's own id, whichever spelling the file used, so runs of one backend group together.
        return RunDescription(backend.id, pipeline, workload, environment)

    def _timing_facts(self, chain: _Chain, number: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """What run ``number``'s timing is judged by: its workload and its environment, as its backend describes them."""
        try:
            described = self.describe_run(chain.job, number, executable=chain.options.executable)
            return described.workload, described.environment
        except Exception:
            # Timing never stops a run.
            logger.exception("Run {} could not be described for its timing; it is timed with no workload", number)
            return {}, {}

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
        self._emit(RunFinished(at=self._timestamp(), number=number, status=record.status, exit_code=exit_code, seconds=record.seconds, output=output, last_frame=record.last_frame, output_width=record.output_width, output_height=record.output_height, output_frames=record.output_frames, output_frame_rate=record.output_frame_rate, output_duration=record.output_duration, audio_codec=record.audio_codec, audio_sample_rate=record.audio_sample_rate, audio_channels=record.audio_channels, audio_duration=record.audio_duration, corrected_output=record.corrected_output))

    def _output_callback(self, number: int) -> MessageCallback | None:
        """A callback that turns each child line of run ``number`` into a RunOutput, and reads it as a stage reading."""

        def on_message(message: ProcessMessage) -> None:
            reading = self._read_stage(message)
            self._emit(RunOutput(at=self._timestamp(), number=number, stream=message.stream.value, text=message.text, phase=reading.phase if reading is not None else None, offset=self._timing.run_offset(message.elapsed_seconds) if self._timing is not None else message.elapsed_seconds, stage=reading.stage if reading is not None else None, done=reading.done if reading is not None else None, total=reading.total if reading is not None else None))

        return on_message

    def _read_stage(self, message: ProcessMessage) -> Reading | None:
        """The reading ``message`` gave, read into the run's timeline; None for a line that is none. A parser that fails
        ends the run's reading of stages, not the run (timing never stops a run)."""
        if self._timing is None or self._timing_read_failed:
            return None
        try:
            return self._timing.reading(message.text, message.elapsed_seconds)
        except Exception:
            self._timing_read_failed = True
            logger.exception("Reading the child's output for stages failed; the rest of this run's lines are shown with no stage")
            return None

    def _finish_timing(self, *, complete: bool) -> None:
        """Close the run's timeline: its stages add up to the run, every gap named, and it is complete or not."""
        timing, self._timing = self._timing, None
        if timing is None:
            return
        try:
            timing.finish(complete)
        except Exception:
            logger.exception("Closing the run's timing failed; the run is unaffected")

    def _execute_run(self, chain: _Chain, run: PlannedRun, record: RunRecord, finish_state: object | None) -> tuple[RunStatus, int]:
        return self._launcher.launch(chain.job.job_for_run(run.number), run, record, shutdown_grace=chain.options.shutdown_grace, on_message=self._output_callback(run.number), on_start=self._on_child_start, finish_state=finish_state, timeline=self._timing.timeline if self._timing is not None else None)


def _copy_first_image(chain: _Chain, source: Path, path: Path) -> bool:
    """Copy ``source`` to the first image ``path``; False, saying why for the color checks, when the copy fails."""
    try:
        shutil.copyfile(source, path)
    except OSError as error:
        logger.warning("Could not keep the first image {}: {}", path.name, error)
        chain.first_image_notes = (f"The first image could not be kept ({error}), so drift since it is left out.",)
        return False
    chain.first_image = path
    return True


def _fresh_t2v(chain: _Chain, number: int) -> bool:
    """Whether run ``number`` is a t2v job's run 1, not resumed: it has no input, and its last frame becomes the first image."""
    return chain.job.mode is GenerationMode.T2V and number == 1 and chain.options.resume is None


def job_started_event(chain: _Chain) -> JobStarted:
    job, manifest, records = chain.job, chain.records.manifest, chain.records
    return JobStarted(
        at=manifest.started_at,
        job_name=job.name,
        job_file=manifest.job_file,
        source_text=job.source_text,
        mode=manifest.mode,
        backend=job.backend,
        backend_settings=dict(job.backend_settings),
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
        first_image=manifest.first_image,
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
        anchor=record.anchor,
        backend=record.backend,
        pipeline=record.pipeline,
        settings_profile=record.settings_profile,
        knobs=dict(record.knobs),
        knob_sources=dict(record.knob_sources),
        input_run=record.input_run,
        input_role=record.input_role,
    )
