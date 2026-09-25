"""Widgets and the text they show; job text comes from jobs.job_report, so it matches the CLI."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED_NOTE, RANDOM_SEED_TEXT, ignored_config_lines, job_summary, pair_runs

KEYS = (
    ("Up/Down, j/k", "Move in the list"),
    ("Enter", "Open the detail view"),
    ("Escape", "Back from the detail view"),
    ("r", "Refresh the list"),
    ("?", "Help"),
    ("q, Ctrl-C", "Quit"),
)


class JobTable(DataTable[str]):
    """The job list, with vi-style keys beside the arrows."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


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
