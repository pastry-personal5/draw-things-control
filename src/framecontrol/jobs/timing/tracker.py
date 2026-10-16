"""Turns a child's readings into the spans of its stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from framecontrol.jobs.timing.model import SOURCE_CHILD, Marks, Reading, StageSpan, thin_marks

SpanObserver = Callable[[StageSpan], None]


@dataclass
class _Open:
    key: tuple[str, str | None]
    stage: str
    phase: str
    start: float
    # The offset of the stage's latest reading, which ends it when the next stage's readings are written as work completes.
    last: float
    done: int | None = None
    total: int | None = None
    unit: str | None = None
    marks: list[tuple[float, int]] = field(default_factory=list)

    def span(self, source: str, end: float) -> StageSpan:
        return StageSpan(self.stage, self.phase, source, self.start, end, self.done, self.total, self.unit, thin_marks(tuple(self.marks)))


class StageTracker:
    """Spans of one child's stages, from its start (``origin``) to the end it is given (``finish``).

    The spans are contiguous: a stage starts where the one before ended, the first at ``origin``, and the last runs to
    the child's exit, so the time before a stage's first line and after its last one is the stage's own. Readings that
    mark work done (H3) end the stage before at its own last reading; readings that mark work starting (Draw Things)
    end it at the new stage's first. A counter that goes back, or whose total changes, is a new pass and opens a new
    span of the same stage. An unknown phase is a stage of its own, keyed by its raw name."""

    def __init__(self, *, origin: float = 0.0, source: str = SOURCE_CHILD, on_started: SpanObserver | None = None, on_finished: SpanObserver | None = None) -> None:
        self._origin = origin
        self._source = source
        self._on_started = on_started
        self._on_finished = on_finished
        self._spans: list[StageSpan] = []
        self._open: _Open | None = None
        self._boundary = origin

    @property
    def spans(self) -> tuple[StageSpan, ...]:
        """The spans closed so far."""
        return tuple(self._spans)

    def feed(self, reading: Reading) -> None:
        current = self._open
        key = (reading.stage, reading.phase if reading.stage == "other" else None)
        if current is None:
            current = self._begin(reading, key, self._boundary)
        elif key != current.key:
            boundary = reading.offset if reading.begins else current.last
            if reading.begins and current.unit is not None and current.total is not None and current.done != current.total:
                # The next stage starting is the proof that this one's last unit is done.
                current.done = current.total
                current.marks.append((boundary, current.total))
            self._close(boundary)
            current = self._begin(reading, key, boundary)
        elif _restarted(current, reading):
            boundary = reading.offset if reading.begins else current.last
            self._close(boundary)
            current = self._begin(reading, key, boundary)
        current.last = max(current.last, reading.offset)
        if reading.done is not None:
            current.done, current.total = reading.done, reading.total
            current.unit = reading.unit or current.unit
            if current.unit is not None and (not current.marks or current.marks[-1][1] != reading.done):
                current.marks.append((reading.offset, reading.done))

    def finish(self, end: float) -> None:
        """The child exited at ``end``: the open span runs to it, and a child that printed no reading is one span."""
        if self._open is None:
            if not self._spans:
                span = StageSpan("child", None, self._source, self._origin, max(end, self._origin))
                self._notify(self._on_started, StageSpan("child", None, self._source, self._origin, self._origin))
                self._spans.append(span)
                self._notify(self._on_finished, span)
            return
        self._close(max(end, self._open.last))

    def _begin(self, reading: Reading, key: tuple[str, str | None], start: float) -> _Open:
        opened = _Open(key, reading.stage, reading.phase, start, start)
        self._open = opened
        self._notify(self._on_started, StageSpan(reading.stage, reading.phase, self._source, start, start))
        return opened

    def _close(self, end: float) -> None:
        assert self._open is not None
        span = self._open.span(self._source, end)
        self._spans.append(span)
        self._open = None
        self._boundary = end
        self._notify(self._on_finished, span)

    @staticmethod
    def _notify(observer: SpanObserver | None, span: StageSpan) -> None:
        if observer is not None:
            observer(span)


def _restarted(current: _Open, reading: Reading) -> bool:
    """Whether ``reading`` is the counter starting over: it went back, or its total is another."""
    if reading.done is None or current.done is None:
        return False
    return reading.done < current.done or (current.total is not None and reading.total != current.total)


__all__ = ["Marks", "SpanObserver", "StageTracker"]
