"""A frozen copy of the TUI's step-counter estimator as it was before Milestone 11 replaced it, for the replay to score.

``tui/estimate.py`` and ``LiveRun`` read a run's progress from the last ``n/N`` any line printed and priced a run from
the latest run of any job. This module keeps that logic unchanged: the functions are the old ones with ``live`` replaced by
this object's own state and the clock passed in. It reads only what the old code read, the *text* of each line, so a
replay stream (stage, counter, and phase text) is turned back into the line the child printed (``line_of``) and read with
the old patterns. That reproduces the faults Milestone 10's evidence records: an early ``1/1`` or ``50/50`` marks an H3 run
as finishing, and Draw Things' ``40 / 40`` is read as the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted, RunStatus
from framecontrol.services.timing_replay import Interval, JobContext, Reading

# The patterns of core/process/output.py as they were: a "[1/3]" file counter is not a step counter.
PROGRESS_PATTERN = re.compile(r"(?<![\d\[])(\d+)\s*/\s*(\d+)(?!\d)")
PERCENT_PATTERN = re.compile(r"(?<!\d)(\d{1,3})\s*%")

# The percentage Draw Things' bar showed on the lines that count no step; a stand-in, since the replay does not store it.
_DRAW_THINGS_PERCENT = {"starting": 0, "processing": 50, "finishing": 98}


@dataclass(frozen=True)
class StepReading:
    step: int
    total: int
    at: float


@dataclass(frozen=True)
class PastRun:
    """A run that already succeeded: draw-things-cli's own time for the whole run, and its step count, when known."""

    seconds: float
    steps: int | None = None


@dataclass(frozen=True)
class FinishedRun:
    number: int
    status: str
    seconds: float | None
    full_seconds: float
    tail_seconds: float | None
    steps: int | None = None


@dataclass(frozen=True)
class Estimate:
    """A share done (0 to 1) and the seconds left; either None while it cannot be told. ``finishing``: past the last step."""

    fraction: float | None = None
    remaining: float | None = None
    finishing: bool = False


def line_of(event: RunOutput) -> str:
    """The line a reading was written from: Draw Things' bar line, H3's ``<phase> n/N``, or nothing for any other line."""
    if event.stage is None:
        return event.text
    if event.stage == "sampling" and event.done is not None and event.total:
        step = event.done + 1
        return f"Sampling... {step} / {event.total} [ ] {round(100 * step / event.total)} %"
    if event.stage in _DRAW_THINGS_PERCENT:
        return f"{event.phase}... [ ] {_DRAW_THINGS_PERCENT[event.stage]} %"
    if event.done is not None and event.total is not None:
        # h3.c writes `"\r%-25s %4d/%-4d"`.
        return f"{event.phase or event.stage:<25} {event.done:4d}/{event.total:<4d}"
    return event.text


def _progress(text: str) -> tuple[int, int] | None:
    match = PROGRESS_PATTERN.search(text)
    if match is None:
        return None
    current, total = (int(value) for value in match.groups())
    return (current, total) if total > 0 and current <= total else None


def _percent(text: str) -> int | None:
    match = PERCENT_PATTERN.search(text)
    if match is None:
        return None
    percent = int(match.group(1))
    return percent if percent <= 100 else None


