"""The text the TUI shows; job text comes from jobs.job_report, so it matches the CLI. Plain functions, no widgets."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rich.style import Style
from rich.text import Text

from draw_things_control.core.draw_things_arguments import CommandSettings, command_arguments, command_settings, config_json
from draw_things_control.core.global_config import parse_cooldown
from draw_things_control.core.numbers import setting_number
from draw_things_control.jobs.job_definition import GenerationMode, JobDefinition
from draw_things_control.jobs.job_events import CooldownEnded, CooldownStarted, JobEvent, JobStarted, RunFinished, RunStarted
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED_NOTE, RANDOM_SEED_TEXT, auto_wait_text, cooldown_details, duration_text, ignored_config_lines, job_summary, pair_runs, policy_text, seconds_text
from draw_things_control.tui.estimate import Estimate, job_estimate, last_succeeded, moment, run_estimate, wait_fraction
from draw_things_control.tui.history import is_imported, run_file
from draw_things_control.tui.live_run import LiveRun

PHASE_TEXT = {"starting": "starting", "running": "running", "cooling_down": "cooling down", "stopping": "stopping", "finished": "finished", "not_started": "did not start"}
VIDEO_MODES = {mode.value for mode in GenerationMode if mode.is_video}
STATUS_STYLE = {"running": "bold cyan", "succeeded": "green", "failed": "red", "timed_out": "red", "interrupted": "yellow", "pending": "dim"}


def history_cells(row: dict[str, Any]) -> tuple[Text, ...]:
    """The pane's row: ID, job name, status, start time, and runs succeeded of total."""
    try:
        started = datetime.fromisoformat(row["started_at"]).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        started = str(row["started_at"])
    total = row["total_runs"] if row["total_runs"] is not None else "?"
    status = row["status"]
    return (Text(str(row["id"])), Text(row["job_name"]), Text(status, style=STATUS_STYLE.get(status, "")), Text(started), Text(f"{row.get('succeeded', 0)}/{total}"))


def jobs_text(rows: list[tuple[str, JobDefinition | None, str | None]], running: str | None, message: str | None) -> Text:
    """The job files: name, job name, mode, runs, and status (valid, running, or the first error)."""
    if message is not None:
        return Text(message, style="yellow")
    text = Text("Jobs\n", style="bold")
    width = max(len(name) for name, _, _ in rows)
    for name, job, error in rows:
        text.append(f"  {name.ljust(width)}  ", style="bold")
        if job is None:
            text.append(f"invalid: {error}\n", style="red")
            continue
        text.append(f"{job.name}  {job.mode}  {job.run_count} run{'s' if job.run_count != 1 else ''}  ")
        text.append("running\n" if name == running else "valid\n", style="bold cyan" if name == running else "green")
    text.rstrip()
    return text


def summary_text(job: JobDefinition) -> Text:
    """validate-job's summary, with a random seed described as the plan uses it, and any ignored configuration."""
    text = Text()
    text.append("Job file: ", style="bold")
    text.append(f"{job.path}\n")
    for label, value in job_summary(job, random_seed_text=RANDOM_SEED_TEXT):
        text.append(f"  {label}: ", style="bold")
        text.append(f"{value}\n")
    warnings = ignored_config_lines(job)
    if warnings:
        text.append("\nNotes\n", style="bold")
        for line in warnings:
            text.append(f"  {line}\n")
    return text


def pairs_text(job: JobDefinition) -> Text:
    """Each prompt pair and the runs that use it."""
    text = Text("Prompt pairs\n", style="bold")
    for pair, runs in pair_runs(job):
        used = ", ".join(str(number) for number in runs) if runs else "none"
        text.append(f"\n{pair.name}", style="bold")
        text.append(f"{' (default)' if pair.default else ''}: run{'s' if len(runs) != 1 else ''} {used}\n")
        text.append("  positive: ", style="green")
        text.append(f"{pair.positive.strip()}\n")
        if pair.negative:
            text.append("  negative: ", style="red")
            text.append(f"{pair.negative.strip()}\n")
    return text


