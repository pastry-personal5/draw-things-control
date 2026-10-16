"""Rebuild a run's stages from the job log it left, for runs timed before the recorder existed.

A log holds the child's lines with the seconds the runner stamped them (to 0.1 s) and the wall-clock time they were
logged, and Framecontrol's own lines with wall-clock times only. A run's origin is its ``Run n/N`` line; the child's
start is where its lines say the runner began; Framecontrol's finishing is, coarsely, from ``Subprocess finished`` to the
run's last check. A log written before output was stamped as read holds H3's readings one update after they were written
(each redraw ends only when the next begins); ``remove_lag`` puts them back, and flags the readings it can only estimate."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from framecontrol.jobs.timing.model import SOURCE_FRAMECONTROL, Reading, StageSpan
from framecontrol.jobs.timing.parsers import StageParser
from framecontrol.jobs.timing.tracker import StageTracker

_LOG_LINE = re.compile(r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) (?:\[(?P<stream>stdout|stderr) \+(?P<offset>\d+(?:\.\d+)?)s\]|\[app\]) (?P<text>.*)$")
_RUN_LINE = re.compile(r"^Run (?P<number>\d+)/\d+ \(pair ")
_STARTED = re.compile(r"^Started subprocess \(PID \d+(?P<stamped>, output stamped as read)?\)")
_FINISHED = re.compile(r"^Subprocess finished with exit code (?P<code>-?\d+)")
# Lines that end a run's finishing: the wait before the next run, and the job's own end.
_ENDS_RUN = re.compile(r"^(Cooldown|Job )")
_EPOCH = datetime(1970, 1, 1)

ParserFor = Callable[[int], StageParser | None]


@dataclass(frozen=True)
class LoggedRun:
    """One run as its log tells it."""

    number: int
    spans: tuple[StageSpan, ...]
    # Seconds from the run's start to the end of its last span.
    seconds: float
    # The log says the child's output was stamped as read, so no shift was applied.
    source_stamped: bool
    # The log says the child exited.
    finished: bool
    exit_code: int | None


@dataclass
class _Section:
    number: int
    origin: float
    started: float | None = None
    stamped: bool = False
    child: list[tuple[float, float, str]] = field(default_factory=list)
    child_end: float | None = None
    exit_code: int | None = None
    last_finish: float | None = None
    closed: bool = False


def parse_job_log(text: str, parser_for: ParserFor) -> dict[int, LoggedRun]:
    """Each run in ``text`` by its number. ``parser_for`` gives the parser of a run's backend (None: its child is one span)."""
    sections: dict[int, _Section] = {}
    current: _Section | None = None
    for raw in text.splitlines():
        match = _LOG_LINE.match(raw)
        if match is None:
            continue
        at = _seconds(match["time"])
        if match["stream"] is not None:
            if current is not None and current.child_end is None:
                current.child.append((at, float(match["offset"]), match["text"]))
            continue
        body = match["text"]
        run_line = _RUN_LINE.match(body)
        if run_line is not None:
            current = sections[int(run_line["number"])] = _Section(int(run_line["number"]), at)
            continue
        if current is not None:
            _app_line(current, at, body)
    return {number: _build(section, parser_for(number)) for number, section in sections.items()}


def _app_line(section: _Section, at: float, body: str) -> None:
    started = _STARTED.match(body)
    finished = _FINISHED.match(body)
    if started is not None:
        section.started, section.stamped = at, started["stamped"] is not None
    elif finished is not None:
        section.child_end, section.exit_code = at, int(finished["code"])
    elif _ENDS_RUN.match(body):
        section.closed = True
    elif section.child_end is not None and not section.closed and not body.startswith("Run ") and (f"(run {section.number})" in body or body.startswith(("Tagged ", "The color correction", "Could not "))):
        section.last_finish = at


def _build(section: _Section, parser: StageParser | None) -> LoggedRun:
    origin = section.origin
    child_origin = _child_origin(section)
    if section.child_end is not None:
        child_end = section.child_end - origin
    elif section.child:
        child_end = max(child_origin + offset for _at, offset, _text in section.child)
    else:
        child_end = child_origin
    spans = [StageSpan("prepare", None, SOURCE_FRAMECONTROL, 0.0, child_origin)]
    if section.started is not None or section.child:
        tracker = StageTracker(origin=child_origin)
        estimated: set[float] = set()
        if parser is not None:
            readings = [reading for _at, offset, text in section.child for reading in parser.readings(text, child_origin + offset)]
            if parser.unterminated_redraws and not section.stamped:
                readings, estimated = remove_lag(readings, child_origin)
            for reading in readings:
                tracker.feed(reading)
        tracker.finish(child_end)
        spans.extend(replace(span, uncertain=True) if span.marks and span.marks[0][0] in estimated else span for span in tracker.spans)
    if section.child_end is not None and section.last_finish is not None and section.last_finish - origin > child_end:
        spans.append(StageSpan("finalize", None, SOURCE_FRAMECONTROL, child_end, section.last_finish - origin))
    return LoggedRun(section.number, tuple(spans), spans[-1].end, section.stamped, section.child_end is not None, section.exit_code)


def _child_origin(section: _Section) -> float:
    """When the runner started, on the run's clock: each child line says it (when it was logged less its stamp). A line is
    logged when it is delivered, never before it is stamped, and a line stamped as read may be delivered up to an idle
    window late (H3's mid-phase readings all are, and only a phase's last reading is delivered at once), so the least of
    them is the one that carries no delay; the stamp's 0.1 s resolution is all that is left. The runner starts before it
    logs `Started subprocess`."""
    if section.child:
        return max(0.0, min(at - section.origin - offset for at, offset, _text in section.child))
    return max(0.0, section.started - section.origin) if section.started is not None else 0.0


def remove_lag(readings: list[Reading], child_origin: float) -> tuple[list[Reading], set[float]]:
    """Move each reading from when it was logged back to when it was written, and the offsets that are only estimates.

    A reading that is not its phase's last is delivered when the next redraw begins, so it was logged at the time the
    next reading was written: reading i was written when reading i - 1 was logged. A phase's last reading ends in a
    newline and is exact. That leaves the first reading after an exact one: its write time is not in the log, and is
    estimated from the gap to the reading after it, or halfway from the last exact time when there is none."""
    final = [reading.done is None or reading.total is None or reading.done == reading.total for reading in readings]
    moved: list[Reading] = []
    estimated: set[float] = set()
    for index, reading in enumerate(readings):
        if index > 0 and not final[index - 1]:
            written = readings[index - 1].offset
        elif final[index]:
            written = reading.offset
        else:
            exact = readings[index - 1].offset if index > 0 else child_origin
            if index + 1 < len(readings) and not final[index + 1]:
                written = max(exact, reading.offset - (readings[index + 1].offset - reading.offset))
            else:
                written = (exact + reading.offset) / 2
            estimated.add(written)
        moved.append(replace(reading, offset=written))
    return moved, estimated


def _seconds(stamp: str) -> float:
    return (datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f") - _EPOCH).total_seconds()
