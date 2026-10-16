"""What a front end shows of a published span, from the span and its own clock: the seconds left, the share done, and
whether the range is worth a mention. Every front end that shows a forecast reads it through here, so a bar and a countdown
mean the same thing in the TUI, in ``fc queue add --wait``, and in a client of the API."""

from __future__ import annotations

from collections.abc import Mapping

# The range is shown when its half-width is over this share of the time left.
RANGE_SHARE = 0.03

Span = Mapping[str, float]


def seconds_left(span: Span, now: float) -> float:
    """The seconds to the span's end, never below zero."""
    return max(0.0, span["end_at"] - now)


def progress(span: Span, now: float) -> float:
    """The share done, 0 to 1: the share of the time from start to end that has passed, never below the floor the server
    held (which is all there is when the end is not after the start)."""
    length = span["end_at"] - span["started_at"]
    floor = span.get("floor", 0.0)
    if length <= 0:
        return floor
    return min(1.0, max(floor, (now - span["started_at"]) / length))


def show_range(span: Span, now: float) -> bool:
    """Whether the range is wide enough, against the time left, to be worth showing."""
    left = seconds_left(span, now)
    return left > 0 and (span["high"] - span["low"]) / 2 > RANGE_SHARE * left
