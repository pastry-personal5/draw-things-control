"""The Status widget and the status line: a job's progress, its bars, and its estimated end."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text

from draw_things_control.core.yaml_files import is_yaml_file
from draw_things_control.jobs.text import duration_text
from draw_things_control.services.queue_hold import HoldState
from draw_things_control.services.queue_park_text import park_point
from draw_things_control.tui.estimate import Estimate, job_estimate, last_succeeded, moment, run_estimate, wait_fraction
from draw_things_control.tui.live_run import LiveRun
from draw_things_control.tui.preferences import VerboseLevel
from draw_things_control.tui.text.common import STATUS_STYLE

PHASE_TEXT = {"starting": "starting", "running": "running", "cooling_down": "cooling down", "stopping": "stopping", "parking": "parking", "finished": "finished", "not_started": "did not start", "ended": "ended (lost track)"}
# The phases the Status widget and the run line show in yellow: something is about to change.
_NOTICE_PHASES = ("starting", "stopping", "parking", "not_started", "ended")


def progress_text(live: LiveRun) -> str | None:
    """Step progress as ``3/8, 37%``, or None before the child reports any."""
    parts = []
    if live.progress is not None:
        parts.append(f"{live.progress[0]}/{live.progress[1]}")
    if live.percent is not None:
        parts.append(f"{live.percent}%")
    return ", ".join(parts) or None


def cooldown_text(live: LiveRun) -> Text:
    """Seconds left in the cooldown and the local time it ends; empty when not cooling down."""
    if live.cooldown is None or live.cooldown_ends_at is None:
        return Text()
    left = max(0, math.ceil(live.cooldown_ends_at - live.now()))
    text = Text("Cooldown", style="bold")
    text.append(f" before run {live.cooldown.after_run + 1}: {duration_text(left)} left, until {live.cooldown.until}")
    return text


def run_line_text(live: LiveRun | None, level: VerboseLevel = "high") -> Text:
    """The draw-things-cli pane's run line: the active run, the cooldown, or what the job is doing. At verbose low no
    progress is shown: it arrives only as run output, which low never receives, so it would freeze at a stale reading."""
    if live is None:
        return Text("No job has run in this session", style="dim")
    if live.active_run is not None and live.run_started_at is not None:
        elapsed = max(0.0, live.now() - live.run_started_at)
        text = Text(f"Run {live.active_run}/{len(live.runs)}", style="bold")
        text.append(f"  {duration_text(int(elapsed))} elapsed")
        progress = progress_text(live) if level != "low" else None
        if progress is not None:
            text.append("  progress ", style="bold")
            text.append(progress)
        text.append(f"  {live.active_output}", style="dim")
        if live.stop_requested:
            text.append("  stopping", style="bold yellow")
        elif live.park_requested:
            text.append("  parking", style="bold yellow")
        return text
    cooldown = cooldown_text(live)
    if cooldown:
        return cooldown
    return Text(f"{live.job_name}: {PHASE_TEXT[live.phase]}", style="bold yellow" if live.phase in ("starting", "stopping", "parking") else "dim")


BAR_DONE = "█"


BAR_LEFT = "░"


OTHER_PROCESS_TEXT = "A job is running in another process"


def whole_duration(seconds: float) -> str:
    """A duration in whole seconds, as the Status widget writes it: ``7 min 12 s``, never tenths."""
    return duration_text(max(0, round(seconds)))


def end_text(remaining: float, now: datetime) -> str:
    """When something left ``remaining`` seconds from ``now`` ends: ``ends ~16:42 (in 23 min)``, dated when it is another day."""
    end = now + timedelta(seconds=remaining)
    clock = end.strftime("%H:%M") if end.date() == now.date() else end.strftime("%m-%d %H:%M")
    return f"ends ~{clock} (in {whole_duration(remaining)})"


def bar_line(label: str, fraction: float | None, tail: str, width: int) -> Text:
    """``label``, a bar, its percentage, and ``tail``; the text is always whole, and the bar takes what is left, down to nothing."""
    percent = f"{math.floor(fraction * 100)}%" if fraction is not None else ""
    words = [part for part in (percent, tail) if part]
    # One space after the label and after the bar, two between the words: ``Job ████░░ 38%  ends ~16:42 (in 23 min)``.
    text_width = len(label) + sum(len(word) + (1 if index == 0 else 2) for index, word in enumerate(words))
    room = max(0, width - text_width - 1)
    text = Text(label, style="bold")
    if room:
        done = round(room * min(1.0, max(0.0, fraction))) if fraction is not None else 0
        text.append(" ")
        text.append(BAR_DONE * done, style="cyan")
        text.append(BAR_LEFT * (room - done), style="dim")
    for index, word in enumerate(words):
        text.append(" " if index == 0 else "  ")
        text.append(word, style="dim" if word in ("estimating", "stopping", "parking") else "")
    return text


def held_text(hold: HoldState, now: datetime) -> Text:
    """``Queue held since 12:04 (by Q0007)``, dated when the hold began on another day; empty when not held."""
    if not hold.held:
        return Text()
    text = Text("Queue held", style="bold yellow")
    if hold.since is not None:
        since = datetime.fromisoformat(hold.since).astimezone()
        text.append(f" since {since.strftime('%H:%M') if since.date() == now.date() else since.strftime('%Y-%m-%d %H:%M')}")
    if hold.by is not None:
        text.append(f" (by {hold.by})")
    return text


def status_lines(live: LiveRun | None, other_process: bool, width: int, *, message: str | None = None, hold: HoldState | None = None, now: datetime | None = None) -> list[Text]:
    """The Status widget's five lines: the phase, the job bar, the run (or wait) bar, the details, and the last run.

    ``message`` is the busy message the run lock's holder would give (an ordinary ``generate`` holder, or the
    server itself when the gRPC feed cannot reach it); without one, a generic line is shown. ``dtc serve`` holds
    the run lock for its whole lifetime, so ``other_process`` is only ever true while the feed itself is down.

    ``hold`` is the queue's (Milestone 05). While no job runs it is shown: as the first line with nothing run this
    session, and otherwise as the third, which those layouts leave empty. ``now`` is the local time it is read at.
    """
    lines = [Text() for _ in range(5)]
    held = held_text(hold, now or datetime.now()) if hold is not None else Text()
    if other_process and (live is None or live.ended):
        lines[0] = Text(message or OTHER_PROCESS_TEXT, style="bold yellow")
        lines[2] = held
        return lines
    if live is None:
        lines[0] = held
        return lines
    lines[0] = _phase_line(live)
    last = last_succeeded(live)
    if last is not None and last.seconds is not None:
        lines[4] = Text(f"last run took {whole_duration(last.seconds)}")
    if live.ended or live.finished is not None:
        lines = _finished_lines(live, lines)
        lines[2] = held
        return lines
    if live.started is None:
        return lines
    # Once a stop is requested, moment() stays at that time, so the bars stay where the stop found them. A park
    # reservation does not freeze them, since the run goes on; the Job bar reads "parking" in place of its end.
    now_moment, wall = moment(live), live.wall_now()
    lines[1] = bar_line("Job", *_bar_parts(live, job_estimate(live, now_moment), wall, "parking" if live.park_requested else None), width)
    waiting = wait_fraction(live, now_moment)
    if waiting is not None and live.cooldown is not None:
        fraction, left = waiting
        lines[2] = bar_line("Wait", fraction, "stopping" if live.stop_requested else end_text(left, wall), width)
        lines[3] = Text(f"next: run {live.cooldown.after_run + 1}/{len(live.runs)}")
    elif live.active_run is not None:
        run = run_estimate(live, now_moment)
        tail = "finishing" if run.finishing else None
        lines[2] = bar_line("Run", *_bar_parts(live, run, wall, tail), width)
        lines[3] = _details_line(live, now_moment)
    return lines


def _finished_lines(live: LiveRun, lines: list[Text]) -> list[Text]:
    if live.finished is not None:
        finished = live.finished
        lines[1] = Text(f"{finished.completed_runs}/{finished.total_runs} runs succeeded")
        if live.started is not None:
            took = (datetime.fromisoformat(finished.at) - datetime.fromisoformat(live.started.at)).total_seconds()
            lines[3] = Text(f"job took {whole_duration(took)}")
    return lines


def _bar_parts(live: LiveRun, estimate: Estimate, wall: datetime, tail: str | None = None) -> tuple[float | None, str]:
    if live.stop_requested:
        return estimate.fraction, "stopping"
    if tail is not None:
        return estimate.fraction, tail
    if estimate.remaining is None:
        return estimate.fraction, "estimating"
    return estimate.fraction, end_text(estimate.remaining, wall)


def _phase_line(live: LiveRun) -> Text:
    if live.finished is not None:
        text = Text("finished (", style="bold")
        text.append(live.finished.status, style=STATUS_STYLE.get(live.finished.status, "bold"))
        text.append(")", style="bold")
    else:
        phase = live.phase
        text = Text(_parking_text(live) if phase == "parking" else PHASE_TEXT[phase], style="bold yellow" if phase in _NOTICE_PHASES else "bold cyan")
    # The execution's ID once the state store has recorded it, as the history and every message write it: ``E0012: walk``.
    text.append(f"  {live.execution_id}: " if live.execution_id is not None else "  ")
    if live.path is not None:
        text.append(job_display_name(live.path))
    if live.finished is None and not live.ended and live.phase != "parking":
        if live.active_run is not None:
            text.append(f"  run {live.active_run}/{len(live.runs)}")
        elif live.cooldown is not None:
            text.append(f"  after run {live.cooldown.after_run}/{len(live.runs)}")
    return text


def _parking_text(live: LiveRun) -> str:
    """``parking after run 3/7``: the run in progress, or the one a cooldown follows, which the park ends at once; or
    ``parking on its last run``, which then finishes (``park_point``)."""
    run = live.active_run
    if run is None and live.cooldown is not None:
        run = live.cooldown.after_run
    point = park_point(run, len(live.runs))
    return f"parking after {point}" if point is not None else "parking on its last run"


def job_display_name(path: Path) -> str:
    """A job file's name as the Status widget shows it: without its YAML suffix (``walk``), any other suffix kept."""
    return path.stem if is_yaml_file(path) else path.name


