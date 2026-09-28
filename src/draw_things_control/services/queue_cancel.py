"""Cancel a queue entry: a queued one never starts, a running one is stopped at once, and a finished one is refused,
naming its state (``JobExecutor.cancel()``'s own contract governs everything past the claim; see queue_worker.py)."""

from __future__ import annotations

from draw_things_control.core.errors import InputError, NotFoundError
from draw_things_control.services.queue_worker import QueueWorker
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store


class CancelRefusedError(InputError):
    """The entry cannot be cancelled: it already finished before this call, naming its state. Not a
    ``NotFoundError``: the entry exists, so this is not a 404 for a front end that maps error codes to statuses."""

    code = "invalid_state"


def cancel_entry(store: Store, worker: QueueWorker, entry_id: int) -> bool:
    """Cancel entry ``entry_id``. A queued entry is cancelled directly, through ``worker.cancel_queued`` (its own
    insert-and-publish lock, so this can never interleave with a claim); a running one is stopped through
    ``worker.cancel_running``. Either way, a race with the job's own natural end is not an error: the entry simply
    reads whatever that race actually produced. Only an entry already finished *before* this call is refused, naming
    its state.

    Returns True when a queued entry was cancelled directly, False when the worker was told to stop a running one.
    Either way the worker itself publishes the change; a caller need not.
    """
    entry = store.queue.get(entry_id)
    if entry is None:
        raise NotFoundError(f"No queue entry {entry_id}")
    state = QueueState(entry.state)
    if state is QueueState.QUEUED:
        if worker.cancel_queued(entry.id, entry.label):
            return True
        # The worker claimed it between our read and the cancel: fall through to the running case below.
    elif state is not QueueState.RUNNING:
        raise CancelRefusedError(f"{entry.label} cannot be cancelled: it is {entry.state}")
    worker.cancel_running(entry.id)
    return False