def plan_text(job: JobDefinition, lines: tuple[str, ...] | None, error: str | None) -> Text:
    """The dry-run plan, or the reason it cannot be made."""
    text = Text("Dry-run plan\n\n", style="bold")
    if error is not None:
        text.append(error, style="red")
        return text
    if job.configured_seed()[0] is None:
        text.append(f"{PLACEHOLDER_SEED_NOTE}\n\n", style="yellow")
    for line in lines or ():
        text.append(f"{line}\n", style="dim" if line.startswith("#") else "")
    return text


def details_text(job: JobDefinition, lines: tuple[str, ...] | None, error: str | None) -> Text:
    """What show prints: the summary, the prompt pairs, and the dry-run plan."""
    text = summary_text(job)
    text.append("\n")
    text.append_text(pairs_text(job))
    text.append("\n")
    text.append_text(plan_text(job, lines, error))
    text.rstrip()
    return text


def confirm_run_text(job: JobDefinition, executable: str) -> Text:
    """The run confirmation: what will run, where it writes, and with which executable."""
    seed, source = job.configured_seed()
    text = Text("Run this job?\n\n", style="bold")
    for label, value in (
        ("Job", job.name),
        ("Mode", str(job.mode)),
        ("Runs", str(job.run_count)),
        ("Cooldown", cooldown_details(job)),
        ("Seed", f"{seed} ({source})" if seed is not None else RANDOM_SEED_TEXT),
        ("Output directory", str(job.output_directory)),
        ("Executable", executable),
    ):
        text.append(f"  {label}: ", style="bold")
        text.append(f"{value}\n")
    # Not Enter: the Enter that submitted /run must not also answer this.
    text.append("\ny: run    n or Escape: cancel", style="dim")
    return text


def question_text(question: str, confirm: str, cancel: str) -> Text:
    """A yes-or-no question with its keys."""
    text = Text(f"{question}\n\n", style="bold")
    text.append(f"y or Enter: {confirm}    n or Escape: {cancel}", style="dim")
    return text


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


def run_line_text(live: LiveRun | None) -> Text:
    """The draw-things-cli pane's run line: the active run, the cooldown, or what the job is doing."""
    if live is None:
        return Text("No job has run in this session", style="dim")
    if live.active_run is not None and live.run_started_at is not None:
        elapsed = max(0.0, live.now() - live.run_started_at)
        text = Text(f"Run {live.active_run}/{len(live.runs)}", style="bold")
        text.append(f"  {duration_text(int(elapsed))} elapsed")
        progress = progress_text(live)
        if progress is not None:
            text.append("  progress ", style="bold")
            text.append(progress)
        text.append(f"  {live.active_output}", style="dim")
        if live.stop_requested:
            text.append("  stopping", style="bold yellow")
        return text
    cooldown = cooldown_text(live)
    if cooldown:
        return cooldown
    return Text(f"{live.job_name}: {PHASE_TEXT[live.phase]}", style="bold yellow" if live.phase in ("starting", "stopping") else "dim")


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
        text.append(word, style="dim" if word in ("estimating", "stopping") else "")
    return text


