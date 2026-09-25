"""Record a job's events in the state store as they arrive."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from draw_things_control.jobs.job_events import CooldownEnded, JobEvent, JobFinished, JobStarted, RunFinished, RunStarted
from draw_things_control.state.store import Store


class ExecutionRecorder:
    """A job observer that writes one execution and its runs to the store.

    A failure is logged once and stops recording for this execution, so later events never update rows that
    were not created; the generation carries on.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._execution_id: int | None = None
        self._last_run: int | None = None
        self._failed = False

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
            self._execution_id = self._store.start_execution(
                job_name=event.job_name,
                job_file=event.job_file,
                mode=event.mode,
                model=event.model,
                seed=event.seed,
                seed_source=event.seed_source,
                cooldown_seconds=event.cooldown_seconds,
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
                    "cooldown_seconds": event.cooldown_seconds,
                    "cooldown_source": event.cooldown_source,
                    "config_file": event.config_file,
                    "config_override": event.config_override,
                    "input_resize": event.input_resize,
                },
            )
        elif self._execution_id is None:
            return
        elif isinstance(event, RunStarted):
            self._last_run = event.number
            self._store.start_run(self._execution_id, event.number, pair=event.pair, positive=event.positive, negative=event.negative, input=event.input, resized_input=event.resized_input, output=event.output, last_frame=event.last_frame, command=list(event.command), started_at=event.at)
        elif isinstance(event, RunFinished):
            self._store.finish_run(self._execution_id, event.number, status=event.status, exit_code=event.exit_code, seconds=event.seconds, output=event.output, last_frame=event.last_frame)
        elif isinstance(event, CooldownEnded):
            if self._last_run is not None:
                self._store.set_run_cooldown(self._execution_id, self._last_run, event.waited_seconds)
        elif isinstance(event, JobFinished):
            self._store.finish_execution(self._execution_id, status=event.status, exit_code=event.exit_code, signal=event.signal, finished_at=event.at)