class FrozenEstimator:
    """The old estimator over a stream of job events. ``observe`` takes each event with the time it arrived on one clock
    (any: the old code used the TUI's monotonic one), and the estimates are made at a time on that clock."""

    def __init__(self, *, past_run: PastRun | None = None) -> None:
        self.past_run = past_run
        self.started: JobStarted | None = None
        self.total_runs = 0
        self.active_run: int | None = None
        self.run_started_at: float | None = None
        self.percent: int | None = None
        self.cooldown: CooldownStarted | None = None
        self.cooldown_ends_at: float | None = None
        self.cooled_after_run: int | None = None
        self.job_started_at: float | None = None
        self.stopped_at: float | None = None
        self.first_step: StepReading | None = None
        self.last_step: StepReading | None = None
        self.finished_runs: list[FinishedRun] = []
        self.finished: JobFinished | None = None

    def observe(self, event: JobEvent, now: float) -> None:
        if isinstance(event, JobStarted):
            self.started, self.total_runs, self.job_started_at = event, event.total_runs, now
        elif isinstance(event, RunStarted):
            self.active_run, self.run_started_at = event.number, now
            self.percent = None
            self.first_step = self.last_step = None
        elif isinstance(event, RunOutput):
            self._run_output(event, now)
        elif isinstance(event, RunFinished):
            self._run_finished(event, now)
        elif isinstance(event, CooldownStarted):
            self.cooldown, self.cooldown_ends_at = event, now + event.seconds
        elif isinstance(event, CooldownEnded):
            self.cooled_after_run = self.cooldown.after_run if self.cooldown is not None else None
            self.cooldown = self.cooldown_ends_at = None
        elif isinstance(event, JobFinished):
            self.finished = event
            self.active_run = self.run_started_at = None
            self.cooldown = self.cooldown_ends_at = None

    def request_stop(self, now: float) -> None:
        if self.stopped_at is None:
            self.stopped_at = now

    def moment(self, now: float) -> float:
        """The time the estimates are made at: now, or when a stop was requested, so a stopping job's bars stay still."""
        return self.stopped_at if self.stopped_at is not None else now

    def _run_output(self, event: RunOutput, now: float) -> None:
        text = line_of(event)
        progress, percent = _progress(text), _percent(text)
        if progress is None and percent is None:
            return
        self.percent = percent if percent is not None else self.percent
        if progress is not None:
            step, total = progress
            last = self.last_step
            if self.first_step is None or last is None or step < last.step or total != last.total:
                self.first_step = StepReading(step, total, now)
            self.last_step = StepReading(step, total, now)

    def _run_finished(self, event: RunFinished, now: float) -> None:
        full = now - self.run_started_at if self.run_started_at is not None else event.seconds or 0.0
        tail = now - self.last_step.at if self.last_step is not None else None
        steps = self.last_step.total if self.last_step is not None else None
        self.finished_runs.append(FinishedRun(event.number, event.status, event.seconds, max(0.0, full), tail, steps))
        self.active_run = self.run_started_at = None

    def reference_run(self) -> PastRun | None:
        """The run a run estimates from before its own rate: this job's last successful run, or else the store's latest."""
        own = next((run for run in reversed(self.finished_runs) if run.status == RunStatus.SUCCEEDED and run.seconds), None)
        if own is not None and own.seconds is not None:
            return PastRun(own.seconds, own.steps)
        return self.past_run

    def run_estimate(self, now: float) -> Estimate:
        """The active run: its share done and its seconds left, from the best measure so far; ``finishing`` after the last step."""
        first, last, past = self.first_step, self.last_step, self.reference_run()
        if last is None:
            return self._before_steps(now, past)
        fraction = last.step / last.total
        if last.step >= last.total:
            return Estimate(1.0, self._tail_left(now), finishing=True)
        if first is None or last.step <= first.step:
            if past is None:
                return Estimate(fraction)
            if past.steps is None or past.steps != last.total:
                return Estimate(fraction, self._past_left(now, past))
            return Estimate(fraction, _positive((last.total - last.step) * past.seconds / past.steps - (now - last.at)))
        rate = (last.at - first.at) / (last.step - first.step)
        return Estimate(fraction, max(0.0, (last.total - last.step) * rate - (now - last.at)))

    def _before_steps(self, now: float, past: PastRun | None) -> Estimate:
        percent = self.percent / 100 if self.percent is not None else None
        left = self._past_left(now, past)
        if past is None or left is None or self.run_started_at is None:
            return Estimate(percent)
        return Estimate(percent if percent is not None else (now - self.run_started_at) / past.seconds, left)

    def _past_left(self, now: float, past: PastRun | None) -> float | None:
        if past is None or past.seconds <= 0 or self.run_started_at is None:
            return None
        return _positive(past.seconds - (now - self.run_started_at))

    def job_estimate(self, now: float) -> Estimate:
        """The whole job: the seconds left and the share of the job's time done, cooldowns included."""
        if self.started is None or self.job_started_at is None or self.finished is not None:
            return Estimate()
        total = self.total_runs
        succeeded = [run for run in self.finished_runs if run.status == RunStatus.SUCCEEDED]
        full = _average([run.full_seconds for run in succeeded])
        child = _average([run.seconds for run in succeeded if run.seconds is not None])
        if self.cooldown is not None and self.cooldown_ends_at is not None:
            current, done, waits_ahead = max(0.0, self.cooldown_ends_at - now), self.cooldown.after_run, 0
        elif self.active_run is not None and self.run_started_at is not None:
            current = self._current_left(now, full)
            if current is None:
                return Estimate()
            if full is None:
                full = child = (now - self.run_started_at) + current
            done, waits_ahead = self.active_run, 1
        else:
            done = max((run.number for run in self.finished_runs), default=0)
            current, waits_ahead = 0.0, 0 if self.cooled_after_run == done else 1
        if full is None:
            return Estimate()
        to_come = max(0, total - done)
        policy: CooldownPolicy = self.started.cooldown
        wait = policy.wait_after(child if child is not None else full).seconds
        waits = to_come if waits_ahead else max(0, to_come - 1)
        remaining = current + to_come * full + waits * wait
        elapsed = max(0.0, now - self.job_started_at)
        return Estimate(elapsed / (elapsed + remaining) if elapsed + remaining > 0 else 1.0, remaining)

    def _current_left(self, now: float, full: float | None) -> float | None:
        run = self.run_estimate(now)
        first, last = self.first_step, self.last_step
        if run.finishing or (first is not None and last is not None and last.step > first.step):
            return run.remaining if run.remaining is not None else 0.0
        if full is not None and self.run_started_at is not None:
            left = _positive(full - (now - self.run_started_at))
            return left if left is not None else run.remaining
        return run.remaining

    def _tail_left(self, now: float) -> float:
        previous = next((run for run in reversed(self.finished_runs) if run.tail_seconds is not None), None)
        if previous is None or previous.tail_seconds is None or self.last_step is None:
            return 0.0
        return max(0.0, previous.tail_seconds - (now - self.last_step.at))


def _positive(seconds: float) -> float | None:
    return seconds if seconds > 0 else None


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class FrozenReplayEstimator:
    """The frozen estimator behind the replay's interface: it learns from the history only what the old code did, the latest
    run of any job from before this one (draw-things-cli's time, and its steps), and it gives no range."""

    def __init__(self, context: JobContext) -> None:
        earlier = [run for run in context.training if run.execution < context.execution.number and run.child_seconds > 0] or [run for run in context.training if run.child_seconds > 0]
        newest = max(earlier, key=lambda run: run.order, default=None)
        steps = newest.workload.get("steps") if newest is not None else None
        past = PastRun(newest.child_seconds, steps if isinstance(steps, int) else None) if newest is not None else None
        self._estimator = FrozenEstimator(past_run=past)

    def observe(self, event: JobEvent, now: float) -> None:
        self._estimator.observe(event, now)

    def reading(self, now: float) -> Reading:
        run, job = self._estimator.run_estimate(now), self._estimator.job_estimate(now)
        return Reading(Interval(run.remaining) if run.remaining is not None else None, Interval(job.remaining) if job.remaining is not None else None, run.fraction, job.fraction)
