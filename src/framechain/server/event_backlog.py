"""A bounded, in-memory backlog of events for the gRPC ``WatchEvents`` RPC (Milestone 02): the Phase 2 job events
(``event_to_dict``) and the queue's own, each appended once and read by every watching client."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# How many events the backlog keeps; a client whose last_event_id falls before the oldest kept event must Reset and
# re-sync over HTTP, as one whose ID is from an earlier run of the server does too (see EventBacklog.__init__).
CAPACITY = 2000


@dataclass(frozen=True)
class BacklogEvent:
    id: int
    kind: str
    # The event's own fields, as JSON: event_to_dict's output for a job event, or the queue's own equivalent
    # mapping. Serialized here, once, rather than by every caller.
    data_json: str


class EventBacklog:
    """Every event this run of the server has emitted, the most recent ``capacity`` of them. IDs only go up, and
    are seeded from wall-clock time rather than restarting at 1, so an ID from a previous run of the server almost
    always reads as older than anything this run has kept, and gets Reset the same way a merely-old ID would,
    without this needing to track a separate run token to tell the two cases apart."""

    def __init__(self, *, capacity: int = CAPACITY) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._events: deque[BacklogEvent] = deque(maxlen=capacity)
        self._next_id = int(time.time() * 1000)

    def append(self, kind: str, data: dict[str, Any]) -> BacklogEvent:
        with self._condition:
            event = BacklogEvent(self._next_id, kind, json.dumps(data, ensure_ascii=False))
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return event

    def since(self, last_event_id: int) -> tuple[BacklogEvent, ...] | None:
        """Events after ``last_event_id``, oldest first; ``()`` when ``last_event_id`` is 0 (only events from now
        on: nothing is replayed); None when it names an event this backlog no longer holds, or one from before this
        run of the server even when nothing has been appended yet (Reset)."""
        with self._lock:
            if last_event_id == 0:
                return ()
            # The oldest ID this run could possibly still explain: a kept event's, or (backlog empty so far) the
            # next ID this run will give out. An empty backlog is not "nothing to compare against": a client
            # reconnecting to a freshly (re)started server, before its first event, must still be told to Reset.
            oldest_explainable = self._events[0].id if self._events else self._next_id
            if last_event_id < oldest_explainable - 1:
                return None
            return tuple(event for event in self._events if event.id > last_event_id)

    def wait_for_more(self, after_id: int, timeout: float) -> bool:
        """Blocks until an event after ``after_id`` arrives, or ``timeout`` elapses; returns whether one did."""
        with self._condition:
            return self._condition.wait_for(lambda: bool(self._events) and self._events[-1].id > after_id, timeout=timeout)

    def latest_id(self) -> int:
        with self._lock:
            return self._events[-1].id if self._events else self._next_id - 1
