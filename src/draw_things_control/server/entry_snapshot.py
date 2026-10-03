"""One queue entry's snapshot, as a watch sends it (Milestone 10): gRPC's ``WatchQueueEntry`` and the API's SSE watch
(``GET /v1/queue/{id}/watch``) both read it here, the fields and the rule for when one is sent, so the two cannot
drift. The fields are ``monitor.proto``'s ``QueueEntrySnapshot``'s, with the same names, an unset one as None."""

from __future__ import annotations

from typing import Any

from draw_things_control.server.context import ServerContext
from draw_things_control.state.ids import execution_id_text

# The one field that changes on every read while a run is active, so it is sent with a change, never as one.
ELAPSED_FIELD = "current_run_elapsed_seconds"


def entry_snapshot(context: ServerContext, entry_id: int) -> dict[str, Any] | None:
    """Entry ``entry_id``'s snapshot, or None once it is gone. ``park_requested`` and ``queue_held`` are always True
    or False, so a change either way is sent."""
    worker = context.worker
    # The reservation first, then the row: the worker marks the entry finished before it drops the reservation,
    # both under its lock, so a job that ends between the two reads shows its final state, never 'running'
    # without its reservation. The queue table alone holds every field read below.
    park_requested = worker.park_requested(entry_id)
    entry = context.store.queue.get(entry_id)
    if entry is None:
        return None
    progress = run_progress(context, entry.id)
    return {
        "queue_id": entry.label,
        "state": entry.state,
        "execution_id": execution_id_text(entry.execution_number) if entry.execution_number is not None else None,
        "current_run": progress["current_run"],
        ELAPSED_FIELD: progress[ELAPSED_FIELD],
        "cooldown_until": progress["cooldown_until"],
        "error": entry.error,
        "current_step": progress["current_step"],
        "current_step_total": progress["current_step_total"],
        "total_runs": entry.total_runs,
        "park_requested": park_requested,
        "queue_held": worker.hold_state().held,
    }


def run_progress(context: ServerContext, entry_id: int) -> dict[str, Any]:
    """The worker's progress on entry ``entry_id`` while it is the one running (its current run, the run's elapsed
    seconds, and its step), and when the between-jobs wait ends: what ``GET /v1/queue/{id}`` and a snapshot both
    carry."""
    worker = context.worker
    current_run = current_step = None
    if worker.current_entry_id() == entry_id:
        current_run = worker.current_run()
        current_step = worker.current_step()
    return {
        "current_run": current_run[0] if current_run is not None else None,
        ELAPSED_FIELD: current_run[1] if current_run is not None else None,
        "current_step": current_step[0] if current_step is not None else None,
        "current_step_total": current_step[1] if current_step is not None else None,
        "cooldown_until": worker.cooldown_until(),
    }


def snapshot_changed(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    """Whether ``current`` is sent after ``previous``, the last one sent: the first always, then whenever a field the
    contract names changes (state, execution ID, run, step, ``cooldown_until``, error, the park reservation, the
    hold, the run count), the elapsed seconds aside."""
    if previous is None:
        return True
    return {**previous, ELAPSED_FIELD: None} != {**current, ELAPSED_FIELD: None}