def _details_line(live: LiveRun, now: float) -> Text:
    parts = []
    if live.last_step is not None:
        parts.append(f"step {live.last_step.step}/{live.last_step.total}")
    elif live.percent is not None:
        parts.append(f"{live.percent}%")
    if live.run_started_at is not None:
        parts.append(f"{whole_duration(now - live.run_started_at)} elapsed")
    return Text("  ".join(parts))


def status_line_text(data_directory: Path, live: LiveRun | None, running: bool, quit_armed: bool = False) -> Text:
    """The status line: the running or last job, the data directory, and where help is; or, after one Ctrl-C, how to quit."""
    if quit_armed:
        return Text(" Press Ctrl-C again to quit", style="bold yellow")
    text = Text(" ")
    if live is None:
        text.append("idle", style="dim")
    else:
        text.append(live.path.name if live.path is not None else "?", style="bold")
        text.append(f" {PHASE_TEXT[live.phase]}", style="bold cyan" if running else "")
        if running and live.active_run is not None:
            text.append(f" run {live.active_run}/{len(live.runs)}")
        elif not running and live.finished is not None:
            text.append(f" ({live.finished.status})", style=STATUS_STYLE.get(live.finished.status, ""))
    text.append("  |  ", style="dim")
    text.append(str(data_directory))
    text.append("  |  /help: commands, Ctrl-C twice: quit", style="dim")
    return text
