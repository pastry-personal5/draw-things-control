"""A job's events as the lines Messages shows, and its result."""

from __future__ import annotations

import math

from rich.text import Text

from draw_things_control.jobs.events import CooldownEnded, CooldownStarted, JobEvent, JobStarted, JobStatus, MediaChecked, RunFinished, RunStarted, RunStatus
from draw_things_control.jobs.text import auto_wait_text, duration_text, media_check_text, seconds_text
from draw_things_control.tui.live_run import LiveRun
from draw_things_control.tui.text.arguments import Arguments, PreviousRun, arguments_text
from draw_things_control.tui.text.common import STATUS_STYLE
from draw_things_control.tui.text.prompts import prompt_block


def event_text(event: JobEvent, arguments: Arguments | None = None, previous: PreviousRun | None = None) -> Text | None:
    """The job log's line for an event, or None for events the log leaves out (the child's output). A run shows its
    argument rows (``argument_rows`` of its command), and after an earlier run (``previous``) only what changed since it."""
    if isinstance(event, JobStarted):
        text = Text(f"Job started: {event.job_name}", style="bold")
        text.append(f" ({event.mode}, {event.total_runs} run{'s' if event.total_runs != 1 else ''}, seed {event.seed} ({event.seed_source}), model {event.model})")
        return text
    if isinstance(event, RunStarted):
        # The prompts, then the arguments as a table: never the command line, whose --config-json is bare JSON.
        text = Text(f"Run {event.number}/{event.total} started", style="bold")
        text.append(f" (pair {event.pair}): {event.output}\n")
        prompt_block(text, "positive", "green", event.positive)
        prompt_block(text, "negative", "red", event.negative)
        if arguments is not None:
            text.append("\n")
            text.append_text(arguments_text(arguments, previous))
        text.rstrip()
        return text
    if isinstance(event, RunFinished):
        text = Text(f"Run {event.number} ")
        text.append(event.status, style=STATUS_STYLE.get(event.status, "bold"))
        if event.seconds is not None:
            text.append(f" in {seconds_text(event.seconds)}")
        if event.exit_code not in (None, 0):
            text.append(f", exit code {event.exit_code}")
        if event.output is not None:
            text.append(f": {event.output}")
        return text
    if isinstance(event, CooldownStarted):
        if event.mode == "auto" and event.ratio is not None and event.run_seconds is not None:
            wait = auto_wait_text(event.seconds, event.ratio, event.after_run, event.run_seconds, event.bound)
        else:
            wait = duration_text(math.ceil(event.seconds))
        return Text(f"Cooldown {wait} before run {event.after_run + 1}, until {event.until}")
    if isinstance(event, MediaChecked):
        return Text(media_check_text(event), style="yellow" if event.verdict == "warning" else "")
    if isinstance(event, CooldownEnded):
        return Text(f"Cooldown cut short after {seconds_text(event.waited_seconds)}", style="yellow") if event.cut_short else None
    return None


def result_text(live: LiveRun) -> Text:
    """How the job ended, or why it did not start; empty while it runs."""
    if live.finished is None:
        if not live.ended:
            return Text()
        # No JobFinished ever arrived to say how it ended: the entry's own error (it failed before JobStarted), or,
        # with none, the run was simply lost track of (a server crash or restart, or a backlog gap that became a
        # Reset before the finish was replayed).
        return Text(f"Ended: {live.error}" if live.error else "Ended without a result (lost track of this run)", style="red")
    finished = live.finished
    text = Text("Job ", style="bold")
    text.append(finished.status, style=STATUS_STYLE.get(finished.status, "bold"))
    text.append(f": {finished.completed_runs}/{finished.total_runs} runs completed, exit code {finished.exit_code if finished.exit_code is not None else '-'}")
    if finished.signal is not None:
        text.append(f", stopped by {finished.signal}")
    if finished.status == JobStatus.PARKED:
        text.append_text(_parked_text(live))
    if live.error is not None:
        text.append(f"\n{live.error}", style="red")
    if live.started is not None and live.started.manifest is not None:
        text.append("\n  manifest: ", style="bold")
        text.append(live.started.manifest)
    if live.started is not None and live.started.log is not None:
        text.append("\n  log: ", style="bold")
        text.append(live.started.log)
    return text


def _parked_text(live: LiveRun) -> Text:
    """How to go on from a parked job: the run number is the chain's, from its last succeeded run, not
    ``JobFinished.completed_runs``, which counts only this execution's runs."""
    last = max((run.number for run in live.runs if run.status == RunStatus.SUCCEEDED), default=0)
    name = live.queue_id or "The job"
    resume = f" ('/queue resume {live.queue_id}' continues at run {last + 1})" if live.queue_id is not None else ""
    return Text(f"\n{name} parked after run {last}/{len(live.runs)}{resume}", style="blue")
