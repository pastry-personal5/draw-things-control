"""Record a job's events in the state store as they arrive."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger

from draw_things_control.jobs.job_events import CooldownEnded, JobEvent, JobFinished, JobStarted, RunFinished, RunStarted
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id
from draw_things_control.state.store import StateError, Store


class ExecutionRecorder:
    """A job observer that writes one execution and its runs to the store.

    A failure is logged once and stops recording for this execution, so later events never update rows that
    were not created; the generation carries on.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._execution_id: int | None = None
        self._label: str | None = None
        self._last_run: int | None = None
        self._failed = False

    @property
    def execution_id(self) -> int | None:
        """The row of the execution being recorded, once JobStarted has created it; None before, or if that failed."""
        return None if self._failed else self._execution_id

    @property
    def execution_label(self) -> str | None:
        """The execution's ID as people see it (E0012), once it is recorded; None before, or once recording failed."""
        return None if self._failed or self._execution_id is None else self._label

    def reserve(self) -> str:
        """Reserve the execution's ID for JobService.run; raises StateError when the store cannot give one, so the job
        does not start."""
        try:
            self._label = execution_id_text(self._store.reserve_execution_number())
        except sqlite3.Error as error:
            raise StateError(f"Cannot give the execution an ID in the state database {self._store.path}: {error}") from error
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
            # The number reserved before the job started; a job run without one takes the next when it is recorded.
            number = parse_typed_id(event.execution_id, EXECUTION_LETTER) if event.execution_id is not None else None
            self._execution_id = self._store.start_execution(
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
                settings={
                    "input": event.input,
                    "output_directory": event.output_directory,
                    "cooldown_seconds": event.cooldown.fixed_seconds,
                    "cooldown_source": event.cooldown_source,
                    "cooldown": event.cooldown.as_dict(),
                    "config_file": event.config_file,
                    "config_override": event.config_override,
                    "input_resize": event.input_resize,
                },
            )
            if number is None:
                given = self._store.execution_number(self._execution_id)
                self._label = execution_id_text(given) if given is not None else None
        elif self._execution_id is None:
            return
        elif isinstance(event, RunStarted):
            self._last_run = event.number
            self._store.start_run(self._execution_id, event.number, pair=event.pair, positive=event.positive, negative=event.negative, input=event.input, resized_input=event.resized_input, output=event.output, last_frame=event.last_frame, command=list(event.command), started_at=event.at)
        elif isinstance(event, RunFinished):
            self._store.finish_run(self._execution_id, event.number, status=event.status, exit_code=event.exit_code, seconds=event.seconds, output=event.output, last_frame=event.last_frame, output_width=event.output_width, output_height=event.output_height, output_frames=event.output_frames)
        elif isinstance(event, CooldownEnded):
            if self._last_run is not None:
                self._store.set_run_cooldown(self._execution_id, self._last_run, event.waited_seconds)
        elif isinstance(event, JobFinished):
            self._store.finish_execution(self._execution_id, status=event.status, exit_code=event.exit_code, signal=event.signal, finished_at=event.at)
