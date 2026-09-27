"""The clock every timestamp comes from, so tests can fix it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

Clock = Callable[[], datetime]


def local_timestamp(now: datetime) -> str:
    """``now`` as a local ISO 8601 timestamp with an offset, to the second: ``2026-09-27T10:05:15+02:00``."""
    return now.astimezone().isoformat(timespec="seconds")
