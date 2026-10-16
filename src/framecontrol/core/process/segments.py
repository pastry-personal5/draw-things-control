"""Cut a child's output into the segments it wrote, each stamped when its last byte arrived.

draw-things-cli ends every bar line with a newline, but H3 begins each redraw with a carriage return and ends none of
them, so a reading is a complete segment only when the next redraw's ``\\r`` arrives, which at H3's denoise step is 19 to
75 seconds later. Stamping a segment with the arrival of its own last content byte, not of the byte that ended it,
gives a reading the time it was written; releasing one early, when the backend's predicate says it already reads as a
whole reading and nothing more arrives within an idle window, shows it when it was written as well.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

from framecontrol.core.process.output import strip_terminal_codes

# A segment is cut at either terminator, so a CRLF cuts a blank segment too, which is dropped.
_TERMINATORS = re.compile(r"([\r\n])")
# Whether text that has not been terminated yet already reads as a whole reading (a backend supplies it).
PartialReading = Callable[[str], bool]


class Segment(NamedTuple):
    """One thing the child wrote, with terminators removed, and when its last content byte arrived (``time.monotonic()``)."""

    text: str
    arrived: float


class SegmentSplitter:
    """Splits one stream's decoded text into segments. Not thread-safe: one reader thread owns one splitter."""

    def __init__(self, accepts: PartialReading | None = None, *, idle_seconds: float = 0.2) -> None:
        if idle_seconds < 0:
            raise ValueError("idle_seconds must not be negative")
        self._accepts = accepts
        self._idle_seconds = idle_seconds
        self._pending = ""
        self._arrived = 0.0
        # What of the pending segment was released early, so its terminated copy is not delivered a second time.
        self._released: str | None = None

    def feed(self, text: str, arrived: float) -> list[Segment]:
        """The segments ``text``, which arrived at ``arrived``, completes."""
        segments: list[Segment] = []
        for piece in _TERMINATORS.split(text):
            if piece in ("\r", "\n"):
                self._cut(segments)
            elif piece:
                self._pending += piece
                self._arrived = arrived
        return segments

    def idle_deadline(self) -> float | None:
        """When the pending segment may be released, or None when nothing is waiting to be."""
        if not self._releasable():
            return None
        return self._arrived + self._idle_seconds

    def release_idle(self, now: float) -> list[Segment]:
        """The pending segment, if it reads as a whole reading and nothing has arrived for the idle window."""
        deadline = self.idle_deadline()
        if deadline is None or now < deadline:
            return []
        self._released = self._pending
        return [Segment(self._pending, self._arrived)]

    def flush(self) -> list[Segment]:
        """The pending segment, at the end of the stream."""
        segments: list[Segment] = []
        self._cut(segments)
        return segments

    def _releasable(self) -> bool:
        return self._accepts is not None and bool(self._pending) and self._released != self._pending and self._accepts(strip_terminal_codes(self._pending))

    def _cut(self, segments: list[Segment]) -> None:
        if self._pending and self._pending != self._released:
            segments.append(Segment(self._pending, self._arrived))
        self._pending = ""
        self._released = None
