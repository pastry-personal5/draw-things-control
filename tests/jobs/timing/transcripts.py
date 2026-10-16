"""Synthetic child transcripts for the stage timing tests.

Each builder writes the lines a backend prints, from a table of phases, counters, and times, in the formats the
evidence in docs/phase-4/milestone-10-stage-timing.md records. The expected span list is worked out here from the table
alone (never by the code under test), so a transcript and its spans agree by construction. A fixture holds no prompt,
path, or real run data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from framecontrol.jobs.timing.model import StageSpan

# The stage each raw phase belongs to, written out again here on purpose: it is the oracle for the parsers' tables.
H3_STAGE = {
    "tokenizer": "tokenize",
    "video VAE encoder": "video_encode",
    "Qwen vision": "vision",
    "text encoder": "text_encode",
    "refine text": "refine_text",
    "precompute AdaLN": "adaln",
    "load transformer core": "load_core",
    "denoise enqueue": "denoise",
    "denoise": "denoise",
    "audio VAE encoder": "audio_encode",
    "audio VAE": "audio_decode",
    "preview VAE load": "preview_load",
    "video VAE load": "video_load",
    "FFmpeg": "ffmpeg",
}
H3_UNIT = {"denoise": "steps", "ffmpeg": "frames"}


class FakeClock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class Line:
    """One thing the child wrote: when (seconds after it started), on which stream, and the text without terminators."""

    offset: float
    text: str
    stream: str = "stdout"


@dataclass(frozen=True)
class Transcript:
    lines: tuple[Line, ...]
    spans: tuple[StageSpan, ...]
    # When the child exited, seconds after it started.
    exit_at: float
    # Draw Things only: the figures its ``Generation timing:`` block states, by the CLI's own definition.
    stated_total: float | None = None
    stated_average: float | None = None
    stated_median: float | None = None


@dataclass(frozen=True)
class H3Phase:
    """One phase: its raw name, its total, and when each reading was written (``times``, one per reading, rising).

    h3.c reports ``0/N`` as a phase begins, then ``1/N`` to ``N/N`` as its units complete; ``start`` is when the ``0/N``
    reading was written (none: the fixture leaves it out)."""

    phase: str
    times: tuple[float, ...]
    # Readings of an uncounted phase (``denoise enqueue``) are not the counter the stage reports.
    counted: bool = True
    start: float | None = None

    @property
    def total(self) -> int:
        return len(self.times)


def even(first: float, last: float, count: int) -> tuple[float, ...]:
    """``count`` times from ``first`` to ``last`` inclusive."""
    if count == 1:
        return (last,)
    return tuple(round(first + (last - first) * index / (count - 1), 3) for index in range(count))


def h3_r2av(*, steps: int = 30, frames: int = 24, exit_at: float = 321.0) -> Transcript:
    """An H3 ``r2av`` run: the setup phases, 30 denoise steps with the enqueue counter running ahead of them, then the tail."""
    denoise_times = even(40.0, 280.0, steps)
    enqueue_times = tuple(round(40.0 + (time - 40.0) * 0.1, 3) for time in denoise_times[:-1]) + (denoise_times[-1],)
    phases = (
        H3Phase("tokenizer", (0.2,), start=0.1),
        H3Phase("video VAE encoder", even(0.4, 1.6, 9), start=0.3),
        H3Phase("Qwen vision", even(2.0, 4.0, 4), start=1.8),
        H3Phase("text encoder", even(5.0, 9.0, 50), start=4.5),
        H3Phase("refine text", (12.5,), start=9.5),
        H3Phase("precompute AdaLN", (13.0, 14.0), start=12.8),
        H3Phase("load transformer core", even(15.0, 24.0, 50), start=14.5),
        H3Phase("denoise enqueue", enqueue_times, counted=False, start=24.4),
        H3Phase("denoise", denoise_times, start=24.6),
        H3Phase("audio VAE", even(284.0, 290.0, 6), start=282.0),
        H3Phase("video VAE load", even(291.0, 292.0, 36), start=290.5),
        H3Phase("FFmpeg", even(293.0, 320.0, frames), start=292.5),
    )
    return h3_transcript(phases, exit_at)


def h3_i2av(*, steps: int = 30, frames: int = 24, exit_at: float = 201.0) -> Transcript:
    """An H3 ``i2av`` run: the first frame is encoded, there are no references to read, and the denoise runs straight after the core loads."""
    denoise_times = even(30.0, 170.0, steps)
    phases = (
        H3Phase("tokenizer", (0.2,), start=0.1),
        H3Phase("video VAE encoder", even(0.4, 1.6, 9), start=0.3),
        H3Phase("text encoder", even(2.0, 6.0, 50), start=1.8),
        H3Phase("precompute AdaLN", (7.0, 8.0), start=6.5),
        H3Phase("load transformer core", even(9.0, 18.0, 50), start=8.5),
        H3Phase("denoise enqueue", tuple(round(30.0 + (time - 30.0) * 0.1, 3) for time in denoise_times[:-1]) + (denoise_times[-1],), counted=False, start=18.4),
        H3Phase("denoise", denoise_times, start=18.6),
        H3Phase("audio VAE", even(173.0, 178.0, 6), start=171.0),
        H3Phase("video VAE load", even(179.0, 180.0, 36), start=178.5),
        H3Phase("FFmpeg", even(181.0, 200.0, frames), start=180.5),
    )
    return h3_transcript(phases, exit_at)


def h3_transcript(phases: tuple[H3Phase, ...], exit_at: float, *, header: tuple[Line, ...] = ()) -> Transcript:
    """The lines of ``phases`` in the order they were written, and the spans they make: each stage runs from the end of
    the one before to its own last reading (the first from the child's start), and the last one to the child's exit."""
    readings = sorted(
        [(phase.start, phase.phase, 0, phase.total) for phase in phases if phase.start is not None] + [(time, phase.phase, index + 1, phase.total) for phase in phases for index, time in enumerate(phase.times)],
        key=lambda item: (item[0], phases_order(phases, item[1]), item[2]),
    )
    # h3.c writes `"\r%-25s %4d/%-4d"`: the phase padded to 25 columns, the number done right-aligned, the total padded.
    lines = (*header, *(Line(time, f"{name:<25} {done:>4}/{total:<4}") for time, name, done, total in readings))
    spans: list[StageSpan] = []
    previous_end = 0.0
    stages: list[str] = []
    for phase in phases:
        if H3_STAGE[phase.phase] not in stages:
            stages.append(H3_STAGE[phase.phase])
    for position, stage in enumerate(stages):
        members = [phase for phase in phases if H3_STAGE[phase.phase] == stage]
        counter = next(phase for phase in members if phase.counted)
        end = max(phase.times[-1] for phase in members)
        if position == len(stages) - 1:
            end = exit_at
        unit = H3_UNIT.get(stage)
        begun = ((counter.start, 0),) if counter.start is not None else ()
        marks = (*begun, *((time, index + 1) for index, time in enumerate(counter.times))) if unit else ()
        spans.append(StageSpan(stage=stage, phase=members[0].phase, source="child", start=previous_end, end=end, done=counter.total, total=counter.total, unit=unit, marks=marks))
        previous_end = end
    return Transcript(lines, tuple(spans), exit_at)


def phases_order(phases: tuple[H3Phase, ...], name: str) -> int:
    return next(index for index, phase in enumerate(phases) if phase.phase == name)


@dataclass(frozen=True)
class DrawThingsRun:
    """What a Draw Things run did, as the times its bar lines were written. ``step_starts`` holds when each step's
    ``Sampling... n / N`` line was written (a step's line is written when it starts)."""

    starting: float = 7.4
    processing: tuple[float, ...] = (9.1, 13.1, 13.1)
    step_starts: tuple[float, ...] = field(default_factory=lambda: tuple(round(17.1 + 51.0 * index + (index % 4) * 0.7, 3) for index in range(40)))
    finishing: tuple[float, ...] = (2123.0, 2134.6)
    generated: float = 2134.7
    exit_at: float = 2134.9


def printed_step_start(index: int, true_start: float) -> float:
    """When a step's line was stamped: a few milliseconds off the CLI's own clock, as a pipe read is, and exactly on it for the first step."""
    return round(true_start + (((index * 7) % 11) - 5) * 0.004, 3) if index else true_start


def draw_things(run: DrawThingsRun | None = None) -> Transcript:
    """A Draw Things run with its ``Generation timing:`` block. The block's figures are the CLI's own, from its own clock:
    the first step runs from the last ``Processing`` line to the first sampling reading, the next from one reading to the
    next, and the last step (which ``Finishing`` ends) is not counted. The lines are stamped a few milliseconds off it."""
    run = run or DrawThingsRun()
    total = len(run.step_starts)
    printed = tuple(printed_step_start(index, start) for index, start in enumerate(run.step_starts))
    lines = [Line(3.0, "I1005 18:13:58.720856 10976635 ev_poll_posix.cc:587] FD from fork parent still in poll list", "stderr"), Line(run.starting, "Models directory: models")]
    lines.append(Line(run.starting, "Starting... [ ] 0  %"))
    lines.extend(Line(time, f"Processing... [ ] {index + 1}  %") for index, time in enumerate(run.processing))
    lines.extend(Line(time, f"Sampling... {index + 1} / {total} [ ] {2 + index}  %") for index, time in enumerate(printed))
    lines.extend(Line(time, f"Finishing... [█] {98 if index == 0 else 100}%") for index, time in enumerate(run.finishing))
    lines.append(Line(run.generated, "Generated [█] 100%"))
    durations = [run.step_starts[0] - run.processing[-1]] + [later - earlier for earlier, later in zip(run.step_starts, run.step_starts[1:], strict=False)]
    ordered = sorted(durations)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    stated_total = round(run.exit_at - 2.1, 2)
    average = sum(durations) / len(durations)
    lines.extend(
        [
            Line(run.exit_at, "Wrote: out.mov"),
            Line(run.exit_at, "Generation timing:"),
            Line(run.exit_at, f"  Total generation time (including model loading): {stated_total:.2f} s"),
            Line(run.exit_at, f"  Sampling step time ({len(durations)} step(s)): avg {average:.2f} s, median {median:.2f} s"),
        ]
    )
    steps = tuple((time, index) for index, time in enumerate(printed))
    sampling_end = run.finishing[0]
    spans = (
        StageSpan("starting", "Starting", "child", 0.0, run.processing[0]),
        StageSpan("processing", "Processing", "child", run.processing[0], printed[0]),
        StageSpan("sampling", "Sampling", "child", printed[0], sampling_end, total, total, "steps", (*steps, (sampling_end, total))),
        StageSpan("finishing", "Finishing", "child", sampling_end, run.exit_at),
    )
    return Transcript(tuple(sorted(lines, key=lambda line: line.offset)), spans, run.exit_at, stated_total, average, median)
