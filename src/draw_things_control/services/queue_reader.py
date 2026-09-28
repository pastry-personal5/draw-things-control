"""The queue, read from the state store: the Queue widget's fallback while the server (and so its API) is down,
beside ``HistoryReader``'s own fallback for the execution history (Milestone 03). Reading never writes."""

from __future__ import annotations

from dataclasses import dataclass, field

from draw_things_control.core.errors import DtcError, NotFoundError, StateUnavailableError
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.queue import QueueRow

NO_QUEUE = "No queue entries"


@dataclass(frozen=True)
class QueueSnapshot:
    """The active entries (queued and running) and a page of finished ones, exactly as ``GET /queue`` shows them
    with no filter and no cursor -- the Queue widget's read-only fallback needs nothing more."""

    active: list[QueueRow] = field(default_factory=list)
    finished: list[QueueRow] = field(default_factory=list)
    message: str | None = None


class QueueReader:
    """Reads the state store's queue for the Queue widget's fallback, from worker threads.

    Mirrors ``HistoryReader``: a store is opened when the database first exists and kept, without pruning (the
    queue worker prunes when it opens its own store). Failures raise a DtcError (StateUnavailableError).
    """

    def __init__(self, paths: ProjectPaths, store: StoreProvider, *, finished_limit: int = 5) -> None:
        self._paths = paths
        self._store = store
        self._finished_limit = finished_limit

    def snapshot(self) -> QueueSnapshot:
        """The active entries and a page of the most recent finished ones, oldest-active-first then newest-finished-first,
        as ``GET /queue``'s own unfiltered, uncursored read shows them."""
        try:
            store = self._store.get(create=False)
            if store is None:
                return QueueSnapshot(message=NO_QUEUE)
            active = store.queue.list_active()
            finished = store.queue.list_finished(limit=self._finished_limit, offset=0)
        except DtcError as error:
            return QueueSnapshot(message=str(error))
        except Exception as error:
            raise StateUnavailableError(f"Cannot read the state database {self._paths.database}: {error}") from error
        if not active and not finished:
            return QueueSnapshot(message=NO_QUEUE)
        return QueueSnapshot(active, finished)

    def by_number(self, number: int) -> QueueRow:
        """One entry by its public number (Q0007); raises NotFoundError when there is none."""
        try:
            store = self._store.get(create=False)
            entry = store.queue.by_number(number) if store is not None else None
        except DtcError:
            raise
        except Exception as error:
            raise StateUnavailableError(f"Cannot read the state database {self._paths.database}: {error}") from error
        if entry is None:
            raise NotFoundError(f"No queue entry Q{number:04d}")
        return entry
