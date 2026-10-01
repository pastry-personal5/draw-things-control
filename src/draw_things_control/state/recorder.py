"""Record a job's events in the state store as they arrive."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger

from draw_things_control.jobs.events import CooldownEnded, FirstImageDropped, JobEvent, JobFinished, JobStarted, MediaChecked, RunFinished, RunStarted
from draw_things_control.state.database import StateError
from draw_things_control.state.executions import ExecutionSettings, MediaCheckRow, NewExecution, NewRun
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id
from draw_things_control.state.store import Store


class ExecutionRecorder:
    """A job observer that writes one execution and its runs to the store.

    A failure is logged once and stops recording for this execution, so later events never update rows that
    were not created; the generation carries on. A media check is the exception (owner decision): it is a diagnostic
    that no later event depends on, so one that cannot be stored is skipped, logged once, and recording goes on.
    """

    def __init__(self, store: Store) -> None:
        self._executions = store.executions
        self._path = store.path
        self._execution_id: int | None = None
        self._label: str | None = None
        self._last_run: int | None = None
        self._failed = False
        self._check_failed = False

    @property
    def execution_id(self) -> int | None:
        """The row of the execution being recorded, once JobStarted has created it; None before, or if that failed."""
        return None if self._failed else self._execution_id

    @property
    def execution_label(self) -> str | None:
        """The execution's ID as people see it (E0012), once it is recorded; None before, or once recording failed."""
        return None if self._failed or self._execution_id is None else self._label

    def reserve(self) -> str:
        """Reserve the execution's ID for JobExecutor.run; raises StateError when the store cannot give one, so the job
        does not start."""
        try:
            self._label = execution_id_text(self._executions.reserve_number())
        except sqlite3.Error as error:
            raise StateError(f"Cannot give the execution an ID in the state database {self._path}: {error}") from error
        return self._label

    def __call__(self, event: JobEvent) -> None:
        if self._failed:
            return
        try:
            self._record(event)
        except Exception:
            self._failed = True
            logger.exception("Recording the execution failed; the rest of this job is not recorded")

    def _record(self, event: JobEvent) -> None:
        if isinstance(event, JobStarted):
            self._job_started(event)
        elif self._execution_id is None:
            return
        elif isinstance(event, RunStarted):
            self._run_started(self._execution_id, event)
        elif isinstance(event, RunFinished):
            self._run_finished(self._execution_id, event)
        elif isinstance(event, CooldownEnded):
            self._cooldown_ended(self._execution_id, event)
        elif isinstance(event, MediaChecked):
            self._media_checked(self._execution_id, event)
        elif isinstance(event, FirstImageDropped):
            self._executions.set_first_image(self._execution_id, None)
        elif isinstance(event, JobFinished):
            self._executions.finish(self._execution_id, status=event.status, exit_code=event.exit_code, signal=event.signal, finished_at=event.at)

    def _media_checked(self, execution_id: int, event: MediaChecked) -> None:
        try:
            self._executions.add_check(execution_id, MediaCheckRow(run=event.run, stage=event.stage, file=event.file, summary=event.summary, verdict=event.verdict, notes=event.notes, facts=event.facts, at=event.at))
        # Whatever breaks storing a check, the runs and the job's end are still recorded.
        except Exception:
            if not self._check_failed:
                self._check_failed = True
                logger.exception("Storing a media check failed; it is skipped (as is any other that fails), and the rest of this job is still recorded")

    def _job_started(self, event: JobStarted) -> None:
        # The number reserved before the job started; a job run without one takes the next when it is recorded.
        number = parse_typed_id(event.execution_id, EXECUTION_LETTER) if event.execution_id is not None else None
        resumes = parse_typed_id(event.resumes_execution, EXECUTION_LETTER) if event.resumes_execution is not None else None
        settings = ExecutionSettings(
            input=event.input,
            output_directory=event.output_directory,
            cooldown_seconds=event.cooldown.fixed_seconds,
            cooldown_source=event.cooldown_source,
            cooldown=event.cooldown.as_dict(),
            config_file=event.config_file,
            config_override=event.config_override,
            input_resize=event.input_resize,
        )
        self._execution_id = self._executions.start(
            NewExecution(
                execution_number=number,
                job_name=event.job_name,
                job_file=event.job_file,
                mode=event.mode,
                model=event.model,
                seed=event.seed,
                seed_source=event.seed_source,
                cooldown_seconds=event.cooldown.fixed_seconds,
                cooldown_source=event.cooldown_source,
                total_runs=event.total_runs,
                started_at=event.at,
                # Resolved, like the import's key, so a manifest recorded live is never imported again.
                manifest_path=str(Path(event.manifest).resolve()) if event.manifest is not None else None,
                log_path=event.log,
                config_file=event.config_file,
                job_yaml=event.source_text,
                settings=settings,
                first_run=event.first_run,
                resumes=resumes,
                first_image=event.first_image,
            )
        )
        if number is None:
            given = self._executions.number_of(self._execution_id)
            self._label = execution_id_text(given) if given is not None else None

    def _run_started(self, execution_id: int, event: RunStarted) -> None:
        self._last_run = event.number
        self._executions.start_run(execution_id, event.number, NewRun(pair=event.pair, positive=event.positive, negative=event.negative, input=event.input, resized_input=event.resized_input, output=event.output, last_frame=event.last_frame, command=event.command, started_at=event.at, anchor=event.anchor))

    def _run_finished(self, execution_id: int, event: RunFinished) -> None:
        self._executions.finish_run(execution_id, event.number, status=event.status, exit_code=event.exit_code, seconds=event.seconds, output=event.output, last_frame=event.last_frame, output_width=event.output_width, output_height=event.output_height, output_frames=event.output_frames, corrected_output=event.corrected_output)

    def _cooldown_ended(self, execution_id: int, event: CooldownEnded) -> None:
        if self._last_run is not None:
            self._executions.set_run_cooldown(execution_id, self._last_run, event.waited_seconds)
