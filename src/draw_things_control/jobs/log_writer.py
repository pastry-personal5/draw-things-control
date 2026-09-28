"""The lines a job writes to the log, read from its events: the log on the terminal, and the job's log file."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from draw_things_control.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, RunFinished, RunStarted, RunStatus
from draw_things_control.jobs.text import auto_wait_text, cooldown_summary, seconds_text


class JobLogWriter:
    """Turns each event of one job into its log line. Call it with every event, in order; it keeps what a later line names."""

    def __init__(self) -> None:
        self._name = ""
        self._total = 0
        self._output_directory = Path()
        self._records = ""
        self._next_run = 0
        # Above 1 only for a resume: this manifest's own completed_runs then understates the chain's true progress.
        self._first_run = 1

    def __call__(self, event: JobEvent) -> None:
        if isinstance(event, JobStarted):
            self._job_started(event)
        elif isinstance(event, RunStarted):
            self._run_started(event)
        elif isinstance(event, RunFinished):
            self._run_finished(event)
        elif isinstance(event, CooldownStarted):
            self._cooldown_started(event)
        elif isinstance(event, CooldownEnded):
            self._cooldown_ended(event)
        elif isinstance(event, JobFinished):
            self._job_finished(event)

    def _job_started(self, event: JobStarted) -> None:
        self._name, self._total, self._output_directory = event.job_name, event.total_runs, Path(event.output_directory)
        self._first_run = event.first_run
        self._records = f"; manifest {event.manifest}; log {event.log}" if event.manifest is not None else ""
        execution = f", execution {event.execution_id}" if event.execution_id is not None else ""
        logger.info("Job {} ({}{}): {} runs, seed {} (from {}), {}{}", event.job_name, event.mode, execution, event.total_runs, event.seed, event.seed_source, cooldown_summary(event.cooldown, event.cooldown_source, "from "), self._records)

    def _run_started(self, event: RunStarted) -> None:
        # Run 1's input is its temporary copy, when it has one.
        run_input = event.resized_input or event.input
        logger.info("Run {}/{} (pair {}): input={}, output={}", event.number, event.total, event.pair, run_input or "(none, text only)", self._output_directory / event.output)

    def _run_finished(self, event: RunFinished) -> None:
        # A run that raised has no exit code: its exception is logged where it was raised.
        if event.status == RunStatus.SUCCEEDED or event.exit_code is None:
            return
        partial = f"; partial output kept: {self._output_directory / event.output}" if event.output is not None else ""
        logger.error("Run {}/{} {} with exit code {}{}", event.number, self._total, event.status.replace("_", " "), event.exit_code, partial)

    def _cooldown_started(self, event: CooldownStarted) -> None:
        self._next_run = event.after_run + 1
        if event.mode == "auto":
            wait = auto_wait_text(event.seconds, event.ratio or 0.0, event.after_run, event.run_seconds or 0.0, event.bound, commas=True)
        else:
            wait = seconds_text(event.seconds)
        logger.info("Cooldown: waiting {} before run {}/{} (until {})", wait, self._next_run, self._total, event.until)

    def _cooldown_ended(self, event: CooldownEnded) -> None:
        if not event.cut_short:
            logger.info("Cooldown finished; starting run {}/{}", self._next_run, self._total)

    def _job_finished(self, event: JobFinished) -> None:
        # A job that raised has no exit code: its exception is logged where it was raised.
        if event.exit_code is not None:
            # The runs before first_run already succeeded, in whichever execution ran them: without adding them
            # back, a completed resumed chain would log as if it had stopped short.
            completed = self._first_run - 1 + event.completed_runs
            logger.info("Job {} {}: {}/{} runs completed{}", self._name, event.status, completed, event.total_runs, self._records)