def status_lines(live: LiveRun | None, other_process: bool, width: int) -> list[Text]:
    """The Status widget's five lines: the phase, the job bar, the run (or wait) bar, the details, and the last run."""
    lines = [Text() for _ in range(5)]
    if other_process and (live is None or live.worker_ended):
        lines[0] = Text(OTHER_PROCESS_TEXT, style="bold yellow")
        return lines
    if live is None:
        return lines
    lines[0] = _phase_line(live)
    last = last_succeeded(live)
    if last is not None and last.seconds is not None:
        lines[4] = Text(f"last run took {whole_duration(last.seconds)}")
    if live.worker_ended or live.finished is not None:
        if live.finished is not None:
            finished = live.finished
            lines[1] = Text(f"{finished.completed_runs}/{finished.total_runs} runs succeeded")
            if live.started is not None:
                took = (datetime.fromisoformat(finished.at) - datetime.fromisoformat(live.started.at)).total_seconds()
                lines[3] = Text(f"job took {whole_duration(took)}")
        return lines
    if live.started is None:
        return lines
    # Once a stop is requested, moment() stays at that time, so the bars stay where the stop found them.
    now, wall = moment(live), live.wall_now()
    lines[1] = bar_line("Job", *_bar_parts(live, job_estimate(live, now), wall), width)
    waiting = wait_fraction(live, now)
    if waiting is not None and live.cooldown is not None:
        fraction, left = waiting
        lines[2] = bar_line("Wait", fraction, "stopping" if live.stop_requested else end_text(left, wall), width)
        lines[3] = Text(f"next: run {live.cooldown.after_run + 1}/{len(live.runs)}")
    elif live.active_run is not None:
        run = run_estimate(live, now)
        tail = "finishing" if run.finishing else None
        lines[2] = bar_line("Run", *_bar_parts(live, run, wall, tail), width)
        lines[3] = _details_line(live, now)
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
        text = Text(PHASE_TEXT[phase], style="bold yellow" if phase in ("starting", "stopping", "not_started") else "bold cyan")
    text.append(f"  {live.path.name}")
    if live.finished is None and not live.worker_ended:
        if live.active_run is not None:
            text.append(f"  run {live.active_run}/{len(live.runs)}")
        elif live.cooldown is not None:
            text.append(f"  after run {live.cooldown.after_run}/{len(live.runs)}")
    return text


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
        text.append(live.path.name, style="bold")
        text.append(f" {PHASE_TEXT[live.phase]}", style="bold cyan" if running else "")
        if running and live.active_run is not None:
            text.append(f" run {live.active_run}/{len(live.runs)}")
        elif not running and live.finished is not None:
            text.append(f" ({live.finished.status})", style=STATUS_STYLE.get(live.finished.status, ""))
    text.append("  |  ", style="dim")
    text.append(str(data_directory))
    text.append("  |  /help: commands, Ctrl-C twice: quit", style="dim")
    return text


def event_text(event: JobEvent) -> Text | None:
    """The job log's line for an event, or None for events the log leaves out (the child's output)."""
    if isinstance(event, JobStarted):
        text = Text(f"Job started: {event.job_name}", style="bold")
        text.append(f" ({event.mode}, {event.total_runs} run{'s' if event.total_runs != 1 else ''}, seed {event.seed} ({event.seed_source}), model {event.model})")
        return text
    if isinstance(event, RunStarted):
        text = Text(f"Run {event.number}/{event.total} started", style="bold")
        text.append(f" (pair {event.pair}): {event.output}\n  command: ")
        text.append(" ".join(event.command), style="dim")
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
    if isinstance(event, CooldownEnded):
        return Text(f"Cooldown cut short after {seconds_text(event.waited_seconds)}", style="yellow") if event.cut_short else None
    return None


def result_text(live: LiveRun) -> Text:
    """How the job ended, or why it did not start; empty while it runs."""
    if live.finished is None:
        if not live.worker_ended:
            return Text()
        return Text(f"Did not start: {live.error}", style="red")
    finished = live.finished
    text = Text("Job ", style="bold")
    text.append(finished.status, style=STATUS_STYLE.get(finished.status, "bold"))
    text.append(f": {finished.completed_runs}/{finished.total_runs} runs completed, exit code {finished.exit_code if finished.exit_code is not None else '-'}")
    if finished.signal is not None:
        text.append(f", stopped by {finished.signal}")
    if live.error is not None:
        text.append(f"\n{live.error}", style="red")
    if live.started is not None and live.started.manifest is not None:
        text.append("\n  manifest: ", style="bold")
        text.append(live.started.manifest)
    if live.started is not None and live.started.log is not None:
        text.append("\n  log: ", style="bold")
        text.append(live.started.log)
    return text


def file_text(execution: dict[str, Any], name: str | None) -> str:
    """A run's file as a full path, marked when it no longer exists."""
    path = run_file(execution, name)
    if path is None:
        return name or "-"
    return str(path) if path.exists() else f"{path} (missing)"


