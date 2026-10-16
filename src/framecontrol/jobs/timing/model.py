"""The vocabulary of stage timing: a reading a backend's output gives, and a span of time one stage took."""

from __future__ import annotations

from dataclasses import dataclass

SOURCE_CHILD = "child"
SOURCE_FRAMECONTROL = "framecontrol"
# A stage keeps this many marks whole and is evenly thinned beyond it.
MAX_MARKS = 1000

# (offset in seconds, units done at that offset)
Marks = tuple[tuple[float, int], ...]


@dataclass(frozen=True)
class Reading:
    """One line of a child's output, read as a stage's progress: ``done`` of ``total`` units were complete at ``offset``."""

    stage: str
    # The backend's own phase text, as printed.
    phase: str
    offset: float
    done: int | None = None
    total: int | None = None
    # steps or frames; None for a stage whose counter is not worth a mark each.
    unit: str | None = None
    # The line marks the start of work (Draw Things prints a stage's line as it starts): the stage before ends at its
    # offset. Otherwise it marks work done (H3 prints a unit's line as it completes): the stage before ended at its own
    # last reading.
    begins: bool = False
    # False for a counter that runs beside the stage's own (H3's ``denoise enqueue``): it is the stage's activity, not its progress.
    counted: bool = True


@dataclass(frozen=True)
class StageSpan:
    """The time one stage took, in seconds from the start of the run (a span of work done before the run started has negative offsets)."""

    stage: str
    # The backend's raw phase text of the stage's first reading; None for a Framecontrol stage.
    phase: str | None
    source: str
    start: float
    end: float
    done: int | None = None
    total: int | None = None
    unit: str | None = None
    marks: Marks = ()
    # The first mark's offset is an estimate (the backfill cannot recover when a phase's first reading was written).
    uncertain: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


def thin_marks(marks: Marks, limit: int = MAX_MARKS) -> Marks:
    """``marks`` whole up to ``limit``, else ``limit`` of them evenly spaced, the first and the last included."""
    if len(marks) <= limit:
        return marks
    last = len(marks) - 1
    return tuple(marks[round(index * last / (limit - 1))] for index in range(limit))
