"""An execution as text: its detail in Messages, and the Execution widget."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.style import Style
from rich.text import Text

from draw_things_control.core.arguments import CommandSettings, command_settings
from draw_things_control.core.cooldown import parse_cooldown
from draw_things_control.jobs.text import MEDIA_CHECK_LABELS, media_check_result, policy_text, seconds_text
from draw_things_control.state.executions import ExecutionRow, MediaCheckRow, RunRow
from draw_things_control.state.ids import execution_id_text
from draw_things_control.tui.text.arguments import PreviousRun, argument_rows, arguments_text, execution_notes
from draw_things_control.tui.text.common import STATUS_STYLE, VIDEO_MODES
from draw_things_control.tui.text.prompts import prompt_block
from draw_things_control.tui.text.status import whole_duration


def file_text(execution: ExecutionRow, name: str | None) -> str:
    """A run's file as a full path, marked when it no longer exists."""
    path = execution.run_file(name)
    if path is None:
        return name or "-"
    return str(path) if path.exists() else f"{path} (missing)"


def stored_cooldown_text(execution: ExecutionRow) -> str:
    """An execution's cooldown and source, from the resolved mapping when it was recorded, else the old seconds, which mean manual."""
    source = f"({execution.cooldown_source or '-'})"
    mapping = execution.settings.cooldown
    if isinstance(mapping, dict):
        try:
            return f"{policy_text(parse_cooldown(mapping, 'cooldown'))} {source}"
        except ValueError:
            pass
    seconds = execution.cooldown_seconds
    return f"{seconds_text(seconds)} {source}" if seconds is not None else "-"


def _fields(text: Text, rows: tuple[tuple[str, str | None], ...]) -> None:
    """Append each ``  label: value`` line; a row whose value is None is left out."""
    for label, value in rows:
        if value is not None:
            text.append(f"  {label}: ", style="bold")
            text.append(f"{value}\n")


def _execution_fields(execution: ExecutionRow) -> tuple[tuple[str, str | None], ...]:
    signal = f", stopped by {execution.signal}" if execution.signal else ""
    settings = execution_settings(execution)
    return (
        ("status", f"{execution.status}, exit code {execution.exit_code if execution.exit_code is not None else '-'}{signal}"),
        ("job file", execution.job_file),
        ("mode", execution.mode),
        ("model", execution.model or "-"),
        ("refiner", refiner_text(settings)),
        ("CFG", number_text(settings.cfg)),
        ("shift", number_text(settings.shift)),
        ("seed", f"{execution.seed} ({execution.seed_source or '-'})" if execution.seed is not None else "-"),
        ("cooldown", stored_cooldown_text(execution)),
        ("resumes", execution_id_text(execution.resumes) if execution.resumes is not None else None),
        ("first run", str(execution.first_run) if execution.first_run > 1 else None),
        ("started", execution.started_at),
        ("finished", execution.finished_at or "-"),
        ("manifest", execution.manifest_path or "-"),
        ("log", execution.log_path or "-"),
    )


def _run_heading(text: Text, run: RunRow) -> None:
    text.append(f"\nRun {run.number} ", style="bold")
    text.append(run.status, style=STATUS_STYLE.get(run.status, ""))
    seconds = f", {seconds_text(run.seconds)}" if run.seconds is not None else ""
    text.append(f" (pair {run.pair}{seconds}, exit code {run.exit_code if run.exit_code is not None else '-'})\n")
    text.append("  steps: ", style="bold")
    text.append(f"{run_steps(run) or '-'}, output {output_measure_text(run)}\n")


def _run_text(text: Text, execution: ExecutionRow, run: RunRow, notes: dict[str, str], previous: PreviousRun | None) -> PreviousRun | None:
    """One run's lines; returns the run whose arguments the next run's are compared with."""
    _run_heading(text, run)
    prompt_block(text, "positive", "green", run.positive)
    prompt_block(text, "negative", "red", run.negative)
    text.append("\n")
    _fields(text, (("input", run.input or "-"), ("output", file_text(execution, run.output)), ("last frame", file_text(execution, run.last_frame) if run.last_frame else None)))
    if run.cooldown_after_seconds is not None:
        _fields(text, (("cooldown after", seconds_text(run.cooldown_after_seconds)),))
    _check_lines(text, run.checks)
    if not run.command:
        return previous
    # The arguments as a table, never the command line with its bare --config-json.
    text.append("\n")
    arguments = argument_rows(run.command, notes)
    text.append_text(arguments_text(arguments, previous))
    return (int(run.number), arguments)


