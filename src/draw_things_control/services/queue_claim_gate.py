"""``QueueWorker``'s own insert-and-publish (``enqueue``) and queued-cancel (``cancel_queued``): kept separate from
its claim and run bookkeeping, so that class's body stays under the project's size limit (as ``WorkerStatus``,
``queue_worker_status.py``, already is) -- but sharing its lock, passed in here rather than a lock of its own: a
claim (``QueueWorker._claim_and_run_one``), an insert, and a queued cancel must never interleave, so a client
watching events can never see a claim's 'running' published before an earlier 'queued' for the same entry
(Milestone 02's event-order fix, phase-3 changelog 2026-09-28)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

from draw_things_control.core.clock import Clock
from draw_things_control.services.history_delete import DeleteReport, delete_executions
from draw_things_control.services.queue_events import QueueEventPublisher
from draw_things_control.state.queue import QueueRow, QueueState
from draw_things_control.state.store import Store


class QueueClaimGate:
    def __init__(self, lock: threading.Lock, store: Store, events: QueueEventPublisher, clock: Clock, wake: Callable[[], None]) -> None:
        self._lock = lock
        self._store = store
        self._events = events
        self._clock = clock
        self._wake = wake

    def enqueue(self, insert: Callable[[], QueueRow]) -> QueueRow:
        """Run ``insert`` (a submission's or a resume's own ``store.queue.submit``) and publish the new entry's
        'queued' change, both under the shared lock: a claim already waiting on it can then never see the row --
        and so never publish 'running' -- before this publish (``routes_queue.py`` passes this in as
        ``submit_job``'s and ``resume_entry``'s own ``enqueue``)."""
        with self._lock:
            entry = insert()
            self._events.entry_changed(entry.label, entry.state)
        self._wake()
        return entry

    def cancel_queued(self, entry_id: int, label: str) -> bool:
        """Cancel ``entry_id`` if it is still queued, publishing under the shared lock; False once the worker has
        already claimed it (``QueueWorker.cancel_running`` applies instead: ``queue_cancel.py``'s ``cancel_entry``
        falls through to it)."""
        with self._lock:
            cancelled = self._store.queue.cancel_queued(entry_id, now=self._clock())
            if cancelled:
                self._events.entry_changed(label, str(QueueState.CANCELLED))
        if cancelled:
            self._wake()
        return cancelled

    def delete_executions(self, numbers: Sequence[int], *, dry_run: bool = False) -> DeleteReport:
        """Delete executions from the history (Milestone 06), checking which are running or in use under the shared
        lock, so no claim, job start, or submission lands between the check and the delete; the files go after it is
        released. Publishes nothing: other clients see a deletion when they next read the history."""
        return delete_executions(self._store, numbers, dry_run=dry_run, lock=self._lock)
