"""An execution's draw-things-cli arguments as tables, as /get param shows them."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from rich.text import Text

from draw_things_control.core.arguments import FLAG_CONFIG_KEYS, OVERRIDE_ARGUMENTS, command_arguments, config_json
from draw_things_control.core.numbers import setting_number
from draw_things_control.jobs.events import JobEvent, RunStarted
from draw_things_control.state.executions import ExecutionRow
from draw_things_control.tui.live_run import LiveRun
from draw_things_control.tui.text.prompts import find_run

# The prompt flags, which /get param leaves out; /get prompts shows them.
PROMPT_FLAGS = ("--prompt", "--negative-prompt", "--prompt-file", "--negative-prompt-file")


def parameters_text(execution: ExecutionRow, run_number: int | None) -> Text:
    """A run's draw-things-cli arguments without its prompts, as two tables (the flags, then --config-json), marking the
    job's overrides and every value a flag replaced."""
    run = find_run(execution, run_number)
    if isinstance(run, str):
        return Text(run, style="red")
    command = run.command or []
    if not command:
        return Text(f"Run {run.number} of execution {execution.label} has no saved command", style="red")
    total = len(execution.runs)
    text = Text(f"Execution {execution.label}: {execution.job_name}, run {run.number} of {total}: draw-things-cli arguments", style="bold")
    if execution.config_file:
        text.append(f" (configuration {execution.config_file})")
    text.append("\n")
    text.append_text(arguments_table(command, execution_notes(execution)))
    text.rstrip()
    return text


# A row of an argument table: its name, its value, and its note.
Row = tuple[str, str, str]


# A command's argument rows: the flags, then the --config-json keys.
Arguments = tuple[list[Row], list[Row]]


# An earlier run's number and argument rows, which a later run's arguments are compared with.
PreviousRun = tuple[int, Arguments]


def override_notes(config_override: dict[str, Any] | None, sized: bool) -> dict[str, str]:
    """The note for each flag or --config-json key the job set: ``job override`` for ``config_override``'s keys, and
    ``desired input size`` for the width and height when desired_input_width or desired_input_height set them (``sized``),
    which then replace any override of the size."""
    notes = {OVERRIDE_ARGUMENTS[key]: "job override" for key in (config_override or {}) if key in OVERRIDE_ARGUMENTS}
    if sized:
        notes.update(dict.fromkeys(("--width", "--height", "width", "height"), "desired input size"))
    return notes


def execution_notes(execution: ExecutionRow) -> dict[str, str]:
    """``override_notes`` for a stored execution, from the job settings it ran with; a resize plan means a desired size."""
    settings = execution.settings
    return override_notes(settings.config_override, settings.input_resize is not None)


def argument_rows(command: Sequence[str], notes: dict[str, str]) -> tuple[list[Row], list[Row]]:
    """A command's draw-things-cli arguments without its prompts, as the flag rows and the --config-json rows, with
    ``notes`` (``override_notes``) and every value a flag replaced marked."""
    config = config_json(command)
    flags = [(flag, value) for flag, value in command_arguments(command) if flag not in PROMPT_FLAGS and flag != "--config-json"]
    given = dict(flags)
    flag_rows = []
    for flag, value in flags:
        marks = [notes[flag]] if flag in notes else []
        key = FLAG_CONFIG_KEYS.get(flag)
        if key in config and value is not None and not _same_value(value, config[key]):
            marks.append(f"replaces --config-json {_config_value(config[key])}")
        flag_rows.append((flag, "yes" if value is None else value, "; ".join(marks)))
    config_rows = []
    replaced_by = {key: flag for flag, key in FLAG_CONFIG_KEYS.items() if flag in given}
    for key, value in config.items():
        marks = [notes[key]] if key in notes else []
        flag = replaced_by.get(key)
        flag_value = given[flag] if flag is not None else None
        if flag is not None and flag_value is not None and not _same_value(flag_value, value):
            marks.append(f"replaced by {flag} {flag_value}")
        config_rows.append((key, _config_value(value), "; ".join(marks)))
    return flag_rows, config_rows


