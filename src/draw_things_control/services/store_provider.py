"""The state store for a front end that only browses: opened on first use, kept, and closed with the screen."""

from __future__ import annotations

import threading

from draw_things_control.core.paths import ProjectPaths
from draw_things_control.state.database import StateError
from draw_things_control.state.store import Store, StoreMode


class StoreProvider:
    """Opens the state store when it is first needed and keeps it, so every worker thread reuses its connection.

    It never prunes: pruning writes, and that is the job worker's to do when it opens its own store. Only a write
    creates the database; a read of one that does not exist yet finds no store, so browsing creates no file.
    """

    def __init__(self, paths: ProjectPaths, retention_days: int) -> None:
        self._paths = paths
        self._retention_days = retention_days
        self._store: Store | None = None
        self._closed = False
        self._lock = threading.Lock()

    def get(self, *, create: bool) -> Store | None:
        """The store, or None when ``create`` is false and there is no database yet; raises StateError when it cannot be used or the provider is closed."""
        with self._lock:
            if self._closed:
                raise StateError("The state store is closed")
            if self._store is None:
                path = self._paths.database
                if not create and not path.exists():
                    return None
                self._store = Store.open(path, mode=StoreMode.WRITE if create else StoreMode.BROWSE, retention_days=self._retention_days)
            return self._store

    def close(self) -> None:
        with self._lock:
            self._closed = True
            store, self._store = self._store, None
        if store is not None:
            store.close()
