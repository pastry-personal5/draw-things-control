"""The events a stored run would have produced, at the times the executor emits them.

A recorded run is its stages (when each began and ended, with the marks of the ones that count steps or frames), and the
executor's events follow from that: ``StageStarted`` and ``StageFinished`` at the spans' edges, a ``RunOutput`` for every
reading the child printed, ``RunFinished`` at the end. The replay feeds a forecaster or a baseline this stream as if it were
live, and the forecaster's own tests play exact runs through it. Nothing here reads a file or the clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, JobStatus, RunFinished, RunOutput, RunStarted, RunStatus, StageFinished, StageStarted, TimingFinished, TimingStarted
from framecontrol.jobs.timing.features import FRAMECONTROL, HistoryExecution, HistoryRun, HistoryStage

TimedEvent = tuple[float, JobEvent]


@dataclass(frozen=True)
class ReplayJob:
    """A job's events in time order, and the truth the replay scores against: when each run began and ended."""

    events: tuple[TimedEvent, ...]
    starts: dict[int, float]
    ends: dict[int, float]
    started: float
    finished: float


def _stamp(time: float) -> str:
    return datetime.fromtimestamp(time).astimezone().isoformat(timespec="milliseconds")


def _readings(stage: HistoryStage) -> list[tuple[float, int | None, int | None]]:
    """(offset, done, total) of each line the child printed for ``stage``: its marks, else its start and its end."""
    if stage.source == FRAMECONTROL:
        return []
    if stage.marks:
        return [(offset, done, stage.total) for offset, done in stage.marks]
    if stage.total is not None:
        return [(stage.start, 0, stage.total), (stage.end, stage.total, stage.total)]
    return [(stage.start, None, None)]


def run_events(run: HistoryRun, number: int, total: int, origin: float) -> tuple[list[TimedEvent], float]:
    """The events of ``run`` (as run ``number`` of ``total``) beginning at ``origin``, and when it ends."""
    stages = run.counted_stages
    end = origin + run.seconds
    events: list[TimedEvent] = [
        (origin, RunStarted(at=_stamp(origin), number=number, total=total, pair="replay", positive="", negative=None, input=None, resized_input=None, output="", last_frame=None, command=(), backend=run.backend, pipeline=run.pipeline)),
        (origin, TimingStarted(at=_stamp(origin), number=number, backend=run.backend, pipeline=run.pipeline, workload=dict(run.workload), environment=dict(run.environment))),
    ]
    for stage in stages:
        began, ended = origin + stage.start, origin + stage.end
        events.append((began, StageStarted(at=_stamp(began), number=number, stage=stage.stage, phase=None, source=stage.source, start=stage.start)))
        for offset, done, units in _readings(stage):
            time = origin + offset
            events.append((time, RunOutput(at=_stamp(time), number=number, stream="stdout", text="", progress=None, percent=None, phase=stage.stage, offset=offset, stage=stage.stage, done=done, total=units)))
        events.append((ended, StageFinished(at=_stamp(ended), number=number, stage=stage.stage, phase=None, source=stage.source, start=stage.start, end=stage.end, done=stage.done, total=stage.total, unit=stage.unit, marks=stage.marks)))
    events.append((end, TimingFinished(at=_stamp(end), number=number, complete=True, load_end=None)))
    events.append((end, RunFinished(at=_stamp(end), number=number, status=RunStatus.SUCCEEDED, exit_code=0, seconds=run.child_seconds, output=None, last_frame=None)))
    # Built in the order the executor emits them (a stage ends, the next begins, its first reading is written), so a stable
    # sort by time keeps that order for events of one instant.
    return sorted(events, key=lambda item: item[0]), end


def replay_job(execution: HistoryExecution, *, first_run: int = 1, origin: float = 0.0) -> ReplayJob:
    """The events of the runs of ``execution`` from ``first_run`` on, beginning at ``origin``, each run after the last
    one's end and the cooldown the execution's policy gives for the time of that run's child."""
    runs = [run for run in execution.runs if run.run >= first_run]
    total = len(execution.runs)
    events: list[TimedEvent] = [
        (
            origin,
            JobStarted(at=_stamp(origin), job_name=f"E{execution.number:04d}", job_file="", source_text="", mode="i2v", total_runs=total, output_directory="", input=None, model="", seed=0, seed_source="replay", cooldown=execution.cooldown, cooldown_source="replay", manifest=None, log=None, first_run=first_run),
        )
    ]
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    cursor = origin
    for index, run in enumerate(runs):
        starts[run.run] = cursor
        stream, end = run_events(run, run.run, total, cursor)
        events.extend(stream)
        ends[run.run] = end
        cursor = end
        if index < len(runs) - 1:
            wait = execution.wait_after(run)
            if wait > 0:
                until = datetime.fromtimestamp(cursor + wait).astimezone().strftime("%H:%M:%S")
                events.append((cursor, CooldownStarted(at=_stamp(cursor), after_run=run.run, seconds=wait, until=until, mode=execution.cooldown.mode, ratio=execution.cooldown.ratio if execution.cooldown.mode == "auto" else None, run_seconds=run.child_seconds)))
                cursor += wait
                events.append((cursor, CooldownEnded(at=_stamp(cursor), waited_seconds=wait, cut_short=False)))
    events.append((cursor, JobFinished(at=_stamp(cursor), status=JobStatus.SUCCEEDED, exit_code=0, completed_runs=len(runs), total_runs=total, signal=None)))
    return ReplayJob(tuple(events), starts, ends, origin, cursor)
