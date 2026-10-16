"""One run's timing as the executor keeps it: the timeline, and the events it makes of it."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from framecontrol.jobs.events import JobEvent, StageFinished, StageStarted, TimingFinished, TimingStarted
from framecontrol.jobs.timing.environment import HostLoad
from framecontrol.jobs.timing.model import Reading, StageSpan
from framecontrol.jobs.timing.parsers import StageParser
from framecontrol.jobs.timing.timeline import RunTimeline

# (stage, started, ended) read from the clock the run's timeline uses, for work done before the run started.
BeforeStart = tuple[str, float, float]


@dataclass(frozen=True)
class RunDescription:
    """What a run is timed as: its backend and pipeline, the workload it is asked to do, and the environment it does it in."""

    backend: str
    pipeline: str | None
    workload: dict[str, Any]
    environment: dict[str, Any]


class RunTiming:
    """The timing of the run ``number``: build it just before RunStarted, call ``begin`` just after, and ``finish`` just before RunFinished."""

    def __init__(self, number: int, clock: Callable[[], float], parser: StageParser | None, emit: Callable[[JobEvent], None], timestamp: Callable[[], str], host_load: Callable[[], HostLoad | None]) -> None:
        self._number = number
        self._host_load = host_load
        self._emit = emit
        self._timestamp = timestamp
        self.timeline = RunTimeline(clock, parser=parser, on_started=self._stage_started, on_finished=self._stage_finished)
        self._finished = False

    def begin(self, backend: str, pipeline: str | None, workload: dict[str, Any], environment: dict[str, Any], before_start: Sequence[BeforeStart] = ()) -> None:
        """The run started: announce what it is judged by, open ``prepare``, and add the work done for it before it started."""
        load = self._host_load()
        self._emit(TimingStarted(at=self._timestamp(), number=self._number, backend=backend, pipeline=pipeline, workload=workload, environment={**environment, "load_start": list(load) if load is not None else None}))
        self.timeline.begin()
        for stage, started, ended in before_start:
            self.timeline.before_start(stage, started, ended)

    def reading(self, text: str, elapsed: float) -> Reading | None:
        """Read a line the child wrote; the reading it gave, or None."""
        return self.timeline.reading(text, elapsed)

    def run_offset(self, elapsed: float) -> float:
        """A child's stamp (seconds after it started) as seconds after the run started."""
        return self.timeline.run_offset(elapsed)

    def finish(self, complete: bool) -> None:
        """The run ended: close its timeline (naming every gap) and say whether it is complete. Once only."""
        if self._finished:
            return
        self._finished = True
        self.timeline.finish()
        self._emit(TimingFinished(at=self._timestamp(), number=self._number, complete=complete, load_end=self._host_load()))

    def _stage_started(self, span: StageSpan) -> None:
        self._emit(StageStarted(at=self._timestamp(), number=self._number, stage=span.stage, phase=span.phase, source=span.source, start=span.start))

    def _stage_finished(self, span: StageSpan) -> None:
        self._emit(StageFinished(at=self._timestamp(), number=self._number, stage=span.stage, phase=span.phase, source=span.source, start=span.start, end=span.end, done=span.done, total=span.total, unit=span.unit, marks=span.marks))