def stored_cooldown_text(execution: dict[str, Any]) -> str:
    """An execution's cooldown and source, from the resolved mapping when it was recorded, else the old seconds, which mean manual."""
    source = f"({execution.get('cooldown_source') or '-'})"
    mapping = (execution.get("settings") or {}).get("cooldown")
    if isinstance(mapping, dict):
        try:
            return f"{policy_text(parse_cooldown(mapping, 'cooldown'))} {source}"
        except ValueError:
            pass
    seconds = execution.get("cooldown_seconds")
    return f"{seconds_text(seconds)} {source}" if seconds is not None else "-"


def execution_text(execution: dict[str, Any]) -> Text:
    """One execution as it ran, from the stored row, and each of its runs; never the current job file."""
    text = Text(f"Execution {execution['id']}: {execution['job_name']}", style="bold")
    if is_imported(execution):
        text.append("  imported", style="yellow")
    text.append("\n")
    signal = f", stopped by {execution['signal']}" if execution.get("signal") else ""
    settings = execution_settings(execution)
    for label, value in (
        ("status", f"{execution['status']}, exit code {execution['exit_code'] if execution['exit_code'] is not None else '-'}{signal}"),
        ("job file", execution["job_file"]),
        ("mode", execution["mode"]),
        ("model", execution.get("model") or "-"),
        ("refiner", refiner_text(settings)),
        ("CFG", number_text(settings.cfg)),
        ("shift", number_text(settings.shift)),
        ("seed", f"{execution['seed']} ({execution.get('seed_source') or '-'})" if execution.get("seed") is not None else "-"),
        ("cooldown", stored_cooldown_text(execution)),
        ("started", execution["started_at"]),
        ("finished", execution.get("finished_at") or "-"),
        ("manifest", execution.get("manifest_path") or "-"),
        ("log", execution.get("log_path") or "-"),
    ):
        text.append(f"  {label}: ", style="bold")
        text.append(f"{value}\n")
    if execution.get("recovered_at"):
        text.append(f"  closed as interrupted at {execution['recovered_at']}, after the process that ran it ended\n", style="yellow")
    for run in execution["runs"]:
        text.append(f"\nRun {run['number']} ", style="bold")
        text.append(run["status"], style=STATUS_STYLE.get(run["status"], ""))
        seconds = f", {seconds_text(run['seconds'])}" if run.get("seconds") is not None else ""
        text.append(f" (pair {run['pair']}{seconds}, exit code {run['exit_code'] if run.get('exit_code') is not None else '-'})\n")
        text.append("  steps: ", style="bold")
        text.append(f"{run_steps(run) or '-'}, output {output_measure_text(run)}\n")
        text.append("  positive: ", style="green")
        text.append(f"{run['positive'].strip()}\n")
        if run.get("negative"):
            text.append("  negative: ", style="red")
            text.append(f"{run['negative'].strip()}\n")
        for label, value in (("input", run.get("input") or "-"), ("output", file_text(execution, run.get("output"))), ("last frame", file_text(execution, run.get("last_frame")) if run.get("last_frame") else None)):
            if value is not None:
                text.append(f"  {label}: ", style="bold")
                text.append(f"{value}\n")
        if run.get("cooldown_after_seconds") is not None:
            text.append("  cooldown after: ", style="bold")
            text.append(f"{seconds_text(run['cooldown_after_seconds'])}\n")
        if run.get("command"):
            text.append("  command: ", style="bold")
            text.append(f"{' '.join(run['command'])}\n", style="dim")
    text.rstrip()
    return text


def cut_middle(text: str, width: int) -> str:
    """``text`` cut in the middle with ``…`` to fit ``width``, so its start and its end (a file's suffix) both show."""
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    head = (width - 1 + 1) // 2
    return text[:head] + "…" + text[len(text) - (width - 1 - head) :]


def number_text(value: float | None) -> str:
    """A setting as a person writes it: ``5``, ``3.99``; ``-`` when unknown."""
    return "-" if value is None else f"{value:g}"


def execution_settings(execution: dict[str, Any]) -> CommandSettings:
    """The execution's settings, from the first run with a readable command; the model falls back to the execution's own."""
    for run in execution.get("runs") or []:
        if run.get("command"):
            settings = command_settings(run["command"])
            if settings != CommandSettings():
                return settings if settings.model else CommandSettings(execution.get("model"), settings.refiner_model, settings.refiner_start, settings.cfg, settings.shift, settings.steps)
    return CommandSettings(model=execution.get("model") or None)