def _check_lines(text: Text, checks: Sequence[MediaCheckRow]) -> None:
    for check in checks:
        text.append(f"  {MEDIA_CHECK_LABELS.get(check.stage, check.stage).lower()}: ", style="bold")
        text.append(media_check_result(check.file, check.summary, check.verdict, check.notes) + "\n", style="yellow" if check.verdict == "warning" else "")


def _unstarted_checks_text(text: Text, checks: Sequence[MediaCheckRow]) -> None:
    """Checks made before a run that never started (a stop during the input checks), under the run they were for."""
    for number in sorted({check.run for check in checks}):
        text.append(f"\nBefore run {number} ", style="bold")
        text.append("(never started)\n", style="yellow")
        _check_lines(text, [check for check in checks if check.run == number])


def execution_text(execution: ExecutionRow) -> Text:
    """One execution as it ran, from the stored row, and each of its runs; never the current job file."""
    text = Text(f"Execution {execution.label}: {execution.job_name}", style="bold")
    if execution.imported:
        text.append("  imported", style="yellow")
    text.append("\n")
    _fields(text, _execution_fields(execution))
    if execution.recovered_at:
        text.append(f"  closed as interrupted at {execution.recovered_at}, after the process that ran it ended\n", style="yellow")
    notes = execution_notes(execution)
    # Each run's arguments after the first with a command: only what changed since the run before.
    previous: PreviousRun | None = None
    for run in execution.runs:
        previous = _run_text(text, execution, run, notes, previous)
    _unstarted_checks_text(text, execution.checks)
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


def execution_settings(execution: ExecutionRow) -> CommandSettings:
    """The execution's settings, from the first run with a readable command; the model falls back to the execution's own."""
    for run in execution.runs:
        if run.command:
            settings = command_settings(run.command)
            if settings != CommandSettings():
                return settings if settings.model else CommandSettings(execution.model, settings.refiner_model, settings.refiner_start, settings.cfg, settings.shift, settings.steps)
    return CommandSettings(model=execution.model or None)


def refiner_text(settings: CommandSettings, width: int | None = None) -> str:
    """``name from 10%``, the name cut in the middle to fit ``width``; ``none`` without a refiner."""
    if settings.refiner_model is None:
        return "none"
    start = f" from {round(settings.refiner_start * 100)}%" if settings.refiner_start is not None else ""
    name = settings.refiner_model if width is None else cut_middle(settings.refiner_model, max(1, width - len(start)))
    return name + start


def succeeded_runs(execution: ExecutionRow) -> tuple[RunRow, ...]:
    """The runs the detail widget lists: those that finished successfully, in run order."""
    return execution.succeeded_runs


def size_text(runs: Sequence[RunRow]) -> str:
    """The measured output size of ``runs`` as ``832x448``; ``sizes vary`` when they differ, ``size -`` with none measured."""
    sizes = {(run.output_width, run.output_height) for run in runs if run.output_width and run.output_height}
    if not sizes:
        return "size -"
    if len(sizes) > 1:
        return "sizes vary"
    width, height = sizes.pop()
    return f"{width}x{height}"


def run_steps(run: RunRow) -> int | None:
    return command_settings(run.command).steps if run.command else None


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
    # The execution it resumes (E0012), when it is a resume; None otherwise.
    resumes: str | None = None


def execution_detail(execution: ExecutionRow) -> ExecutionDetail:
    """Read the execution for the detail widget; it looks for every output file, so it runs off the UI thread."""
    video = str(execution.mode or "") in VIDEO_MODES
    runs = succeeded_runs(execution)
    listed = []
    for run in runs:
        path = execution.run_file(run.output)
        listed.append(DetailRun(int(run.number), run.output_frames if video else None, run_steps(run), run.seconds, run.output or None, path is not None and path.exists()))
    resumes = execution_id_text(execution.resumes) if execution.resumes is not None else None
    return ExecutionDetail(int(execution.id), execution_settings(execution), size_text(runs), tuple(listed), resumes)


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
    if detail.resumes is not None:
        text.append(cut_middle(f"resumes {detail.resumes}", width) + "\n", style="dim")
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


def output_measure_text(run: RunRow) -> str:
    """A run's measured output: ``832x448, 81 frames``; ``not measured`` when it was not."""
    if not (run.output_width and run.output_height):
        return "not measured"
    frames = f", {run.output_frames} frames" if run.output_frames else ""
    return f"{run.output_width}x{run.output_height}{frames}"
