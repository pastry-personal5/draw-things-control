"""The queue's hold (Milestone 05): while held, the worker starts no new entry. A park reservation holds the queue, and so
does ``/queue hold``; only a release ends it. The hold is kept in memory and in the state store's ``settings`` table,
under ``queue_hold``, so it survives a ``dtc serve`` restart. Its own class, so the hold's memory, its row, and the
reading of a damaged row stay together, apart from the worker's claim and cancel bookkeeping."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.services.queue_events import QueueEventPublisher
from draw_things_control.state.settings import SettingsRepository

HOLD_KEY = "queue_hold"


@dataclass(frozen=True)
class HoldState:
    """Whether the queue is held, since when (a local timestamp), and by which entry's park reservation (Q0007), or
    None for a ``/queue hold``. A damaged saved hold reads as held with neither."""

    held: bool = False
    since: str | None = None
    by: str | None = None

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> HoldState:
        """The hold fields of an API response (``held``, ``held_since``, ``held_by``, as ``serializers.queue_hold``
        writes them); not held when it has none."""
        return cls(held=bool(body.get("held")), since=body.get("held_since"), by=body.get("held_by"))

    @property
    def damaged(self) -> bool:
        """A saved hold that could not be read: held, with neither ``since`` nor ``by`` (a hold always saves ``since``)."""
        return self.held and self.since is None


def read_hold(settings: SettingsRepository, *, warn: bool = True) -> HoldState:
    """The saved hold. A row that cannot be read counts as held, with a warning unless ``warn`` is off: a damaged row
    never starts the queue by surprise. Shared by the worker and the TUI's read-only fallback (``QueueReader``), so both
    read a row the same way; the fallback polls, and warns only on its first read of a damaged row."""
    text = settings.get(HOLD_KEY)
    if text is None:
        return HoldState()
    try:
        data = json.loads(text)
        since, by = data["since"], data["by"]
        if not isinstance(since, str) or not (by is None or isinstance(by, str)):
            raise TypeError("since must be text and by text or null")
        datetime.fromisoformat(since)
    except (ValueError, TypeError, KeyError) as error:
        if warn:
            logger.warning("The saved queue hold cannot be read ({}); the queue counts as held until a release", error)
        return HoldState(held=True)
    return HoldState(held=True, since=since, by=by)


class QueueHold:
    """The hold, in memory and in its ``settings`` row, loaded once when the worker is built. Safe from any thread; each
    change publishes ``queue_held`` or ``queue_released``."""

    def __init__(self, settings: SettingsRepository, events: QueueEventPublisher, *, clock: Clock = datetime.now) -> None:
        self._settings = settings
        self._events = events
        self._clock = clock
        self._lock = threading.Lock()
        self._state = read_hold(settings)

    @property
    def is_held(self) -> bool:
        with self._lock:
            return self._state.held

    def snapshot(self) -> HoldState:
        with self._lock:
            return self._state

    def hold(self, by: str | None) -> bool:
        """Hold the queue, by the park reservation of entry ``by`` or, with None, directly (``/queue hold``). On a queue
        already held, a direct hold makes the hold its own, so a later unpark no longer releases it; a reservation's
        changes nothing. True when the queue was not held before."""
        with self._lock:
            was_held = self._state.held
            if was_held and (by is not None or self._state.by is None):
                return False
            since = self._state.since if was_held and self._state.since is not None else local_timestamp(self._clock())
            # The row first: a hold that could not be saved is not held in memory either.
            self._settings.set(HOLD_KEY, json.dumps({"since": since, "by": by}))
            self._state = HoldState(held=True, since=since, by=by)
        self._events.held(since, by)
        return not was_held

    def release(self) -> bool:
        """End the hold; False, doing nothing, when the queue is not held."""
        return self._release(None)

    def release_if_by(self, label: str) -> bool:
        """End the hold only when entry ``label``'s park reservation made it (an unpark); True when it did."""
        return self._release(label)

    def _release(self, only_by: str | None) -> bool:
        """End the hold, only when entry ``only_by``'s reservation made it unless that is None; True when it ended."""
        with self._lock:
            if not self._state.held or (only_by is not None and self._state.by != only_by):
                return False
            # The row first: a hold whose row could not be removed is still held in memory too.
            self._settings.delete(HOLD_KEY)
            self._state = HoldState()
        self._events.released()
        return True


def hold_text(hold: HoldState, *, already: bool = False) -> str:
    """``Queue held since 2026-09-29 12:04:05 (by Q0007)``, for a log line or a command's output; empty when not held.
    ``already`` words a hold asked for on a queue that was held before: ``Queue already held since ...``."""
    if not hold.held:
        return ""
    since = f" since {datetime.fromisoformat(hold.since).strftime('%Y-%m-%d %H:%M:%S')}" if hold.since is not None else ""
    return ("Queue already held" if already else "Queue held") + since + (f" (by {hold.by})" if hold.by is not None else "")


def hold_outcome_text(body: dict[str, Any]) -> str:
    """What ``POST /v1/queue/hold`` did, from its response, for ``dtc queue hold`` and the TUI's ``/queue hold``."""
    return hold_text(HoldState.from_body(body), already=not body.get("changed", True))