def refiner_text(settings: CommandSettings, width: int | None = None) -> str:
    """``name from 10%``, the name cut in the middle to fit ``width``; ``none`` without a refiner."""
    if settings.refiner_model is None:
        return "none"
    start = f" from {round(settings.refiner_start * 100)}%" if settings.refiner_start is not None else ""
    name = settings.refiner_model if width is None else cut_middle(settings.refiner_model, max(1, width - len(start)))
    return name + start


def succeeded_runs(execution: dict[str, Any]) -> list[dict[str, Any]]:
    """The runs the detail widget lists: those that finished successfully, in run order."""
    return [run for run in execution.get("runs") or [] if run.get("status") == "succeeded"]


def size_text(runs: list[dict[str, Any]]) -> str:
    """The measured output size of ``runs`` as ``832x448``; ``sizes vary`` when they differ, ``size -`` with none measured."""
    sizes = {(run["output_width"], run["output_height"]) for run in runs if run.get("output_width") and run.get("output_height")}
    if not sizes:
        return "size -"
    if len(sizes) > 1:
        return "sizes vary"
    width, height = sizes.pop()
    return f"{width}x{height}"


def run_steps(run: dict[str, Any]) -> int | None:
    return command_settings(run["command"]).steps if run.get("command") else None


def reveal_action(execution_id: int, run_number: int) -> str:
    """The click action that reveals a run's output; made of the two numbers only, never of stored text."""
    return f"reveal({int(execution_id)}, {int(run_number)})"


@dataclass(frozen=True)
class DetailRun:
    """A run the detail widget lists, as read once: ``exists`` is whether its output file was there."""

    number: int
    frames: int | None
    steps: int | None
    seconds: float | None
    output: str | None
    exists: bool


@dataclass(frozen=True)
class ExecutionDetail:
    """What the detail widget shows of an execution, read once, so a redraw neither reads the disk nor parses commands."""

    execution_id: int
    settings: CommandSettings
    size: str
    runs: tuple[DetailRun, ...]


def execution_detail(execution: dict[str, Any]) -> ExecutionDetail:
    """Read the execution for the detail widget; it looks for every output file, so it runs off the UI thread."""
    video = str(execution.get("mode") or "") in VIDEO_MODES
    runs = succeeded_runs(execution)
    listed = []
    for run in runs:
        path = run_file(execution, run.get("output"))
        listed.append(DetailRun(int(run["number"]), run.get("output_frames") if video else None, run_steps(run), run.get("seconds"), run.get("output") or None, path is not None and path.exists()))
    return ExecutionDetail(int(execution["id"]), execution_settings(execution), size_text(runs), tuple(listed))


def execution_detail_text(detail: ExecutionDetail, width: int, selected: int | None) -> tuple[Text, list[int]]:
    """The detail widget's content, and the run number each run's first line shows, in order.

    Every line is cut to ``width``; a file name is a link, through a click action built from numbers, never markup.
    """
    width = max(8, width)
    settings = detail.settings
    text = Text()
    for label, value in (("model", settings.model or "-"), ("refiner", None)):
        text.append(f"{label} ", style="bold")
        text.append(cut_middle(value, width - len(label) - 1) if value is not None else refiner_text(settings, width - len(label) - 1))
        text.append("\n")
    text.append(cut_middle(f"{detail.size}  CFG {number_text(settings.cfg)}  shift {number_text(settings.shift)}", width) + "\n")
    if not detail.runs:
        text.append("No run finished successfully", style="dim")
        return text, []
    text.append(cut_middle("#  Frames Steps Time", width), style="bold")
    shown: list[int] = []
    for run in detail.runs:
        seconds = whole_duration(run.seconds) if run.seconds is not None else "-"
        line = f"{run.number:<3}{str(run.frames or '-'):<7}{str(run.steps or '-'):<6}{seconds}"
        text.append("\n")
        text.append(cut_middle(line, width).ljust(width), style="reverse" if run.number == selected else "")
        text.append("\n   ")
        if not run.output:
            text.append("-", style="dim")
        elif not run.exists:
            text.append(cut_middle(f"{Path(run.output).name} (missing)", width - 3), style="dim")
        else:
            text.append(cut_middle(Path(run.output).name, width - 3), style=Style(underline=True, meta={"@click": reveal_action(detail.execution_id, run.number)}))
        shown.append(run.number)
    return text, shown