def arguments_table(command: Sequence[str], notes: dict[str, str]) -> Text:
    """A command's arguments as two tables, the flags and then each --config-json key; never JSON as it is."""
    return _tables(*argument_rows(command, notes))


def arguments_text(arguments: Arguments, previous: PreviousRun | None) -> Text:
    """A run's argument rows (``argument_rows``) as tables: in full, or, after an earlier run (``previous``), only the rows
    that changed since it, and ``(not given)`` for a row it had and this run does not. Values are written one to one
    (``_config_value``), so comparing the rows compares the values."""
    if previous is None:
        return _tables(*arguments)
    number, before = previous
    changed = [_changed_rows(old, new) for old, new in zip(before, arguments, strict=True)]
    if not any(changed):
        return Text(f"Arguments as run {number}\n", style="dim")
    text = Text(f"Arguments as run {number}, except:\n", style="dim")
    text.append_text(_tables(*changed))
    return text


def _changed_rows(before: list[Row], after: list[Row]) -> list[Row]:
    """The rows of ``after`` that differ from ``before``, then each of ``before``'s rows ``after`` lacks, as not given. A
    flag given more than once (``--image``) is matched by its place among its own repeats."""

    def keyed(rows: list[Row]) -> dict[tuple[str, int], Row]:
        seen: dict[str, int] = {}
        result = {}
        for row in rows:
            seen[row[0]] = seen.get(row[0], 0) + 1
            result[(row[0], seen[row[0]])] = row
        return result

    old, new = keyed(before), keyed(after)
    return [row for key, row in new.items() if old.get(key) != row] + [(row[0], "(not given)", "") for key, row in old.items() if key not in new]


def _tables(flag_rows: list[Row], config_rows: list[Row]) -> Text:
    text = _table(("Argument", "Value", "Note"), flag_rows) if flag_rows else Text()
    if config_rows:
        text.append("--config-json\n" if not flag_rows else "\n--config-json\n", style="bold")
        text.append_text(_table(("Key", "Value", "Note"), config_rows))
    return text


def _same_value(flag_value: str, config_value: object) -> bool:
    """Whether a flag's text and a --config-json value are the same setting: numbers by value (``5.0`` is ``5``), text as
    it is, anything else as the table writes it."""
    flag_number, config_number = setting_number(flag_value), setting_number(config_value)
    if flag_number is not None and config_number is not None:
        return flag_number == config_number
    return flag_value == (config_value if isinstance(config_value, str) else _config_value(config_value))


def _config_value(value: object) -> str:
    """A --config-json value in words, never JSON, and one to one, so two values that differ never read the same: text
    always in double quotes (a quote or backslash inside escaped), a number as the command line writes it (``5.0`` as
    ``5``, the same setting), ``true`` or ``false``, ``(none)`` for null, a list in brackets with its items separated by
    commas, and a mapping in braces as ``key=value`` pairs, a key quoted unless it is a plain name."""
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "(none)"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dict):
        return "{" + " ".join(f"{key if PLAIN_KEY.fullmatch(str(key)) else _quoted(str(key))}={_config_value(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_config_value(item) for item in value) + "]"
    return str(value)


# A mapping key written without quotes.
PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


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


def run_arguments(live: LiveRun | None, event: JobEvent) -> tuple[Arguments | None, PreviousRun | None]:
    """For a run with a command: its argument rows, and the last such run of this job to compare them with. The last
    run is kept on the job's LiveRun, which each job starts afresh; a run without a command is never compared with."""
    if not isinstance(event, RunStarted) or not event.command:
        return None, None
    started = live.started if live is not None else None
    notes = override_notes(started.config_override, started.input_resize is not None) if started is not None else {}
    arguments = argument_rows(event.command, notes)
    if live is None:
        return arguments, None
    previous, live.previous_arguments = live.previous_arguments, (event.number, arguments)
    return arguments, previous
