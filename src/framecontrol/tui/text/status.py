"""The work status and the status line: a job's progress, its bars, and its estimated end."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from rich.text import Text

from framecontrol.core.yaml_files import is_yaml_file
from framecontrol.jobs.events import RunStatus
from framecontrol.jobs.text import duration_text
from framecontrol.services.forecast_view import end_text, progress, seconds_left, show_range, stage_text, whole_duration
from framecontrol.services.queue_hold import HoldState
from framecontrol.services.queue_park_text import park_point
from framecontrol.tui.live_run import FinishedRun, LiveRun
from framecontrol.tui.preferences import VerboseLevel
from framecontrol.tui.text.common import STATUS_STYLE

PHASE_TEXT = {"starting": "starting", "running": "running", "cooling_down": "cooling down", "stopping": "stopping", "parking": "parking", "finished": "finished", "not_started": "did not start", "ended": "ended (lost track)"}
# The phases the work status and the run summary show in yellow: something is about to change.
_NOTICE_PHASES = ("starting", "stopping", "parking", "not_started", "ended")


def moment(live: LiveRun) -> float:
    """The time the elapsed seconds are read at: now, or when a stop was requested, so a stopping job's bars stay still."""
    return live.stopped_at if live.stopped_at is not None else live.now()


def wait_fraction(live: LiveRun, now: float) -> tuple[float, float] | None:
    """During a cooldown: the share of the wait done and the seconds left."""
    if live.cooldown is None or live.cooldown_ends_at is None:
        return None
    left = max(0.0, live.cooldown_ends_at - now)
    seconds = live.cooldown.seconds
    return (1.0 - left / seconds if seconds > 0 else 1.0), left


def last_succeeded(live: LiveRun) -> FinishedRun | None:
    """This job's last run that finished successfully; a failed run never replaces it."""
    return next((run for run in reversed(live.finished_runs) if run.status == RunStatus.SUCCEEDED), None)


def cooldown_text(live: LiveRun) -> Text:
    """Seconds left in the cooldown and the local time it ends; empty when not cooling down."""
    if live.cooldown is None or live.cooldown_ends_at is None:
        return Text()
    left = max(0, math.ceil(live.cooldown_ends_at - live.now()))
    text = Text("Cooldown", style="bold")
    text.append(f" before run {live.cooldown.after_run + 1}: {duration_text(left)} left, until {live.cooldown.until}")
    return text


def run_summary_text(live: LiveRun | None, level: VerboseLevel = "high") -> Text:
    """The generation output's run summary: the active run, the cooldown, or what the job is doing. The stage and its units
    come from the server's forecast, which low receives too, so they are shown at every verbose level."""
    if live is None:
        return Text("No job has run in this session", style="dim")
    if live.active_run is not None and live.run_started_at is not None:
        elapsed = max(0.0, live.now() - live.run_started_at)
        text = Text(f"Run {live.active_run}/{len(live.runs)}", style="bold")
        text.append(f"  {duration_text(int(elapsed))} elapsed")
        stage = stage_text(live.forecast)
        if stage is not None:
            text.append("  stage ", style="bold")
            text.append(stage)
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


def work_status_lines(live: LiveRun | None, other_process: bool, width: int, *, message: str | None = None, hold: HoldState | None = None, now: datetime | None = None) -> list[Text]:
    """The work status's five lines: the phase, the job bar, the run (or wait) bar, the details, and the last run.

    ``message`` is the busy message the run lock's holder would give (an ordinary ``generate`` holder, or the
    server itself when the gRPC feed cannot reach it); without one, a generic line is shown. ``fc serve`` holds
    the run lock for its whole lifetime, so ``other_process`` is only ever true while the feed itself is down.

    ``hold`` is the queue's. While no job runs it is shown: as the first line with nothing run this
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
    # Once a stop is requested, the forecast is read at that moment, so the bars stay where the stop found them. A park
    # reservation does not freeze them, since the run goes on; the Job bar reads "parking" in place of its end.
    now_moment, shown = moment(live), live.shown_at()
    forecast = live.forecast or {}
    lines[1] = bar_line("Job", *_bar_parts(live, forecast.get("job"), shown, "parking" if live.park_requested else None), width)
    waiting = wait_fraction(live, now_moment)
    if waiting is not None and live.cooldown is not None:
        fraction, left = waiting
        lines[2] = bar_line("Wait", fraction, "stopping" if live.stop_requested else end_text(left, live.wall_now()), width)
        lines[3] = Text(f"next: run {live.cooldown.after_run + 1}/{len(live.runs)}")
    elif live.active_run is not None:
        lines[2] = bar_line("Run", *_bar_parts(live, forecast.get("run"), shown), width)
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


def _bar_parts(live: LiveRun, span: dict[str, float] | None, shown: float, tail: str | None = None) -> tuple[float | None, str]:
    """The share done and the words after it, from a published span read at ``shown`` (epoch seconds): its end, with the
    range when that is wide enough to mention; ``estimating`` when the server gave no number."""
    if span is None:
        return None, "stopping" if live.stop_requested else tail or "estimating"
    fraction = progress(span, shown)
    if live.stop_requested:
        return fraction, "stopping"
    if tail is not None:
        return fraction, tail
    left = seconds_left(span, shown)
    if left <= 0:
        return fraction, "finishing"
    bounds = (max(0.0, span["low"] - shown), max(0.0, span["high"] - shown)) if show_range(span, shown) else None
    return fraction, end_text(left, datetime.fromtimestamp(shown), bounds)


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
    """A job file's name as the work status shows it: without its YAML suffix (``walk``), any other suffix kept."""
    return path.stem if is_yaml_file(path) else path.name


def _details_line(live: LiveRun, now: float) -> Text:
    parts = []
    stage = stage_text(live.forecast)
    if stage is not None:
        parts.append(stage)
    if live.run_started_at is not None:
        parts.append(f"{whole_duration(now - live.run_started_at)} elapsed")
    return Text("  ".join(parts))


def status_line_text(data_directory: Path, live: LiveRun | None, running: bool, quit_armed: bool = False, *, shortcut_help: bool = False) -> Text:
    """The status line: job state, data directory, and help; or, after one Ctrl-C, how to quit."""
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
    if shortcut_help:
        text.append("  |  Press ? for shortcut help", style="dim")
    text.append("  |  /help: commands, Ctrl-C twice: quit", style="dim")
    return text
