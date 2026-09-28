"""The queue host: the one place that takes the run lock for its whole lifetime, opens the state store, recovers what
a crash left running, and starts and stops the worker. What a future ``dtc serve`` (Milestone 02) wraps in a process."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from draw_things_control.core.clock import Clock
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.core.run_lock import SERVER_HOLDER_NAME, RunLock, is_draw_things_process
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.services.queue_events import EventSink
from draw_things_control.services.queue_recovery import recover_queue
from draw_things_control.services.queue_worker import QueueWorker
from draw_things_control.state.store import Store, StoreMode


class QueueHost:
    """Owns the run lock, the state store, and the queue worker for as long as the server is up.

    ``start()`` acquires the lock (refusing, as any starter does, while an earlier server's ``draw-things-cli`` is
    still alive), opens the store in ``WRITE`` mode (never pruning, so recovery reads a crash exactly as it left it),
    recovers, and starts the worker thread. ``stop()`` cancels the running job at once, ends any between-jobs wait,
    joins the worker thread, and only then releases the lock, so a second starter right after never races this
    entry's own update to ``interrupted``.
    """

    def __init__(self, paths: ProjectPaths, executor: JobExecutor, global_config: GlobalConfig, *, executable: str, shutdown_grace: float, clock: Clock = datetime.now, child_check: Callable[[int, str], bool] = is_draw_things_process, on_event: EventSink | None = None) -> None:
        self._paths = paths
        self._executor = executor
        self._global_config = global_config
        self._executable = executable
        self._shutdown_grace = shutdown_grace
        self._clock = clock
        self._child_check = child_check
        self._on_event = on_event
        self._lock: RunLock | None = None
        self._store: Store | None = None
        self.worker: QueueWorker | None = None

    @property
    def store(self) -> Store | None:
        """The open store, from ``start()`` until ``stop()``; None otherwise. Milestone 02's ``dtc serve`` reads
        this to build the ``ServerContext`` every route and the gRPC service share."""
        return self._store

    def start(self) -> None:
        """Take the lock, recover, and start the worker. Raises ``BusyError`` (refusing while an earlier server's
        ``draw-things-cli`` is still alive, or another host already holds it) or ``StateUnavailableError`` (from
        ``RunLock.acquire`` and ``Store.open``), leaving nothing started."""
        lock = RunLock(SERVER_HOLDER_NAME, directory=self._paths.state, child_check=self._child_check)
        lock.acquire()
        try:
            store = Store.open(self._paths.database, mode=StoreMode.WRITE, retention_days=self._global_config.history_retention_days, clock=self._clock)
        except BaseException:
            lock.release()
            raise
        try:
            recover_queue(store, clock=self._clock)
            session = JobRunSession(self._paths, self._executor, self._global_config)
            worker = QueueWorker(store, session, self._executor, lock, self._paths, self._global_config, executable=self._executable, shutdown_grace=self._shutdown_grace, clock=self._clock, on_event=self._on_event)
            worker.start()
        except BaseException:
            store.close()
            lock.release()
            raise
        self._lock, self._store, self.worker = lock, store, worker

    def stop(self) -> None:
        """Stop the worker, close the store, and release the lock last, in that order; safe to call once, after a
        successful ``start()``."""
        if self.worker is not None:
            self.worker.stop()
        if self._store is not None:
            self._store.close()
        if self._lock is not None:
            self._lock.release()
        self._lock = self._store = self.worker = None

    def __enter__(self) -> QueueHost:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
