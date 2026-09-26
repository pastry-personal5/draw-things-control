"""How far the running job has gone and how long it has left, from the LiveRun alone: plain functions, no widgets.

A run is estimated as early as possible (owner decision), from the best measure there is at the time:

- before draw-things-cli reports a step: a past run's time (this job's last successful run, or else the latest successful
  run of any job in the state store) less the time elapsed;
- at its first step report: the past run's time per step, times the steps left, when the past run counted the same
  steps (else its time less the time elapsed, as before);
- from the second report on: the live rate, the time since the first reading divided by the steps since, so model loading
  is not counted.

The job estimate times the runs still to come by the full time of the runs this job has finished (W), and the waits
between them by those runs' draw-things-cli time (C), as the cooldown does.
"""

from __future__ import annotations

from dataclasses import dataclass

from draw_things_control.tui.live_run import FinishedRun, LiveRun, PastRun


@dataclass(frozen=True)
class Estimate:
    """A share done (0 to 1) and the seconds left; either None while it cannot be told. ``finishing``: past the last step."""

    fraction: float | None = None
    remaining: float | None = None
    finishing: bool = False


def moment(live: LiveRun) -> float:
    """The time the estimates are made at: now, or when a stop was requested, so a stopping job's bars stay still."""
    return live.stopped_at if live.stopped_at is not None else live.now()


def run_estimate(live: LiveRun, now: float) -> Estimate:
    """The active run: its share done and its seconds left, from the best measure so far; ``finishing`` after the last step."""
    first, last, past = live.first_step, live.last_step, live.reference_run()
    if last is None:
        return _before_steps(live, now, past)
    fraction = last.step / last.total
    if last.step >= last.total:
        return Estimate(1.0, _tail_left(live, now), finishing=True)
    if first is None or last.step <= first.step:
        # One reading: no rate yet, so the past run's time per step stands in for it.
        # The time per step holds only when the past run counted its steps as this counter does: an image-to-image run,
        # a refiner, or a video's segments count fewer or more than --steps. Otherwise the past run's whole time stands.
        if past is None:
            return Estimate(fraction)
        if past.steps != last.total:
            return Estimate(fraction, _past_left(live, now, past))
        return Estimate(fraction, _positive((last.total - last.step) * past.seconds / past.steps - (now - last.at)))
    rate = (last.at - first.at) / (last.step - first.step)
    return Estimate(fraction, max(0.0, (last.total - last.step) * rate - (now - last.at)))


def _before_steps(live: LiveRun, now: float, past: PastRun | None) -> Estimate:
    """Before the first step report: the past run's time less the time elapsed, and the share of it passed."""
    percent = live.percent / 100 if live.percent is not None else None
    left = _past_left(live, now, past)
    if past is None or left is None or live.run_started_at is None:
        return Estimate(percent)
    return Estimate(percent if percent is not None else (now - live.run_started_at) / past.seconds, left)


def _past_left(live: LiveRun, now: float, past: PastRun | None) -> float | None:
    """The past run's time less this run's elapsed time; None once this run has taken longer, since the past run no longer says."""
    if past is None or past.seconds <= 0 or live.run_started_at is None:
        return None
    return _positive(past.seconds - (now - live.run_started_at))


def _positive(seconds: float) -> float | None:
    """Seconds left from a past measure; None once it is used up, since the measure no longer says (``estimating``, not ``in 0 s``)."""
    return seconds if seconds > 0 else None


def job_estimate(live: LiveRun, now: float) -> Estimate:
    """The whole job: the seconds left and the share of the job's time done, cooldowns included."""
    if live.started is None or live.job_started_at is None or live.finished is not None:
        return Estimate()
    total = len(live.runs)
    succeeded = [run for run in live.finished_runs if run.status == "succeeded"]
    full = _average([run.full_seconds for run in succeeded])
    child = _average([run.seconds for run in succeeded if run.seconds is not None])
    if live.cooldown is not None and live.cooldown_ends_at is not None:
        # The countdown stands for the rest of the run it follows and the wait after it.
        current, done, waits_ahead = max(0.0, live.cooldown_ends_at - now), live.cooldown.after_run, 0
    elif live.active_run is not None and live.run_started_at is not None:
        current = _current_left(live, now, full)
        if current is None:
            return Estimate()
        if full is None:
            # Run 1: the current run is the only measure of a run so far.
            full = child = (now - live.run_started_at) + current
        done, waits_ahead = live.active_run, 1
    else:
        # Between runs: the last one has finished, and its wait is still ahead unless it has already been waited.
        done = max((run.number for run in live.finished_runs), default=0)
        current, waits_ahead = 0.0, 0 if live.cooled_after_run == done else 1
    if full is None:
        return Estimate()
    to_come = max(0, total - done)
    wait = live.started.cooldown.wait_after(child if child is not None else full).seconds
    # A wait comes before each run still to come: after the current run (if it is not the last), and after each of the others but the last.
    waits = to_come if waits_ahead else max(0, to_come - 1)
    remaining = current + to_come * full + waits * wait
    elapsed = max(0.0, now - live.job_started_at)
    return Estimate(elapsed / (elapsed + remaining) if elapsed + remaining > 0 else 1.0, remaining)


def wait_fraction(live: LiveRun, now: float) -> tuple[float, float] | None:
    """During a cooldown: the share of the wait done and the seconds left."""
    if live.cooldown is None or live.cooldown_ends_at is None:
        return None
    left = max(0.0, live.cooldown_ends_at - now)
    seconds = live.cooldown.seconds
    return (1.0 - left / seconds if seconds > 0 else 1.0), left


def last_succeeded(live: LiveRun) -> FinishedRun | None:
    """This job's last run that finished successfully; a failed run never replaces it."""
    return next((run for run in reversed(live.finished_runs) if run.status == "succeeded"), None)


def _current_left(live: LiveRun, now: float, full: float | None) -> float | None:
    """The active run's seconds left, for the job: the run's own estimate once it has a live rate or is finishing; before
    that, W less its elapsed time from run 2 on (W counts the tail after draw-things-cli, which a past run's time leaves
    out), and on run 1 the run's estimate from a past run."""
    run = run_estimate(live, now)
    first, last = live.first_step, live.last_step
    if run.finishing or (first is not None and last is not None and last.step > first.step):
        return run.remaining if run.remaining is not None else 0.0
    if full is not None and live.run_started_at is not None:
        left = _positive(full - (now - live.run_started_at))
        # A run that has taken longer than W falls back to its own estimate, which says estimating rather than 0 s.
        return left if left is not None else run.remaining
    return run.remaining


def _tail_left(live: LiveRun, now: float) -> float:
    """After the last step: how long the previous run took from its last step to its end, less the time spent so far."""
    previous = next((run for run in reversed(live.finished_runs) if run.tail_seconds is not None), None)
    if previous is None or previous.tail_seconds is None or live.last_step is None:
        return 0.0
    return max(0.0, previous.tail_seconds - (now - live.last_step.at))


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
