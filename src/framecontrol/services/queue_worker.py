"""The queue worker: claims the oldest queued entry, runs it through ``JobRunSession``, and waits the cooldown between
queued jobs. One worker thread; ``claim_and_run_one`` is the synchronous step a test drives directly without one.
It keeps a running entry's park reservation, and the queue's hold (``QueueHold``)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from loguru import logger

from framecontrol.core.arguments import redact_command
from framecontrol.core.backend_ids import H3_BACKEND
from framecontrol.core.clock import Clock, local_timestamp
from framecontrol.core.global_config import GlobalConfig
from framecontrol.core.paths import ProjectPaths
from framecontrol.core.run_lock import SERVER_HOLDER_NAME, RunLock
from framecontrol.jobs.backend import BackendRegistry
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.events import JobEvent, JobFinished, JobObserver, JobStarted, JobStatus, RunStatus
from framecontrol.jobs.executor import JobExecutor, ResumePoint
from framecontrol.jobs.timing.forecast import Forecast, Forecaster
from framecontrol.services.forecast_publisher import ForecastPublisher, job_plan
from framecontrol.services.generation_submit import GenerationSnapshot
from framecontrol.services.history_delete import DeleteReport
from framecontrol.services.job_runs import JobRunSession
from framecontrol.services.queue_claim_gate import QueueClaimGate
from framecontrol.services.queue_events import EventSink, QueueEventPublisher
from framecontrol.services.queue_hold import HoldState, QueueHold
from framecontrol.services.queue_submit import parse_snapshot
from framecontrol.services.queue_worker_status import WorkerStatus
from framecontrol.services.timing_history import TimingHistory
from framecontrol.state.executions import ExecutionSettings, NewExecution, NewRun
from framecontrol.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id
from framecontrol.state.queue import QueueRow, QueueState
from framecontrol.state.store import Store

# What the worker itself asked for, so a job that ends 'interrupted' knows whether that means cancelled or the server
# stopping; a run stopped from outside neither of these and is not a normal failure either (see _final_state).
STOP_CANCEL, STOP_SHUTDOWN = "cancel", "shutdown"

_FINAL_QUEUE_STATE = {JobStatus.SUCCEEDED: QueueState.SUCCEEDED, JobStatus.FAILED: QueueState.FAILED, JobStatus.PARKED: QueueState.PARKED}
# The finished states after which the next queued entry waits the between-jobs cooldown: a parked job's last run succeeded.
_COOLDOWN_AFTER = (QueueState.SUCCEEDED, QueueState.PARKED)


def _resume_point_of(entry: QueueRow, store: Store | None = None) -> ResumePoint | None:
    if entry.resume_first_run is None:
        return None
    assert entry.resume_input is not None and entry.resume_seed is not None
    resumes_execution = execution_id_text(entry.resumes_execution) if entry.resumes_execution is not None else None
    first_image = Path(entry.resume_first_image) if entry.resume_first_image is not None else None
    anchor = Path(entry.resume_anchor) if entry.resume_anchor is not None else None
    fingerprint = None
    if store is not None and entry.resumes_execution is not None:
        previous = store.executions.by_number(entry.resumes_execution)
        if previous is not None and previous.settings.backend_settings is not None:
            installation = previous.settings.backend_settings.get("h3_installation")
            if isinstance(installation, dict) and isinstance(installation.get("executable_sha256"), str):
                fingerprint = installation["executable_sha256"]
    return ResumePoint(first_run=entry.resume_first_run, input=Path(entry.resume_input), seed=entry.resume_seed, resumes_execution=resumes_execution, first_image=first_image, anchor=anchor, backend_fingerprint=fingerprint)


# Waits ``seconds`` between queued jobs; the default is the worker's own interruptible poll (``_poll_wait``), and a
# test injects a spy in its place, as ``JobExecutor``'s own ``Cooldown`` is faked in its tests.
WaitBetweenJobs = Callable[[float], None]


class QueueWorker:
    """Runs queued jobs one after another off its own thread, while ``lock`` and ``store`` are held for the host's whole lifetime."""

    def __init__(self, store: Store, session: JobRunSession, executor: JobExecutor, lock: RunLock, paths: ProjectPaths, global_config: GlobalConfig, *, executable: str, shutdown_grace: float, clock: Clock = datetime.now, poll_interval: float = 0.02, wait_between_jobs: WaitBetweenJobs | None = None, on_event: EventSink | None = None, backends: BackendRegistry = DEFAULT_BACKENDS) -> None:
        self._store = store
        self._session = session
        self._executor = executor
        self._lock = lock
        self._paths = paths
        self._global_config = global_config
        self._backends = backends
        self._executable = executable
        self._shutdown_grace = shutdown_grace
        self._clock = clock
        self._poll_interval = poll_interval
        self._wait_between_jobs = wait_between_jobs or self._poll_wait
        self._events = QueueEventPublisher(on_event)
        self._state_lock = threading.Lock()
        self._claim_gate = QueueClaimGate(self._state_lock, store, self._events, clock, self.wake)
        self._current: QueueRow | None = None
        self._stop_reason: str | None = None
        self._pending_cancel = False
        # The claimed entry's park reservation, kept in memory only: stopping the server stops its run anyway. Applied to
        # the executor only once JobStarted has been seen (_job_started), so that before then an unpark withdraws it here
        # alone. _job_finished is set, under the same lock, as the entry is marked finished: from then on it is not
        # running, so a park that loses the race with the job's own end is refused rather than holding the queue.
        self._park_pending = False
        self._job_started = False
        self._job_finished = False
        # Set by a release that lands once the claimed job has ended: the between-jobs cooldown before the next claim is
        # dropped (release). Cleared by the claim.
        self._release_skips_wait = False
        self._hold = QueueHold(store.settings, self._events, clock=clock)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = WorkerStatus(clock=clock)
        # The forecast of the entry being run: its history, its publisher, and the latest one published. Its own lock,
        # since a forecast is published while the state lock is held (a park) and while it is not (a job event).
        self._history = TimingHistory(store)
        self._publisher: ForecastPublisher | None = None
        self._forecast: Forecast | None = None
        self._forecast_entry: int | None = None
        self._forecast_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="queue-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to end: cancel the running job at once (or, if it is still between the claim and the executor's ``begin()``, keep the cancel pending for ``_start_guard`` to apply at ``JobStarted``), end any between-jobs wait, and wait for the thread."""
        self._stop_event.set()
        with self._state_lock:
            # Whichever stop reason lands first wins: a shutdown that lands after a cancel already asked for one
            # must not relabel it, and the reverse (a cancel of an entry already stopping) is already a no-op.
            if self._current is not None and self._stop_reason is None:
                self._stop_reason = STOP_SHUTDOWN
                self._pending_cancel = True
        self._executor.cancel()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join()

    def wake(self) -> None:
        """Wake an idle worker, or end its between-jobs wait early: a submission, or a cancel of the queued entry it is waiting for. A no-op when the worker is not waiting."""
        self._wake_event.set()

    def enqueue(self, insert: Callable[[], QueueRow]) -> QueueRow:
        """``QueueClaimGate.enqueue``, sharing this worker's own claim lock (``queue_claim_gate.py``): a claim already waiting on it can never see the row, and so never publish 'running', before this publishes 'queued'."""
        return self._claim_gate.enqueue(insert)

    def cancel_queued(self, entry_id: int, label: str) -> bool:
        """``QueueClaimGate.cancel_queued``, sharing this worker's own claim lock."""
        return self._claim_gate.cancel_queued(entry_id, label)

    def delete_executions(self, numbers: Sequence[int], *, dry_run: bool = False) -> DeleteReport:
        """``QueueClaimGate.delete_executions``, sharing this worker's own claim lock."""
        return self._claim_gate.delete_executions(numbers, dry_run=dry_run)

    def cancel_running(self, entry_id: int) -> bool:
        """Cancel the entry if it is the one currently claimed; False when it is not (queued, finished, or another entry entirely). Idempotent: a second call on an entry already stopping does nothing new."""
        with self._state_lock:
            if self._current is None or self._current.id != entry_id:
                return False
            if self._stop_reason is None:
                self._stop_reason = STOP_CANCEL
            self._pending_cancel = True
        self._executor.cancel()
        self._cancel_forecast()
        return True

    def current_entry_id(self) -> int | None:
        with self._state_lock:
            return self._current.id if self._current is not None else None

    def park_running(self, entry_id: int, label: str, caller: str | None = None) -> bool:
        """Make a park reservation on the running entry, and hold the queue by it, for ``caller``, unless the queue is
        already held; on a held queue, a person's park makes an agent's hold the person's. False, doing nothing, when the entry is not the running one (queued, finished, or another entry), or a stop
        (a cancel, or the server stopping) is already asked for it: ``stop_reason`` tells which. On an entry already
        parking, it only holds the queue again, when a release has ended the hold. Made before ``JobStarted``, the
        reservation stays pending and is applied there (``_start_guard``)."""
        with self._state_lock:
            if not self._is_running(entry_id) or self._stop_reason is not None:
                return False
            # The hold first: one that cannot be saved raises before any of the park is made, so a park never runs
            # without the hold that keeps the next entry from starting after it. On a queue already held, a person's
            # park makes an agent's hold the person's (QueueHold._held_again), so the agent cannot release it.
            self._hold.hold(label, caller)
            if not self._park_pending:
                self._park_pending = True
                if self._job_started:
                    self._executor.park()
                self._events.park_changed(label, True)
                self._forecast_park(True)
        return True

    def unpark_running(self, entry_id: int, label: str, caller: str | None = None) -> bool:
        """Withdraw the running entry's park reservation, releasing the hold only when that reservation made it; a no-op
        when it has none. False when the entry is not the running one, or its park has already taken effect. Refused
        (``NotPermittedError``), with the reservation and its hold standing, for an agent ``caller`` when a person made
        that hold."""
        with self._state_lock:
            if not self._is_running(entry_id):
                return False
            if not self._park_pending:
                return True
            # The hold is released inside the executor's unpark, so no run boundary passes between the two: a hold that
            # cannot be released raises with the reservation, and the hold it made, standing as they were.
            if self._job_started:
                if not self._executor.unpark(lambda: self._hold.release_if_by(label, caller)):
                    return False
            else:
                self._hold.release_if_by(label, caller)
            self._park_pending = False
            self._events.park_changed(label, False)
            self._forecast_park(False)
        return True

    def parking_entry_id(self) -> int | None:
        """The running entry's id when it has a park reservation, else None: one lock for a whole page (``GET /queue``)."""
        with self._state_lock:
            return self._current.id if self._current is not None and not self._job_finished and self._park_pending else None

    def park_requested(self, entry_id: int) -> bool:
        """Whether the running entry ``entry_id`` has a park reservation (``GET /queue``, ``WatchQueueEntry``)."""
        return self.parking_entry_id() == entry_id

    def stop_reason(self, entry_id: int) -> str | None:
        """``STOP_CANCEL`` or ``STOP_SHUTDOWN`` when a stop is asked for the running entry ``entry_id``; None otherwise."""
        with self._state_lock:
            return self._stop_reason if self._is_running(entry_id) else None

    def hold(self, caller: str | None = None) -> tuple[bool, HoldState]:
        """Hold the queue directly (``/queue hold``) for ``caller``: a running job is not stopped, and nothing starts after
        it. Ends a between-jobs wait at once. Returns whether the queue was not held before, and the hold, both read
        under the same lock as the change."""
        with self._state_lock:
            changed = self._hold.hold(None, caller)
            hold = self._hold.snapshot()
        self.wake()
        return changed, hold

    def release(self, caller: str | None = None) -> tuple[bool, HoldState]:
        """End the hold for ``caller``; the oldest queued entry is claimed at once, with no between-jobs cooldown. Returns
        whether the queue was held (False, doing nothing, when it was not), and the hold, both read under the same lock
        as the change. Refused (``NotPermittedError``) for an agent when a person made the hold."""
        with self._state_lock:
            released = self._hold.release(caller)
            # Once the claimed job has ended (or with none claimed), the next claim skips the between-jobs cooldown, even
            # when this lands before _wait_after reads the hold; a release while the job still runs leaves it to wait.
            if released and (self._current is None or self._job_finished):
                self._release_skips_wait = True
            hold = self._hold.snapshot()
        self.wake()
        return released, hold

    def hold_state(self) -> HoldState:
        return self._hold.snapshot()

    def _is_running(self, entry_id: int) -> bool:
        """Call with ``_state_lock`` held: ``entry_id`` is claimed and not yet marked finished."""
        return self._current is not None and self._current.id == entry_id and not self._job_finished

    def is_alive(self) -> bool:
        """False once the thread has stopped (an error escaping ``run_forever`` itself, not a single job's failure), for ``GET /health``: the queue processes nothing more until the server restarts."""
        return self._thread is not None and self._thread.is_alive()

    def state(self) -> str:
        """``running``, ``cooling_down``, ``idle`` (``GET /queue``), or ``held``: not running a job, and the
        queue is held. A job running under a hold still reads ``running``."""
        state = self._status.state()
        return "held" if state != "running" and self._hold.is_held else state

    def cooldown_until(self) -> float | None:
        """When the between-jobs wait ends (an epoch), or None outside it."""
        return self._status.cooldown_until()

    def current_run(self) -> tuple[int, float] | None:
        """The claimed entry's current run number and its elapsed seconds (``GET /queue/{id}``, ``WatchQueueEntry``)."""
        return self._status.current_run()

    def latest_forecast(self, entry_id: int) -> Forecast | None:
        """The forecast last published for entry ``entry_id`` while it is the one running; None for any other entry, and
        once it has been released, cancelled, or has finished (``GET /queue/{id}``, ``WatchQueueEntry``)."""
        with self._forecast_lock:
            return self._forecast if self._forecast_entry == entry_id else None

    def between_runs_after(self) -> int | None:
        """The succeeded run the claimed entry is between runs after, until its next run starts (``GET /queue/{id}``)."""
        return self._status.between_runs_after()

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            if not self.claim_and_run_one():
                self._idle_wait()

    def claim_and_run_one(self) -> bool:
        """Claim the oldest queued entry and run it to completion, then wait its cooldown if another entry is already queued. False, doing nothing, when there is none or the queue is held. Never raises: an error that escapes the job (or the store itself) fails only its entry (or is logged and skipped), and the worker goes on."""
        try:
            return self._claim_and_run_one()
        except Exception:
            logger.exception("Queue worker: claim_and_run_one failed unexpectedly")
            # False, so run_forever backs off (the idle wait) instead of retrying a persistent error at once.
            return False

    def _claim_and_run_one(self) -> bool:
        # The claim (a DB write marking the entry 'running') and registering it as self._current happen under the same lock: cancel_running also takes this lock, so it never observes the DB already reading 'running' while self._current is still the previous (or no) entry, which would make its cancel a silent no-op.
        with self._state_lock:
            # Checked under the lock, so a hold and a claim never interleave.
            if self._hold.is_held:
                return False
            entry = self._store.queue.claim_oldest(self._clock())
            if entry is None:
                return False
            if self._stop_event.is_set():
                # Claimed right as shutdown began: nothing of it will run, so it goes back to queued, matching
                # recovery's own rule for a crash between the claim and JobStarted.
                self._store.queue.requeue(entry.id)
                self._events.entry_changed(entry.label, str(QueueState.QUEUED))
                return True
            self._current, self._stop_reason, self._pending_cancel = entry, None, False
            self._park_pending = self._job_started = self._job_finished = self._release_skips_wait = False
            self._status.entry_claimed()
            # Published under the lock, as park_changed and held are: a park that lands right after the claim then never
            # reaches a front end before the 'running' it reads as this entry's claim (tui/feed.py's pending_queue_id).
            self._events.entry_changed(entry.label, entry.state)
        job: JobDefinition | None = None
        try:
            job = self._run_claimed(entry)
        finally:
            with self._state_lock:
                self._current = None
            self._cancel_forecast()
            self._status.entry_released()
        if job is not None:
            self._wait_after(entry, job)
        return True

    def _run_claimed(self, entry: QueueRow) -> JobDefinition | None:
        if entry.kind == "generate":
            self._run_generation(entry)
            return None
        # Set once JobFinished has already marked the entry, so an exception the executor re-raises after that (it
        # always re-raises what it caught) does not overwrite a real outcome with a spurious 'failed'.
        finished = [False]
        try:
            job = parse_snapshot(entry, self._global_config, self._paths.params, allow_missing_input=entry.backend == H3_BACKEND and (entry.resume_first_run or 1) > 1, backends=self._backends)()
            resume = _resume_point_of(entry, self._store)
        except Exception as error:
            self._fail_to_start(entry, error)
            return None
        forecast = self._start_forecast(entry, job, resume)
        try:
            self._session.run(
                job,
                holder=SERVER_HOLDER_NAME,
                executable=self._executable,
                shutdown_grace=self._shutdown_grace,
                observers=(self._start_guard, self._status.observe_run, forecast, self._events.job_event, self._make_finisher(entry, finished)),
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

    def _run_generation(self, entry: QueueRow) -> None:
        """Run and record exactly one snapshot, sharing the worker's executor, lock, cancellation, and queue state."""
        execution_id: int | None = None
        try:
            snapshot = GenerationSnapshot.from_json(entry.snapshot)
            arguments = snapshot.arguments(self._executable)
            number = self._store.executions.reserve_number()
            self._store.queue.link_execution(entry.id, number)
            started_at = local_timestamp(self._clock())
            execution_id = self._store.executions.start(NewExecution(execution_number=number, job_name=snapshot.display_name, job_file="", mode="i2v" if Path(snapshot.output).suffix.lower() in {".mov", ".mp4"} else "i2i", model=snapshot.model, seed=snapshot.seed, seed_source="given" if snapshot.seed is not None else None, total_runs=1, started_at=started_at, config_file=snapshot.config_file, job_yaml=entry.snapshot, settings=ExecutionSettings(input=snapshot.image[0] if snapshot.image else None, output_directory=entry.output_directory, config_file=snapshot.config_file), source_kind="generate"))
            self._store.executions.start_run(execution_id, 1, NewRun(pair="generate", positive=snapshot.prompt or "", negative=snapshot.negative_prompt, input=snapshot.image[0] if snapshot.image else None, output=snapshot.output_path, command=redact_command(arguments.command), started_at=started_at))
            self._status.generation_started()
            outcome = self._executor.run_generation(arguments, timeout=snapshot.timeout, shutdown_grace=self._shutdown_grace, on_begin=self._generation_start_guard, on_child_start=self._lock.record_child)
            output_exists = Path(snapshot.output_path).is_file()
            status = RunStatus.SUCCEEDED if outcome.exit_code == 0 and not outcome.timed_out and outcome.termination_signal is None and output_exists else RunStatus.TIMED_OUT if outcome.timed_out else RunStatus.INTERRUPTED if outcome.termination_signal is not None else RunStatus.FAILED
            exit_code = outcome.exit_code if output_exists or outcome.exit_code else 1
            finished_at = local_timestamp(self._clock())
            self._store.executions.finish_run(execution_id, 1, status=str(status), exit_code=exit_code, seconds=(self._clock().timestamp() - datetime.fromisoformat(started_at).timestamp()), output=snapshot.output_path, last_frame=None)
            execution_status = JobStatus.SUCCEEDED if status == RunStatus.SUCCEEDED else JobStatus.INTERRUPTED if status == RunStatus.INTERRUPTED else JobStatus.FAILED
            self._store.executions.finish(execution_id, status=str(execution_status), exit_code=exit_code, signal=outcome.termination_signal.name if outcome.termination_signal is not None else None, finished_at=finished_at)
            if execution_status == JobStatus.INTERRUPTED:
                state = self._final_state(JobFinished(at=finished_at, status=execution_status, exit_code=exit_code, completed_runs=0, total_runs=1, signal=outcome.termination_signal.name if outcome.termination_signal is not None else None))
            else:
                state = QueueState.SUCCEEDED if execution_status == JobStatus.SUCCEEDED else QueueState.FAILED
            with self._state_lock:
                self._store.queue.finish(entry.id, state=state, finished_at=finished_at)
                self._job_finished = True
            self._events.entry_changed(entry.label, str(state))
        except Exception as error:
            if execution_id is None:
                self._fail_to_start(entry, error)
            else:
                finished_at = local_timestamp(self._clock())
                self._store.executions.finish_run(execution_id, 1, status=str(RunStatus.FAILED), exit_code=None, seconds=None, output=None, last_frame=None)
                self._store.executions.finish(execution_id, status=str(JobStatus.FAILED), exit_code=None, signal=None, finished_at=finished_at)
                with self._state_lock:
                    self._store.queue.finish(entry.id, state=QueueState.FAILED, finished_at=finished_at, error=str(error))
                    self._job_finished = True
                self._events.entry_changed(entry.label, str(QueueState.FAILED))
        finally:
            self._status.generation_finished()

    def _generation_start_guard(self) -> None:
        """Apply a cancel which reached a claimed one-off before its cancel token began.

        ``JobExecutor.run_generation`` invokes this immediately after beginning that token.  A cancellation which
        lands before then is therefore still delivered to the runner when it attaches, matching the job path's
        ``JobStarted`` guard.
        """
        with self._state_lock:
            self._job_started = True
            pending = self._pending_cancel
        if pending:
            self._executor.cancel()

    def _linker(self, entry: QueueRow) -> Callable[[str], None]:
        def on_reserved(label: str) -> None:
            number = parse_typed_id(label, EXECUTION_LETTER)
            if number is not None:
                self._store.queue.link_execution(entry.id, number)

        return on_reserved

    def _start_guard(self, event: JobEvent) -> None:
        """A cancel (or shutdown) that landed between the claim and the executor's ``begin()`` is kept and applied
        here, at ``JobStarted``, as the TUI does for a stop requested during start-up. So is a park reservation, under
        the lock, since unlike a cancel it can be withdrawn: an unpark can never land between the read and the call."""
        if isinstance(event, JobStarted):
            with self._state_lock:
                self._job_started = True
                pending = self._pending_cancel
                if self._park_pending:
                    self._executor.park()
                    self._forecast_park(True)
            if pending:
                self._executor.cancel()

    def _start_forecast(self, entry: QueueRow, job: JobDefinition, resume: ResumePoint | None) -> JobObserver:
        """Build the entry's forecaster, after its job is parsed and before the executor starts, so the fit (up to 200 ms)
        delays nothing already running and run 1 has a forecast at RunStarted. The observer that feeds it; one that does
        nothing when the job cannot be described, since a forecast must never stop a job."""
        try:
            plan = job_plan(self._executor, job, first_run=resume.first_run if resume is not None else 1, executable=self._executable)
            publisher = ForecastPublisher(Forecaster(self._history.model(), plan), lambda forecast: self._publish_forecast(entry, forecast), clock=lambda: self._clock().timestamp())
        except Exception:
            logger.exception("Queue entry {} has no forecast: its runs could not be described", entry.label)
            return lambda event: None
        with self._forecast_lock:
            self._publisher = publisher
        return publisher.observe

    def _publish_forecast(self, entry: QueueRow, forecast: Forecast | None) -> None:
        """Hold the forecast the publisher made, and tell the event stream."""
        with self._forecast_lock:
            self._forecast, self._forecast_entry = forecast, entry.id if forecast is not None else None
        self._events.forecast_updated(entry.label, forecast.as_dict() if forecast is not None else None)

    def _forecast_park(self, parking: bool) -> None:
        with self._forecast_lock:
            publisher = self._publisher
        if publisher is not None:
            publisher.park_changed(parking)

    def _cancel_forecast(self) -> None:
        """A cancel was accepted, the entry was released, or the job ended: no forecast stands."""
        with self._forecast_lock:
            publisher, self._publisher = self._publisher, None
        if publisher is not None:
            publisher.cancel()

    def _make_finisher(self, entry: QueueRow, finished: list[bool]) -> JobObserver:
        def observe(event: JobEvent) -> None:
            if isinstance(event, JobFinished):
                finished[0] = True
                # Read before the lock is taken: _final_state takes it itself.
                state = self._final_state(event)
                with self._state_lock:
                    self._store.queue.finish(entry.id, state=state, finished_at=event.at)
                    self._job_finished = True
                self._events.entry_changed(entry.label, str(state))

        return observe

    def _final_state(self, event: JobFinished) -> QueueState:
        known = _FINAL_QUEUE_STATE.get(event.status)
        if known is not None:
            return known
        # JobStatus.INTERRUPTED: only ever reaches here because we asked JobExecutor.cancel() for it (a run killed
        # from outside instead produces a plain JobStatus.FAILED, at the executor level, never this branch).
        with self._state_lock:
            reason = self._stop_reason
        if reason == STOP_CANCEL:
            return QueueState.CANCELLED
        if reason == STOP_SHUTDOWN:
            return QueueState.INTERRUPTED
        return QueueState.FAILED

    def _fail_to_start(self, entry: QueueRow, error: Exception) -> None:
        """Only reached when JobFinished never fired, so the execution row (if a number was ever reserved for it) was
        never created: clears the link, so a later resume sees 'never ran' rather than mistaking the dangling number
        for one that ran and was pruned."""
        logger.exception("Queue entry {} failed to start", entry.label)
        with self._state_lock:
            self._store.queue.finish(entry.id, state=QueueState.FAILED, finished_at=local_timestamp(self._clock()), error=str(error), clear_link=True)
            self._job_finished = True
        self._events.entry_changed(entry.label, str(QueueState.FAILED))

    def _wait_after(self, entry: QueueRow, job: JobDefinition) -> None:
        """The cooldown between queued jobs: after a job that succeeded or parked, when another entry is already queued
        and the queue is not held (a release then claims at once, with no cooldown to wait out). Reuses ``job``
        (already parsed and validated by ``_run_claimed``) instead of parsing the snapshot again, which would re-check
        the job's own input file and could raise if it was since removed."""
        if self._stop_event.is_set() or self._hold.is_held or self._release_skips_wait:
            return
        updated = self._store.queue.get(entry.id)
        if updated is None or updated.state not in _COOLDOWN_AFTER or not self._store.queue.has_queued():
            return
        wait = job.cooldown.wait_after(self._last_run_seconds(updated))
        if wait.seconds > 0:
            self._status.cooldown_started(wait.seconds)
            cooldown_until = self._status.cooldown_until()
            assert cooldown_until is not None
            self._events.wait_started(entry.label, cooldown_until)
            try:
                self._wait_between_jobs(wait.seconds)
            finally:
                self._status.cooldown_ended()
                self._events.wait_ended(entry.label)

    def _last_run_seconds(self, entry: QueueRow) -> float:
        if entry.execution_number is None:
            return 0.0
        execution = self._store.executions.by_number(entry.execution_number)
        return (execution.runs[-1].seconds or 0.0) if execution is not None and execution.runs else 0.0

    def _poll_wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            # A hold ends the wait at once; nothing of it is remembered, so a release claims at once, even one that ended a
            # hold made and released between two polls.
            if self._stop_event.is_set() or self._hold.is_held or self._release_skips_wait or not self._store.queue.has_queued():
                return
            self._wake_event.wait(timeout=min(self._poll_interval, max(0.0, deadline - time.monotonic())))
            self._wake_event.clear()

    def _idle_wait(self) -> None:
        # No entry queued, or the queue is held: wait to be woken by a submission, a release, or shutdown, checking
        # occasionally in case a wake is missed.
        self._wake_event.wait(timeout=1.0)
        self._wake_event.clear()
