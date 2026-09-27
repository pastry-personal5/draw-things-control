"""The Job Definition widget's sort, which the state store keeps across sessions."""

from __future__ import annotations

import threading

from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.tui.commands import SORT_KEYS

SORT_SETTING = "job_definition.sort"
DEFAULT_SORT = ("changed", True)


class SortPreference:
    """The remembered sort: a key and a direction. Neither method raises: a failed worker closes the app."""

    def __init__(self, store: StoreProvider) -> None:
        self._store = store
        # Saves are written one at a time, and only if no later choice was saved first.
        self._lock = threading.Lock()
        self._saved_choice = 0

    def sort(self) -> tuple[str, bool]:
        """The remembered sort (key, descending); the default when none is kept or the store cannot say."""
        try:
            store = self._store.get(create=False)
            value = store.settings.get(SORT_SETTING) if store is not None else None
        except Exception:
            return DEFAULT_SORT
        key, _, direction = (value or "").partition(" ")
        return (key, direction == "desc") if key in SORT_KEYS and direction in ("asc", "desc") else DEFAULT_SORT

    def keep(self, key: str, descending: bool, choice: int) -> str | None:
        """Remember the sort across sessions, unless a later ``choice`` (they count up) was saved first; why not, when the
        store cannot."""
        with self._lock:
            if choice <= self._saved_choice:
                return None
            try:
                store = self._store.get(create=True)
                assert store is not None
                store.settings.set(SORT_SETTING, f"{key} {'desc' if descending else 'asc'}")
            except Exception as error:
                return f"Cannot keep the sort: {error}"
            self._saved_choice = choice
        return None