def output_measure_text(run: dict[str, Any]) -> str:
    """A run's measured output: ``832x448, 81 frames``; ``not measured`` when it was not."""
    if not (run.get("output_width") and run.get("output_height")):
        return "not measured"
    frames = f", {run['output_frames']} frames" if run.get("output_frames") else ""
    return f"{run['output_width']}x{run['output_height']}{frames}"


# The prompt flags, which /get param leaves out; /get prompts shows them.
PROMPT_FLAGS = ("--prompt", "--negative-prompt", "--prompt-file", "--negative-prompt-file")
# The --config-json key each flag replaces when both are given (a flag wins, as draw-things-cli applies them).
FLAG_CONFIG_KEYS = {"--model": "model", "--steps": "steps", "--cfg": "guidanceScale", "--width": "width", "--height": "height", "--frames": "numFrames", "--strength": "strength", "--seed": "seed"}
# A job's configuration overrides, and the flag or --config-json key each becomes (JobService._plan_run and build_config_json).
OVERRIDE_ARGUMENTS = {
    "model": "--model",
    "steps": "--steps",
    "guidance_scale": "--cfg",
    "width": "--width",
    "height": "--height",
    "frame_count": "--frames",
    "strength": "--strength",
    "seed": "--seed",
    "refiner_model": "refinerModel",
    "refiner_start": "refinerStart",
    "shift": "shift",
}


def find_run(execution: dict[str, Any], run_number: int | None) -> dict[str, Any] | str:
    """The run numbered ``run_number``, or, without one, the first run with a saved command; else why there is none."""
    runs = execution.get("runs") or []
    if run_number is not None:
        match = next((run for run in runs if run["number"] == run_number), None)
        return match if match is not None else f"Execution {execution['id']} has no run {run_number}"
    first = next((run for run in runs if run.get("command")), None)
    return first if first is not None else f"Execution {execution['id']} has no run with a saved command"


def prompts_text(execution: dict[str, Any], which: str, run_number: int | None) -> tuple[Text, tuple[str, str] | None]:
    """``positive``, ``negative``, or both (``prompts``): each prompt pair once with the runs that used it, or one run's.

    Each label stands on its own line with a blank line before it, and its prompt starts on the next line, so a prompt
    reads and selects as a block. Also returns what to copy to the clipboard and what to call it: the prompts alone for
    ``positive`` or ``negative``, the labelled prompts for ``prompts``; None when there is no prompt.
    """
    text = Text(f"Execution {execution['id']}: {execution['job_name']}", style="bold")
    if run_number is not None:
        run = find_run(execution, run_number)
        if isinstance(run, str):
            return Text(run, style="red"), None
        groups = [(f"Run {run['number']} (pair {run['pair']})", run)]
    else:
        # Each pair once, in the order it first ran, with every run that used it.
        pairs: dict[tuple[str, str, str | None], list[int]] = {}
        for run in execution.get("runs") or []:
            pairs.setdefault((run["pair"], run["positive"], run.get("negative")), []).append(int(run["number"]))
        if not pairs:
            text.append("\n  no runs", style="dim")
            return text, None
        groups = [(f"Pair {pair} (run{'s' if len(numbers) != 1 else ''} {', '.join(str(number) for number in numbers)})", {"positive": positive, "negative": negative}) for (pair, positive, negative), numbers in pairs.items()]
    copied: list[str] = []
    for heading, run in groups:
        text.append(f"\n{heading}\n", style="bold")
        for label, style in (("positive", "green"), ("negative", "red")):
            if which not in (label, "prompts"):
                continue
            value = run.get(label)
            prompt = value.strip() if value else ""
            text.append(f"\n{label}:", style=style)
            text.append("\n")
            text.append(f"{prompt or '(none)'}\n", style="" if prompt else "dim")
            if prompt:
                copied.append(f"{label}:\n{prompt}" if which == "prompts" else prompt)
    if not copied:
        return text, None
    return text, ("\n\n".join(copied), "prompts" if which == "prompts" else f"{which} prompt{'s' if len(copied) > 1 else ''}")


