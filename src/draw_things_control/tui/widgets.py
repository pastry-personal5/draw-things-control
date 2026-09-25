"""Widgets and the text they show; job text comes from jobs.job_report, so it matches the CLI."""

from __future__ import annotations

import math

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED_NOTE, RANDOM_SEED_TEXT, cooldown_details, duration_text, ignored_config_lines, job_summary, pair_runs, seconds_text
from draw_things_control.tui.live_run import LiveRun, RunState

KEYS = (
    ("Up/Down, j/k", "Move in the list"),
    ("Enter", "Open the detail view"),
    ("x", "Run the job, after confirmation (list, detail)"),
    ("l", "Open the live view of the running or last job (list)"),
    ("s", "Stop the job, after confirmation (live view)"),
    ("Escape", "Back to the list (a running job continues)"),
    ("r", "Refresh the list"),
    ("?", "Help"),
    ("q, Ctrl-C", "Quit; while a job runs, asks to stop it first"),
)
PHASE_TEXT = {"starting": "Starting...", "running": "running", "cooling_down": "cooling down", "stopping": "Stopping...", "finished": "finished", "not_started": "did not start"}
STATUS_STYLE = {"running": "bold cyan", "succeeded": "green", "failed": "red", "timed_out": "red", "interrupted": "yellow", "pending": "dim"}


class JobTable(DataTable[str]):
    """The job list, with vi-style keys beside the arrows."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


class RunTable(DataTable[Text]):
    """The live view's runs: number, pair, status, and seconds."""

    def show(self, runs: list[RunState]) -> None:
        """Show ``runs``, changing cells in place, so the table keeps its scroll position."""
        if not self.columns:
            self.add_columns("Run", "Pair", "Status", "Seconds")
        for run in runs[self.row_count :]:
            self.add_row(*run_cells(run), key=str(run.number))
        for run in runs:
            for column, cell in zip(self.columns, run_cells(run), strict=True):
                if self.get_cell(str(run.number), column) != cell:
                    self.update_cell(str(run.number), column, cell, update_width=True)


def run_cells(run: RunState) -> tuple[Text, ...]:
    seconds = seconds_text(run.seconds) if run.seconds is not None else "-"
    return (Text(str(run.number)), Text(run.pair), Text(run.status, style=STATUS_STYLE.get(run.status, "")), Text(seconds))


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
    """The dry-run plan, the reason it cannot be made, or a note that it is being made."""
    text = Text("Dry-run plan\n\n", style="bold")
    if error is not None:
        text.append(error, style="red")
        return text
    if lines is None:
        text.append("Computing the plan...", style="dim")
        return text
    if job.configured_seed()[0] is None:
        text.append(f"{PLACEHOLDER_SEED_NOTE}\n\n", style="yellow")
    for line in lines:
        text.append(f"{line}\n", style="dim" if line.startswith("#") else "")
    return text


def keys_text() -> Text:
    """The help screen's key table."""
    text = Text("Keys\n\n", style="bold")
    width = max(len(key) for key, _ in KEYS)
    for key, action in KEYS:
        text.append(f"  {key.ljust(width)}  ", style="bold")
        text.append(f"{action}\n")
    text.append("\nPress Escape or ? to close.", style="dim")
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
    text.append("\ny or Enter: run    n or Escape: cancel", style="dim")
    return text


def question_text(question: str, confirm: str, cancel: str) -> Text:
    """A yes-or-no question with its keys."""
    text = Text(f"{question}\n\n", style="bold")
    text.append(f"y or Enter: {confirm}    n or Escape: {cancel}", style="dim")
    return text


def live_header_text(live: LiveRun) -> Text:
    """The job's name, mode, seed, and phase."""
    text = Text(live.job_name, style="bold")
    if live.started is not None:
        text.append(f"  {live.started.mode}  seed {live.started.seed} ({live.started.seed_source})")
    text.append("  ")
    text.append(PHASE_TEXT[live.phase], style="bold yellow" if live.phase in ("starting", "stopping") else "bold")
    return text


def progress_text(live: LiveRun) -> str | None:
    """Step progress as ``3/8, 37%``, or None before the child reports any."""
    parts = []
    if live.progress is not None:
        parts.append(f"{live.progress[0]}/{live.progress[1]}")
    if live.percent is not None:
        parts.append(f"{live.percent}%")
    return ", ".join(parts) or None


def active_run_text(live: LiveRun) -> Text:
    """The running run's elapsed time, output, progress, and redacted command; empty when none runs."""
    if live.active_run is None or live.run_started_at is None:
        return Text()
    elapsed = max(0.0, live.now() - live.run_started_at)
    text = Text(f"Run {live.active_run}/{len(live.runs)}", style="bold")
    text.append(f"  {duration_text(int(elapsed))} elapsed\n")
    text.append("  output: ", style="bold")
    text.append(f"{live.active_output}\n")
    progress = progress_text(live)
    if progress is not None:
        text.append("  progress: ", style="bold")
        text.append(f"{progress}\n")
    text.append("  command: ", style="bold")
    text.append(" ".join(live.command), style="dim")
    return text


def cooldown_text(live: LiveRun) -> Text:
    """Seconds left in the cooldown and the local time it ends; empty when not cooling down."""
    if live.cooldown is None or live.cooldown_ends_at is None:
        return Text()
    left = max(0, math.ceil(live.cooldown_ends_at - live.now()))
    text = Text("Cooldown", style="bold")
    text.append(f" before run {live.cooldown.after_run + 1}: {duration_text(left)} left, until {live.cooldown.until}")
    return text


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
    text.append("\n")
    if live.error is not None:
        text.append(f"{live.error}\n", style="red")
    if live.started is not None and live.started.manifest is not None:
        text.append("  manifest: ", style="bold")
        text.append(f"{live.started.manifest}\n")
    if live.started is not None and live.started.log is not None:
        text.append("  log: ", style="bold")
        text.append(f"{live.started.log}\n")
    return text
