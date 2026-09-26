"""The text the TUI shows; job text comes from jobs.job_report, so it matches the CLI. Plain functions, no widgets."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text

from draw_things_control.core.global_config import parse_cooldown
from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_events import CooldownEnded, CooldownStarted, JobEvent, JobStarted, RunFinished, RunStarted
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED_NOTE, RANDOM_SEED_TEXT, auto_wait_text, cooldown_details, duration_text, ignored_config_lines, job_summary, pair_runs, policy_text, seconds_text
from draw_things_control.tui.history import is_imported, run_file
from draw_things_control.tui.live_run import LiveRun

PHASE_TEXT = {"starting": "starting", "running": "running", "cooling_down": "cooling down", "stopping": "stopping", "finished": "finished", "not_started": "did not start"}
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
    for label, value in (
        ("status", f"{execution['status']}, exit code {execution['exit_code'] if execution['exit_code'] is not None else '-'}{signal}"),
        ("job file", execution["job_file"]),
        ("mode", execution["mode"]),
        ("model", execution.get("model") or "-"),
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
