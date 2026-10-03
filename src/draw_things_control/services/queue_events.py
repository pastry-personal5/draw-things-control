"""Turns the queue worker's own transitions (claim, finish, the between-jobs wait) and the job events it observes
into the (kind, data) shape an event sink takes (``server/event_backlog.py``'s ``EventBacklog.append``, kept out of
this layer: ``services/`` never imports ``server/``). A worker built with no sink does nothing extra, the common
case in tests that do not care about the event stream."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from draw_things_control.jobs.events import JobEvent, event_to_dict

# The return value is always discarded (object, not None, so a function returning something real -
# EventBacklog.append returns the stored BacklogEvent - can be passed directly, as dtc serve does).
EventSink = Callable[[str, dict[str, Any]], object]


class QueueEventPublisher:
    def __init__(self, sink: EventSink | None) -> None:
        self._sink = sink

    def job_event(self, event: JobEvent) -> None:
        """A Phase 2 job event, exactly as ``event_to_dict`` already shapes it for every other reader of it."""
        if self._sink is not None:
            data = event_to_dict(event)
            self._sink(data["kind"], data)

    def entry_changed(self, queue_id: str, state: str) -> None:
        if self._sink is not None:
            self._sink("queue_entry_changed", {"queue_id": queue_id, "state": state})

    def wait_started(self, queue_id: str, cooldown_until: float) -> None:
        if self._sink is not None:
            self._sink("queue_wait_started", {"queue_id": queue_id, "cooldown_until": cooldown_until})

    def wait_ended(self, queue_id: str) -> None:
        if self._sink is not None:
            self._sink("queue_wait_ended", {"queue_id": queue_id})

    def park_changed(self, queue_id: str, park_requested: bool) -> None:
        """A park reservation was made or withdrawn (Milestone 05). Its own kind, not ``queue_entry_changed``: that one
        means a change of state, and a front end reads its ``running`` as the claim that comes before ``JobStarted``."""
        if self._sink is not None:
            self._sink("queue_park_changed", {"queue_id": queue_id, "park_requested": park_requested})

    def held(self, since: str | None, by: str | None, caller: str | None = None) -> None:
        """The queue was held, or its hold changed hands; ``caller`` made it (Milestone 10)."""
        if self._sink is not None:
            self._sink("queue_held", {"since": since, "by": by, "caller": caller})

    def released(self) -> None:
        if self._sink is not None:
            self._sink("queue_released", {})
