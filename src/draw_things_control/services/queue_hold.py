"""The queue's hold (Milestone 05): while held, the worker starts no new entry. A park reservation holds the queue, and so
does ``/queue hold``; only a release ends it. The hold is kept in memory and in the state store's ``settings`` table,
under ``queue_hold``, so it survives a ``dtc serve`` restart. Its own class, so the hold's memory, its row, and the
reading of a damaged row stay together, apart from the worker's claim and cancel bookkeeping. From Milestone 10 it
records the caller that made it, and an agent may release only a hold an agent made (``queue_callers.py``)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from draw_things_control.core.clock import Clock, local_timestamp
from draw_things_control.core.errors import NotPermittedError
from draw_things_control.services.queue_callers import is_agent, maker_text
from draw_things_control.services.queue_events import QueueEventPublisher
from draw_things_control.state.settings import SettingsRepository

HOLD_KEY = "queue_hold"


@dataclass(frozen=True)
class HoldState:
    """Whether the queue is held, since when (a local timestamp), by which entry's park reservation (Q0007), or None
    for a ``/queue hold``, and the caller that made it (Milestone 10: cli, tui, mcp, or api). A damaged saved hold reads
    as held with none of them; a hold saved before callers were recorded has no caller. Either counts as a person's."""

    held: bool = False
    since: str | None = None
    by: str | None = None
    caller: str | None = None

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> HoldState:
        """The hold fields of an API response (``held``, ``held_since``, ``held_by``, ``hold_caller``, as
        ``serializers.queue_hold`` writes them); not held when it has none."""
        return cls(held=bool(body.get("held")), since=body.get("held_since"), by=body.get("held_by"), caller=body.get("hold_caller"))

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
        # No caller in a hold saved before Milestone 10: it reads as a person's, not as damaged.
        since, by, caller = data["since"], data["by"], data.get("caller")
        if not isinstance(since, str) or not all(value is None or isinstance(value, str) for value in (by, caller)):
            raise TypeError("since must be text, and by and caller text or null")
        datetime.fromisoformat(since)
    except (ValueError, TypeError, KeyError) as error:
        if warn:
            logger.warning("The saved queue hold cannot be read ({}); the queue counts as held until a release", error)
        return HoldState(held=True)
    return HoldState(held=True, since=since, by=by, caller=caller)


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

    def hold(self, by: str | None, caller: str | None = None) -> bool:
        """Hold the queue for ``caller``, by the park reservation of entry ``by`` or, with None, directly (``/queue
        hold``). On a queue already held, see ``_held_again``. True when the queue was not held before."""
        with self._lock:
            was_held = self._state.held
            if was_held:
                again = self._held_again(by, caller)
                if again is None:
                    return False
                by, caller = again
            since = self._state.since if was_held and self._state.since is not None else local_timestamp(self._clock())
            # The row first: a hold that could not be saved is not held in memory either.
            self._settings.set(HOLD_KEY, json.dumps({"since": since, "by": by, "caller": caller}))
            self._state = HoldState(held=True, since=since, by=by, caller=caller)
        self._events.held(since, by, caller)
        return not was_held

    def _held_again(self, by: str | None, caller: str | None) -> tuple[str | None, str | None] | None:
        """Call with the lock held, on a held queue: the hold's entry and caller after a hold by ``by`` for ``caller``,
        or None when it changes nothing. A direct hold makes a park's hold its own, so a later unpark no longer releases
        it (Milestone 05). A person's hold, direct or a park's, makes an agent's hold the person's, so an agent cannot
        release what a person's park or hold relies on; an agent never comes to own a person's hold (Milestone 10)."""
        state = self._state
        new_by = None if by is None else state.by
        if is_agent(caller) != is_agent(state.caller):
            new_caller = state.caller if is_agent(caller) else caller
        else:
            new_caller = caller if new_by is None and state.by is not None else state.caller
        if state.damaged or (new_by, new_caller) == (state.by, state.caller):
            return None
        return new_by, new_caller

    def release(self, caller: str | None = None) -> bool:
        """End the hold for ``caller``; False, doing nothing, when the queue is not held. Refused (``NotPermittedError``)
        for an agent when a person made the hold."""
        return self._release(None, caller)

    def release_if_by(self, label: str, caller: str | None = None) -> bool:
        """End the hold only when entry ``label``'s park reservation made it (an unpark); True when it did. Refused, as
        ``release`` is, for an agent when a person made it (a person parked the entry)."""
        return self._release(label, caller)

    def _release(self, only_by: str | None, caller: str | None) -> bool:
        """End the hold, only when entry ``only_by``'s reservation made it unless that is None; True when it ended. The
        check of ``caller`` is under the same lock as the release, so the hold cannot change hands between them."""
        with self._lock:
            if not self._state.held or (only_by is not None and self._state.by != only_by):
                return False
            if is_agent(caller) and not is_agent(self._state.caller):
                raise NotPermittedError(_refusal_text(self._state))
            # The row first: a hold whose row could not be removed is still held in memory too.
            self._settings.delete(HOLD_KEY)
            self._state = HoldState()
        self._events.released()
        return True


def _refusal_text(hold: HoldState) -> str:
    """Why an agent may not release ``hold``, a person's."""
    if hold.damaged:
        return "The queue's saved hold cannot be read, so it counts as a person's; an agent may release only a hold an agent made"
    park = f" with {hold.by}'s park" if hold.by is not None else ""
    return f"The queue was held{park} by {maker_text(hold.caller)}; an agent may release only a hold an agent made"


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


def release_outcome_text(body: dict[str, Any]) -> str:
    """What ``POST /v1/queue/release`` did, from its response, for ``dtc queue release`` and the TUI's ``/queue release``."""
    return "Queue released" if body.get("changed") else "The queue is not held"
