"""The queue worker: claims the oldest queued entry, runs it through ``JobRunSession``, and waits the cooldown between
queued jobs. One worker thread; ``claim_and_run_one`` is the synchronous step a test drives directly without one."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from loguru import logger

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.run_lock import SERVER_HOLDER_NAME, RunLock
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import JobEvent, JobFinished, JobObserver, JobStarted, JobStatus
from draw_things_control.jobs.executor import JobExecutor, ResumePoint
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.services.queue_submit import parse_snapshot
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id
from draw_things_control.state.queue import QueueRow, QueueState
from draw_things_control.state.store import Store

# What the worker itself asked for, so a job that ends 'interrupted' knows whether that means cancelled or the server
# stopping; a run stopped from outside neither of these and is not a normal failure either (see _final_state).
_CANCEL, _SHUTDOWN = "cancel", "shutdown"

_FINAL_QUEUE_STATE = {JobStatus.SUCCEEDED: QueueState.SUCCEEDED, JobStatus.FAILED: QueueState.FAILED}

# Waits ``seconds`` between queued jobs; the default is the worker's own interruptible poll (``_poll_wait``), and a
# test injects a spy in its place, as ``JobExecutor``'s own ``Cooldown`` is faked in its tests.
WaitBetweenJobs = Callable[[float], None]


class QueueWorker:
    """Runs queued jobs one after another off its own thread, while ``lock`` and ``store`` are held for the host's
    whole lifetime."""

    def __init__(self, store: Store, session: JobRunSession, executor: JobExecutor, lock: RunLock, paths: ProjectPaths, global_config: GlobalConfig, *, executable: str, shutdown_grace: float, clock: Clock = datetime.now, poll_interval: float = 0.02, wait_between_jobs: WaitBetweenJobs | None = None) -> None:
        self._store = store
        self._session = session
        self._executor = executor
        self._lock = lock
        self._paths = paths
        self._global_config = global_config
        self._executable = executable
        self._shutdown_grace = shutdown_grace
        self._clock = clock
        self._poll_interval = poll_interval
        self._wait_between_jobs = wait_between_jobs or self._poll_wait
        self._state_lock = threading.Lock()
        self._current: QueueRow | None = None
        self._stop_reason: str | None = None
        self._pending_cancel = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="queue-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to end: cancel the running job at once (or, if it is still between the claim and the
        executor's ``begin()``, keep the cancel pending for ``_cancel_guard`` to apply at ``JobStarted``), end any
        between-jobs wait, and wait for the thread."""
        self._stop_event.set()
        with self._state_lock:
            # Whichever stop reason lands first wins: a shutdown that lands after a cancel already asked for one
            # must not relabel it, and the reverse (a cancel of an entry already stopping) is already a no-op.
            if self._current is not None and self._stop_reason is None:
                self._stop_reason = _SHUTDOWN
                self._pending_cancel = True
        self._executor.cancel()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join()

    def wake(self) -> None:
        """Wake an idle worker, or end its between-jobs wait early: a submission, or a cancel of the queued entry it
        is waiting for. A no-op when the worker is not waiting."""
        self._wake_event.set()

    def cancel_running(self, entry_id: int) -> bool:
        """Cancel the entry if it is the one currently claimed; False when it is not (queued, finished, or another
        entry entirely). Idempotent: a second call on an entry already stopping does nothing new."""
        with self._state_lock:
            if self._current is None or self._current.id != entry_id:
                return False
            if self._stop_reason is None:
                self._stop_reason = _CANCEL
            self._pending_cancel = True
        self._executor.cancel()
        return True

    def current_entry_id(self) -> int | None:
        with self._state_lock:
            return self._current.id if self._current is not None else None

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            if not self.claim_and_run_one():
                self._idle_wait()

    def claim_and_run_one(self) -> bool:
        """Claim the oldest queued entry and run it to completion, then wait its cooldown if another entry is
        already queued. False, doing nothing, when there is none. Never raises: an error that escapes the job (or
        the store itself) fails only its entry (or is logged and skipped), and the worker goes on."""
        try:
            return self._claim_and_run_one()
        except Exception:
            logger.exception("Queue worker: claim_and_run_one failed unexpectedly")
            # False, so run_forever backs off (the idle wait) instead of retrying a persistent error at once.
            return False

    def _claim_and_run_one(self) -> bool:
        # The claim (a DB write marking the entry 'running') and registering it as self._current happen under the
        # same lock: cancel_running also takes this lock, so it never observes the DB already reading 'running'
        # while self._current is still the previous (or no) entry, which would make its cancel a silent no-op.
        with self._state_lock:
            entry = self._store.queue.claim_oldest(self._clock())
            if entry is None:
                return False
            if self._stop_event.is_set():
                # Claimed right as shutdown began: nothing of it will run, so it goes back to queued, matching
                # recovery's own rule for a crash between the claim and JobStarted.
                self._store.queue.requeue(entry.id)
                return True
            self._current, self._stop_reason, self._pending_cancel = entry, None, False
        job: JobDefinition | None = None
        try:
            job = self._run_claimed(entry)
        finally:
            with self._state_lock:
                self._current = None
        if job is not None:
            self._wait_after(entry, job)
        return True

    def _run_claimed(self, entry: QueueRow) -> JobDefinition | None:
        # Set once JobFinished has already marked the entry, so an exception the executor re-raises after that (it
        # always re-raises what it caught) does not overwrite a real outcome with a spurious 'failed'.
        finished = [False]
        try:
            job = parse_snapshot(entry, self._global_config, self._paths.params)()
            resume = self._resume_point(entry)
        except Exception as error:
            self._fail_to_start(entry, error)
            return None
        try:
            self._session.run(
                job,
                holder=SERVER_HOLDER_NAME,
                executable=self._executable,
                shutdown_grace=self._shutdown_grace,
                observers=(self._cancel_guard, self._make_finisher(entry, finished)),
                lock=self._lock,
                resume=resume,
                on_reserved=self._linker(entry),
            )
        except Exception as error:
            if finished[0]:
                # JobFinished already recorded the entry's real outcome (failed, or interrupted turned cancelled);
                # this exception is what caused it, not a second failure.
                self._store.queue.set_error(entry.id, str(error))
            else:
                self._fail_to_start(entry, error)
        return job

    @staticmethod
    def _resume_point(entry: QueueRow) -> ResumePoint | None:
        if entry.resume_first_run is None:
            return None
        assert entry.resume_input is not None and entry.resume_seed is not None
        resumes_execution = execution_id_text(entry.resumes_execution) if entry.resumes_execution is not None else None
        return ResumePoint(first_run=entry.resume_first_run, input=Path(entry.resume_input), seed=entry.resume_seed, resumes_execution=resumes_execution)

    def _linker(self, entry: QueueRow) -> Callable[[str], None]:
        def on_reserved(label: str) -> None:
            number = parse_typed_id(label, EXECUTION_LETTER)
            if number is not None:
                self._store.queue.link_execution(entry.id, number)

        return on_reserved

    def _cancel_guard(self, event: JobEvent) -> None:
        """A cancel that landed between the claim and the executor's ``begin()`` is kept and applied here, at
        ``JobStarted``, as the TUI does for a stop requested during start-up. Covers both a targeted cancel and a
        shutdown that arrived in that same window."""
        if isinstance(event, JobStarted):
            with self._state_lock:
                pending = self._pending_cancel
            if pending:
                self._executor.cancel()

    def _make_finisher(self, entry: QueueRow, finished: list[bool]) -> JobObserver:
        def observe(event: JobEvent) -> None:
            if isinstance(event, JobFinished):
                finished[0] = True
                state = self._final_state(event)
                self._store.queue.finish(entry.id, state=state, finished_at=event.at)

        return observe

    def _final_state(self, event: JobFinished) -> QueueState:
        known = _FINAL_QUEUE_STATE.get(event.status)
        if known is not None:
            return known
        # JobStatus.INTERRUPTED: only ever reaches here because we asked JobExecutor.cancel() for it (a run killed
        # from outside instead produces a plain JobStatus.FAILED, at the executor level, never this branch).
        with self._state_lock:
            reason = self._stop_reason
        if reason == _CANCEL:
            return QueueState.CANCELLED
        if reason == _SHUTDOWN:
            return QueueState.INTERRUPTED
        return QueueState.FAILED

    def _fail_to_start(self, entry: QueueRow, error: Exception) -> None:
        """Only reached when JobFinished never fired, so the execution row (if a number was ever reserved for it) was
        never created: clears the link, so a later resume sees 'never ran' rather than mistaking the dangling number
        for one that ran and was pruned."""
        logger.exception("Queue entry {} failed to start", entry.label)
        self._store.queue.finish(entry.id, state=QueueState.FAILED, finished_at=local_timestamp(self._clock()), error=str(error), clear_link=True)

    def _wait_after(self, entry: QueueRow, job: JobDefinition) -> None:
        """The cooldown between queued jobs: after a job that succeeded, when another entry is already queued.
        Reuses ``job`` (already parsed and validated by ``_run_claimed``) instead of parsing the snapshot again,
        which would re-check the job's own input file and could raise if it was since removed."""
        if self._stop_event.is_set():
            return
        updated = self._store.queue.get(entry.id)
        if updated is None or updated.state != QueueState.SUCCEEDED or not self._store.queue.has_queued():
            return
        wait = job.cooldown.wait_after(self._last_run_seconds(updated))
        if wait.seconds > 0:
            self._wait_between_jobs(wait.seconds)

    def _last_run_seconds(self, entry: QueueRow) -> float:
        if entry.execution_number is None:
            return 0.0
        execution = self._store.executions.by_number(entry.execution_number)
        return (execution.runs[-1].seconds or 0.0) if execution is not None and execution.runs else 0.0

    def _poll_wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop_event.is_set() or not self._store.queue.has_queued():
                return
            self._wake_event.wait(timeout=min(self._poll_interval, max(0.0, deadline - time.monotonic())))
            self._wake_event.clear()

    def _idle_wait(self) -> None:
        # No entry queued: wait to be woken by a submission or shutdown, checking occasionally in case a wake is missed.
        self._wake_event.wait(timeout=1.0)
        self._wake_event.clear()
