"""Run a job on this machine with its execution recorded: the use case behind ``run-job``, the TUI, and a queue worker."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import JobObserver, combine_observers
from draw_things_control.jobs.executor import JobExecutor, JobOutcome, JobRunOptions
from draw_things_control.state.database import StateError
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import Store, StoreMode


class JobRunSession:
    """Runs jobs for one front end: takes the run lock, opens the state store, closes what a crash left running, and
    runs the job with the recorder watching it, so nothing else can start a run while this one records.

    Every step that can fail raises a DtcError before the job starts: BusyError (the run lock, or an earlier run's child),
    or StateUnavailableError (the lock file or the database). A run that cannot get an execution ID does not start.
    """

    def __init__(self, paths: ProjectPaths, executor: JobExecutor, settings: GlobalConfig) -> None:
        self._paths = paths
        self._executor = executor
        self._settings = settings

    def run(
        self,
        job: JobDefinition,
        *,
        holder: str,
        executable: str,
        shutdown_grace: float,
        observers: Sequence[JobObserver] = (),
        before_run: Callable[[Store, ExecutionRecorder], None] | None = None,
        lock: RunLock | None = None,
    ) -> JobOutcome:
        """Run ``job`` and record it. ``holder`` names the process in the lock file (``run-job``, ``tui``). ``observers`` see
        each event after the recorder. ``before_run`` is called with the store and the recorder once the lock is held and
        before the job starts. A ``lock`` the caller already holds is used, and left held; without one, this takes and
        releases its own."""
        own_lock = lock is None
        run_lock = lock if lock is not None else RunLock(holder, directory=self._paths.state)
        if own_lock:
            run_lock.acquire()
        try:
            store = self._open_store()
            try:
                return self._run(job, store, run_lock, executable, shutdown_grace, observers, before_run)
            finally:
                store.close()
        finally:
            if own_lock:
                run_lock.release()

    def _open_store(self) -> Store:
        try:
            return Store.open(self._paths.database, mode=StoreMode.RUN, retention_days=self._settings.history_retention_days)
        except sqlite3.Error as error:
            raise StateError(f"Cannot use the state database {self._paths.database}: {error}") from error

    def _run(self, job: JobDefinition, store: Store, lock: RunLock, executable: str, shutdown_grace: float, observers: Sequence[JobObserver], before_run: Callable[[Store, ExecutionRecorder], None] | None) -> JobOutcome:
        try:
            # Holding the lock proves no runner is alive, so any row still 'running' is a crash.
            store.sweep_interrupted()
        except sqlite3.Error as error:
            raise StateError(f"Cannot use the state database {store.path}: {error}") from error
        # The recorder comes first, so its row exists before any other observer sees JobStarted.
        recorder = ExecutionRecorder(store)
        if before_run is not None:
            before_run(store, recorder)
        options = JobRunOptions(
            executable=executable,
            shutdown_grace=shutdown_grace,
            write_records=self._settings.write_job_records,
            observer=combine_observers(recorder, *observers),
            on_child_start=lock.record_child,
            # The execution's ID is reserved before the job starts; a job that cannot get one does not start.
            reserve_execution_id=recorder.reserve,
        )
        return self._executor.run(job, options)
