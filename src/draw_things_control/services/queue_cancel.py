"""Cancel a queue entry: a queued one never starts, a running one is stopped at once, and a finished one is refused,
naming its state (``JobExecutor.cancel()``'s own contract governs everything past the claim; see queue_worker.py)."""

from __future__ import annotations

from datetime import datetime

from draw_things_control.core.clock import Clock
from draw_things_control.core.errors import InputError, NotFoundError
from draw_things_control.services.queue_worker import QueueWorker
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store


class CancelRefusedError(InputError):
    """The entry cannot be cancelled: it already finished before this call, naming its state. Not a
    ``NotFoundError``: the entry exists, so this is not a 404 for a front end that maps error codes to statuses."""


def cancel_entry(store: Store, worker: QueueWorker, entry_id: int, *, clock: Clock = datetime.now) -> None:
    """Cancel entry ``entry_id``. A queued entry is cancelled directly; a running one is stopped through ``worker``.
    Either way, a race with the job's own natural end is not an error: the entry simply reads whatever that race
    actually produced. Only an entry already finished *before* this call is refused, naming its state."""
    entry = store.queue.get(entry_id)
    if entry is None:
        raise NotFoundError(f"No queue entry {entry_id}")
    state = QueueState(entry.state)
    if state is QueueState.QUEUED:
        if store.queue.cancel_queued(entry.id, now=clock()):
            worker.wake()
            return
        # The worker claimed it between our read and the cancel: fall through to the running case below.
    elif state is not QueueState.RUNNING:
        raise CancelRefusedError(f"{entry.label} cannot be cancelled: it is {entry.state}")
    worker.cancel_running(entry.id)
