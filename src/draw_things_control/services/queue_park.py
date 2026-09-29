"""Park and unpark a running queue entry, and hold and release the queue (Milestone 05). A park lets the entry's current
run finish, ends the job ``parked``, and holds the queue until a release; ``queue_worker.py`` keeps the reservation and
the hold. Modeled on ``queue_cancel.py``."""

from __future__ import annotations

from draw_things_control.core.errors import InputError, NotFoundError
from draw_things_control.services.queue_worker import STOP_CANCEL, QueueWorker
from draw_things_control.state.queue import QueueRow, QueueState
from draw_things_control.state.store import Store


class ParkRefusedError(InputError):
    """A park or an unpark was refused, naming the reason. Not a ``NotFoundError``: the entry exists, so this is not a
    404 for a front end that maps error codes to statuses."""

    code = "invalid_state"


def _entry(store: Store, entry_id: int) -> QueueRow:
    entry = store.queue.get(entry_id)
    if entry is None:
        raise NotFoundError(f"No queue entry {entry_id}")
    return entry


def _state_now(store: Store, entry: QueueRow) -> str:
    updated = store.queue.get(entry.id)
    return updated.state if updated is not None else "gone"


def park_entry(store: Store, worker: QueueWorker, entry_id: int) -> None:
    """Make a park reservation on running entry ``entry_id``, holding the queue. Refused, naming the reason, for a queued
    or finished entry, one being cancelled or stopping with the server, and one that finished between this read and the
    worker's check: the queue was not held then, so the call must not look accepted. On an entry already parking, it
    holds the queue again when a release has ended the hold."""
    entry = _entry(store, entry_id)
    if entry.state == QueueState.QUEUED:
        raise ParkRefusedError(f"{entry.label} cannot be parked: it is queued and has not started; cancel it to remove it, or hold the queue to keep it from starting")
    if entry.state != QueueState.RUNNING:
        raise ParkRefusedError(f"{entry.label} cannot be parked: it is {entry.state}")
    if worker.park_running(entry.id, entry.label):
        return
    reason = worker.stop_reason(entry.id)
    if reason is not None:
        raise ParkRefusedError(f"{entry.label} cannot be parked: it is {'being cancelled' if reason == STOP_CANCEL else 'stopping with the server'}")
    raise ParkRefusedError(f"{entry.label} cannot be parked: it is {_state_now(store, entry)}")


def unpark_entry(store: Store, worker: QueueWorker, entry_id: int) -> None:
    """Withdraw running entry ``entry_id``'s park reservation: the job runs on as if it had never been made, and the
    hold is released when that reservation made it. A no-op on a running entry with none. Refused for a queued or
    finished entry, and once the park has taken effect."""
    entry = _entry(store, entry_id)
    if entry.state != QueueState.RUNNING:
        raise ParkRefusedError(f"{entry.label} cannot be unparked: it is {entry.state}")
    if worker.unpark_running(entry.id, entry.label):
        return
    state = _state_now(store, entry)
    if state in (QueueState.RUNNING, QueueState.PARKED):
        raise ParkRefusedError(f"{entry.label} has already parked")
    raise ParkRefusedError(f"{entry.label} cannot be unparked: it is {state}")