def parameters_text(execution: dict[str, Any], run_number: int | None) -> Text:
    """A run's draw-things-cli arguments without its prompts, as two tables (the flags, then --config-json), marking the
    job's overrides and every value a flag replaced."""
    run = find_run(execution, run_number)
    if isinstance(run, str):
        return Text(run, style="red")
    command = run.get("command") or []
    if not command:
        return Text(f"Run {run['number']} of execution {execution['id']} has no saved command", style="red")
    overrides = {OVERRIDE_ARGUMENTS[key] for key in ((execution.get("settings") or {}).get("config_override") or {}) if key in OVERRIDE_ARGUMENTS}
    config = config_json(command)
    flags = [(flag, value) for flag, value in command_arguments(command) if flag not in PROMPT_FLAGS and flag != "--config-json"]
    given = dict(flags)
    flag_rows = []
    for flag, value in flags:
        notes = ["job override"] if flag in overrides else []
        key = FLAG_CONFIG_KEYS.get(flag)
        if key in config and value is not None and not _same_value(value, config[key]):
            notes.append(f"replaces --config-json {_config_value(config[key])}")
        flag_rows.append((flag, "yes" if value is None else value, "; ".join(notes)))
    config_rows = []
    replaced_by = {key: flag for flag, key in FLAG_CONFIG_KEYS.items() if flag in given}
    for key, value in config.items():
        notes = ["job override"] if key in overrides else []
        flag = replaced_by.get(key)
        if flag is not None and given[flag] is not None and not _same_value(given[flag], value):
            notes.append(f"replaced by {flag} {given[flag]}")
        config_rows.append((key, _config_value(value), "; ".join(notes)))
    total = len(execution.get("runs") or [])
    text = Text(f"Execution {execution['id']}: {execution['job_name']}, run {run['number']} of {total}: draw-things-cli arguments", style="bold")
    if execution.get("config_file"):
        text.append(f" (configuration {execution['config_file']})")
    text.append("\n")
    text.append_text(_table(("Argument", "Value", "Note"), flag_rows))
    if config_rows:
        text.append("\n--config-json\n", style="bold")
        text.append_text(_table(("Key", "Value", "Note"), config_rows))
    text.rstrip()
    return text


def _same_value(flag_value: str, config_value: object) -> bool:
    """Whether a flag's text and a --config-json value are the same setting: numbers by value (``5.0`` is ``5``), the rest as text."""
    flag_number, config_number = setting_number(flag_value), setting_number(config_value)
    if flag_number is not None and config_number is not None:
        return flag_number == config_number
    return flag_value == _config_value(config_value)


def _config_value(value: object) -> str:
    """A --config-json value as the command line writes the same setting: text as it is, anything else as JSON."""
    if isinstance(value, str):
        return value if value else '""'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _table(header: tuple[str, str, str], rows: list[tuple[str, str, str]]) -> Text:
    """Rows in aligned columns under a header and a rule, as plain text; the last column is not padded."""
    widths = [max(len(row[index]) for row in (header, *rows)) for index in range(2)]
    text = Text()
    text.append(f"  {header[0].ljust(widths[0])}  {header[1].ljust(widths[1])}  {header[2]}".rstrip() + "\n", style="bold")
    text.append(f"  {'-' * widths[0]}  {'-' * widths[1]}  {'-' * len(header[2])}\n", style="dim")
    for name, value, note in rows:
        # No trailing spaces when a row has no note.
        text.append(f"  {name.ljust(widths[0])}  {value.ljust(widths[1])}  " if note else f"  {name.ljust(widths[0])}  {value}".rstrip())
        text.append(note, style="yellow")
        text.append("\n")
    return text
