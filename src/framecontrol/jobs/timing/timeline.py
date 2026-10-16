"""One run's timeline: the child's stages and Framecontrol's own, on a single clock, with no gap hidden."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Protocol

from framecontrol.jobs.timing.model import SOURCE_FRAMECONTROL, StageSpan
from framecontrol.jobs.timing.parsers import StageParser
from framecontrol.jobs.timing.tracker import SpanObserver, StageTracker

# A gap between two stages shorter than this is not worth a span of its own.
GAP_SECONDS = 0.001


class StageTimer(Protocol):
    """Times a step of Framecontrol's own work as a stage of the run."""

    def stage(self, name: str) -> AbstractContextManager[None]: ...


class NoTimer:
    """The timer of a caller that keeps no timeline (a test, a tool that finishes an output by itself)."""

    def stage(self, name: str) -> AbstractContextManager[None]:
        return nullcontext()


NO_TIMER = NoTimer()


class RunTimeline:
    """The spans of one run, from ``begin`` (RunStarted) to ``finish`` (just before RunFinished).

    ``prepare`` runs from the start to the child's spawn; the child's stages, read from its output by ``parser`` and
    placed where the child started on the run's clock; then Framecontrol's own, ``with timeline.stage(name)``. What is
    left between them is an ``unaccounted`` span, so the stages add up to the run's time. Work done for the run
    before it started (resizing the input) is a span with negative offsets, outside that sum."""

    def __init__(self, clock: Callable[[], float], *, parser: StageParser | None = None, on_started: SpanObserver | None = None, on_finished: SpanObserver | None = None) -> None:
        self._clock = clock
        self._origin = clock()
        self._parser = parser
        self._on_started = on_started
        self._on_finished = on_finished
        self._spans: list[StageSpan] = []
        self._tracker: StageTracker | None = None
        self._child_origin: float | None = None
        self._child_ended = False
        self._prepare_open = False

    def now(self) -> float:
        """Seconds since the run started."""
        return self._clock() - self._origin

    def run_offset(self, elapsed: float) -> float:
        """A moment ``elapsed`` seconds after the child started, as seconds since the run started; ``elapsed`` itself before a child has."""
        return self._child_origin + elapsed if self._child_origin is not None else elapsed

    def begin(self) -> None:
        """The run started: ``prepare`` begins."""
        self._prepare_open = True
        self._started(StageSpan("prepare", None, SOURCE_FRAMECONTROL, 0.0, 0.0))

    def before_start(self, stage: str, started: float, ended: float) -> None:
        """Work done for this run before it started, read from the same clock at ``started`` and ``ended``."""
        self._record(StageSpan(stage, None, SOURCE_FRAMECONTROL, started - self._origin, ended - self._origin))

    def child_started(self) -> None:
        """The child is about to spawn: ``prepare`` ends, and the child's stages are read from here."""
        self._child_origin = self.now()
        self._close_prepare(self._child_origin)
        self._tracker = StageTracker(origin=self._child_origin, on_started=self._started, on_finished=self._finished)

    def reading(self, text: str, elapsed: float) -> str | None:
        """Read a line the child wrote ``elapsed`` seconds after it started; the phase it names, or None for any other line."""
        if self._tracker is None or self._child_origin is None or self._parser is None:
            return None
        readings = self._parser.readings(text, self._child_origin + elapsed)
        if not readings:
            return None
        for reading in readings:
            self._tracker.feed(reading)
        return readings[0].phase

    def child_finished(self, elapsed: float | None = None) -> None:
        """The child exited ``elapsed`` seconds after it started (now, when not given): its last stage runs to here."""
        if self._tracker is not None and self._child_origin is not None and not self._child_ended:
            self._child_ended = True
            self._tracker.finish(self._child_origin + elapsed if elapsed is not None else self.now())

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time the block as a Framecontrol stage, closed even when the block raises."""
        start = self.now()
        self._started(StageSpan(name, None, SOURCE_FRAMECONTROL, start, start))
        try:
            yield
        finally:
            self._finished(StageSpan(name, None, SOURCE_FRAMECONTROL, start, self.now()))

    def finish(self) -> tuple[StageSpan, ...]:
        """The run ended: close what is open, name the gaps, and return every span in time order."""
        end = self.now()
        if self._child_origin is not None and not self._child_ended:
            self.child_finished(max(0.0, end - self._child_origin))
        self._close_prepare(end)
        cursor = 0.0
        for span in sorted((span for span in self._spans if span.start >= 0), key=lambda span: (span.start, span.end)):
            if span.start - cursor > GAP_SECONDS:
                self._record(StageSpan("unaccounted", None, SOURCE_FRAMECONTROL, cursor, span.start))
            cursor = max(cursor, span.end)
        if end - cursor > GAP_SECONDS:
            self._record(StageSpan("unaccounted", None, SOURCE_FRAMECONTROL, cursor, end))
        return tuple(sorted(self._spans, key=lambda span: (span.start, span.end)))

    def _close_prepare(self, end: float) -> None:
        if self._prepare_open:
            self._prepare_open = False
            self._finished(StageSpan("prepare", None, SOURCE_FRAMECONTROL, 0.0, end))

    def _record(self, span: StageSpan) -> None:
        self._started(StageSpan(span.stage, span.phase, span.source, span.start, span.start))
        self._finished(span)

    def _started(self, span: StageSpan) -> None:
        if self._on_started is not None:
            self._on_started(span)

    def _finished(self, span: StageSpan) -> None:
        self._spans.append(span)
        if self._on_finished is not None:
            self._on_finished(span)
