"""Restart recovery: what a crash or a clean shutdown left ``running`` becomes what actually happened, before the
worker starts. Must run against a store opened in ``WRITE`` mode (never ``RUN``, which prunes) and while the run
lock is held: holding it proves no runner is alive, so any row still ``running`` is a crash."""

from __future__ import annotations

from datetime import datetime

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.state.queue import QueueRow, QueueState
from draw_things_control.state.store import Store

_EXECUTION_TO_QUEUE_STATE = {"succeeded": QueueState.SUCCEEDED, "failed": QueueState.FAILED, "interrupted": QueueState.INTERRUPTED}


def recover_queue(store: Store, *, clock: Clock = datetime.now) -> None:
    """Close whatever a crash left running, in order: sweep executions first (a ``running`` row becomes
    ``interrupted``), then bring every ``running`` queue entry to what its execution actually shows. ``queued``
    entries are untouched: they stay queued and run in order."""
    store.sweep_interrupted()
    for entry in store.queue.list(state=str(QueueState.RUNNING)):
        _recover_entry(store, entry, clock)


def _recover_entry(store: Store, entry: QueueRow, clock: Clock) -> None:
    if entry.execution_number is None:
        # The crash landed between the worker's claim and JobStarted: nothing of it ever ran.
        store.queue.requeue(entry.id)
        return
    row_id = store.executions.row_of(entry.execution_number)
    execution = store.executions.get(row_id) if row_id is not None else None
    if execution is None:
        # The execution row itself never made it in (the crash landed between reserving the ID and the recorder's
        # insert); nothing of it ran either.
        store.queue.requeue(entry.id)
        return
    state = _EXECUTION_TO_QUEUE_STATE.get(execution.status, QueueState.INTERRUPTED)
    finished_at = execution.finished_at or local_timestamp(clock())
    store.queue.finish(entry.id, state=state, finished_at=finished_at)
