"""The job files as text: the list, the Job Definition rows, a job's summary and plan, and the confirmation of a run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.text import Text

from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.text import PLACEHOLDER_SEED_NOTE, RANDOM_SEED_TEXT, cooldown_details, ignored_config_lines, job_summary, pair_runs
from draw_things_control.services.job_catalog import JobRow
from draw_things_control.services.job_details import JobDetails
from draw_things_control.tui.text.arguments import PreviousRun, argument_rows, arguments_text, override_notes
from draw_things_control.tui.text.prompts import prompt_block


def jobs_text(rows: list[JobRow], running: str | None, message: str | None) -> Text:
    """The job files: job ID, file name, job name, mode, runs, and status (valid, running, or the first error)."""
    if message is not None:
        return Text(message, style="yellow")
    text = Text("Jobs\n", style="bold")
    width = max(len(row.path.name) for row in rows)
    id_width = max((len(row.job_id) for row in rows if row.job_id is not None), default=0)
    for row in rows:
        if id_width:
            text.append(f"  {(row.job_id or '-').ljust(id_width)}", style="bold")
        text.append(f"  {row.path.name.ljust(width)}  ", style="bold")
        job = row.job
        if job is None:
            text.append(f"invalid: {row.error}\n", style="red")
            continue
        text.append(f"{job.name}  {job.mode}  {job.run_count} run{'s' if job.run_count != 1 else ''}  ")
        running_here = row.path.name == running
        text.append("running\n" if running_here else "valid\n", style="bold cyan" if running_here else "green")
    text.rstrip()
    return text


def job_display_names(paths: list[Path]) -> dict[Path, str]:
    """Each job file's name in the Job Definition widget: without its extension, unless another file has the same stem
    (``walk.yaml``, ``walk.yml``), when both keep it so they stay apart."""
    stems: dict[str, int] = {}
    for path in paths:
        stems[path.stem] = stems.get(path.stem, 0) + 1
    return {path: path.stem if stems[path.stem] == 1 else path.name for path in paths}


def changed_text(changed: float | None, now: datetime | None = None) -> str:
    """A file's modification time: ``09-26 14:05`` this year, or the date with its year (``2025-09-26``) before it."""
    if changed is None:
        return "-"
    when = datetime.fromtimestamp(changed).astimezone()
    return when.strftime("%m-%d %H:%M") if when.year == (now or datetime.now().astimezone()).year else when.strftime("%Y-%m-%d")


def job_definition_cells(row: JobRow, name: str) -> tuple[Text, ...]:
    """The Job Definition widget's row: ID, name, changed time, mode, and runs; an invalid file in dim red."""
    style = "dim red" if row.job is None else ""
    mode = str(row.job.mode) if row.job is not None else "invalid"
    runs = str(row.job.run_count) if row.job is not None else "invalid"
    return tuple(Text(cell, style=style) for cell in (row.job_id or "-", name, changed_text(row.changed), mode, runs))


def sort_job_rows(rows: list[JobRow], key: str, descending: bool) -> list[JobRow]:
    """The rows sorted by ``key``, ties by job ID (a row without one last); invalid files after valid ones by mode and runs."""
    names = job_display_names([row.path for row in rows])
    by_id = sorted(rows, key=lambda row: (row.number is None, row.number or 0))
    if key in ("mode", "runs"):
        valid = [row for row in by_id if row.job is not None]
        invalid = [row for row in by_id if row.job is None]
        value = (lambda row: str(row.job.mode)) if key == "mode" else (lambda row: row.job.run_count)
        return sorted(valid, key=value, reverse=descending) + invalid
    values = {
        "id": lambda row: (row.number is None, row.number or 0),
        "name": lambda row: names[row.path].lower(),
        "changed": lambda row: row.changed or 0.0,
    }
    return sorted(by_id, key=values.get(key, values["id"]), reverse=descending)


def summary_text(job: JobDefinition, job_id: str | None = None) -> Text:
    """validate-job's summary, with the job ID when it has one, a random seed described as the plan uses it, and any
    ignored configuration."""
    text = Text()
    if job_id is not None:
        text.append("Job ID: ", style="bold")
        text.append(f"{job_id}\n")
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
    """Each prompt pair and the runs that use it, its prompts laid out as /get prompts lays them out."""
    text = Text("Prompt pairs\n", style="bold")
    for pair, runs in pair_runs(job):
        used = ", ".join(str(number) for number in runs) if runs else "none"
        text.append(f"\n{pair.name}", style="bold")
        text.append(f"{' (default)' if pair.default else ''}: run{'s' if len(runs) != 1 else ''} {used}\n")
        prompt_block(text, "positive", "green", pair.positive)
        prompt_block(text, "negative", "red", pair.negative)
    return text


def plan_text(job: JobDefinition, details: JobDetails) -> Text:
    """The dry-run plan of ``job`` in words: its header, then each run's heading and its arguments as a table (after run
    1, only what changed since the run before), never a command line; or the reason it cannot be made. The prompts are
    in the prompt pairs above."""
    text = Text("Dry-run plan\n\n", style="bold")
    if details.plan_error is not None:
        text.append(details.plan_error, style="red")
        return text
    if job.configured_seed()[0] is None:
        text.append(f"{PLACEHOLDER_SEED_NOTE}\n\n", style="yellow")
    for line in details.plan or ():
        text.append(f"{line}\n", style="dim")
    notes = override_notes(job.config_override.as_dict(), job.size is not None)
    previous: PreviousRun | None = None
    for step in details.runs:
        if step.cooldown is not None:
            text.append(f"\n{step.cooldown}\n", style="dim")
        text.append(f"\n{step.heading}: {step.run.output}\n", style="bold")
        arguments = argument_rows(step.command, notes)
        text.append_text(arguments_text(arguments, previous))
        previous = (step.run.number, arguments)
    return text


def details_text(job: JobDefinition, details: JobDetails, job_id: str | None = None) -> Text:
    """What /describe job prints for a valid job (the caller shows an invalid one's error): the summary, the prompt
    pairs, and the dry-run plan."""
    text = summary_text(job, job_id)
    text.append("\n")
    text.append_text(pairs_text(job))
    text.append("\n")
    text.append_text(plan_text(job, details))
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
    # Not Enter: the Enter that submitted /apply must not also answer this.
    text.append("\ny: run    n or Escape: cancel", style="dim")
    return text


def question_text(question: str, confirm: str, cancel: str) -> Text:
    """A yes-or-no question with its keys."""
    text = Text(f"{question}\n\n", style="bold")
    text.append(f"y or Enter: {confirm}    n or Escape: {cancel}", style="dim")
    return text
