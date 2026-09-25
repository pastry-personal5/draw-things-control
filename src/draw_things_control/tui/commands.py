"""The command line's `/` commands: parsing, usage, help text, and completion."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual.suggester import Suggester

from draw_things_control.tui.history import STATUSES

PREFIX = "/"
# Name, arguments, and what it does, in the order help lists them.
COMMANDS = (
    ("help", "", "List the commands and keys"),
    ("jobs", "", "List the job files and whether each is valid"),
    ("job", "JOB", "The summary, prompt pairs, and dry-run plan of a job"),
    ("run", "JOB", "Read the job again, confirm, and run it"),
    ("stop", "", "Stop the running job, after confirmation"),
    ("history", "", "Read the execution history again"),
    ("execution", "ID", "The detail of one execution"),
    ("filter", "status STATUS", f"Show only {', '.join(STATUSES)} executions"),
    ("filter", "name TEXT", "Show only executions whose job name or file name contains TEXT"),
    ("filter", "off", "Remove the history filters"),
    ("reveal", "ID [RUN]", "Reveal a run's output in Finder (default: the last output)"),
    ("clear", "", "Clear the messages"),
    ("quit", "", "Quit; while a job runs, asks to stop it first"),
)
COMMAND_NAMES = tuple(dict.fromkeys(name for name, _, _ in COMMANDS))
FILTER_WORDS = ("status", "name", "off")
# Characters a shell would split or interpret, escaped with a backslash in a completed job file name.
SHELL_SPECIAL = re.compile(r"([^\w@%+=:,./-])")
KEYS = (
    ("Enter", "Run the command, or show the selected execution (history)"),
    ("Tab", "Complete the command line, or move to the history"),
    ("Up/Down", "Recall this session's commands (command line), move (history)"),
    ("Escape", "Clear the command line, or go back to it (history)"),
    ("Ctrl-C", "Clear the command line, or press twice to quit"),
)


class CommandError(ValueError):
    """A command line that cannot be run, and why."""


@dataclass(frozen=True)
class Command:
    """A parsed command line: the command's name, without the slash, and its arguments."""

    name: str
    arguments: tuple[str, ...]


def parse(line: str) -> Command | None:
    """Split the line as a shell would; None for an empty line. Raises CommandError for a line without the slash, an unknown command, or bad quoting."""
    if not line.strip():
        return None
    if not line.lstrip().startswith(PREFIX):
        raise CommandError(f"Commands begin with {PREFIX}; type {PREFIX}help")
    try:
        words = shlex.split(line)
    except ValueError as error:
        raise CommandError(f"Cannot read the command: {error}") from error
    name = words[0][len(PREFIX) :].lower()
    if name not in COMMAND_NAMES:
        raise CommandError(f"Unknown command '{words[0]}'; type {PREFIX}help")
    return Command(name, tuple(words[1:]))


def usage(name: str) -> str:
    """Every form of a command, as help lists them."""
    return " | ".join(f"{PREFIX}{name} {arguments}".strip() for command, arguments, _ in COMMANDS if command == name)


def help_text() -> Text:
    """The commands and keys, for the messages."""
    text = Text("Commands\n", style="bold")
    forms = [(f"{PREFIX}{name} {arguments}".strip(), action) for name, arguments, action in COMMANDS]
    width = max(len(form) for form, _ in forms)
    for form, action in forms:
        text.append(f"  {form.ljust(width)}  ", style="bold")
        text.append(f"{action}\n")
    text.append("JOB is a job file name in the data directory, or its name without the suffix. Quote a name with spaces.\n", style="dim")
    text.append("Keys\n", style="bold")
    width = max(len(key) for key, _ in KEYS)
    for key, action in KEYS:
        text.append(f"  {key.ljust(width)}  ", style="bold")
        text.append(f"{action}\n")
    text.rstrip()
    return text


def completions(line: str, job_names: list[str]) -> list[str]:
    """Whole command lines that ``line`` could be completed to, in order; each one begins with ``line``, as Input needs."""
    command, space, rest = line.partition(" ")
    if space and command.lower() in (f"{PREFIX}run", f"{PREFIX}job"):
        return [f"{command} {name}" for name in job_completions(rest, job_names)]
    head, _, last = line.rpartition(" ")
    words = [word.lower() for word in head.split()]
    if not words:
        candidates = [f"{PREFIX}{name}" for name in COMMAND_NAMES] if not head else []
    elif words == [f"{PREFIX}filter"]:
        candidates = list(FILTER_WORDS)
    elif words == [f"{PREFIX}filter", "status"]:
        candidates = list(STATUSES)
    else:
        candidates = []
    prefix = f"{head} " if head or line.startswith(" ") else ""
    return [f"{prefix}{candidate}" for candidate in candidates if candidate.startswith(last) and candidate != last]


def job_completions(typed: str, job_names: list[str]) -> list[str]:
    """Each job file name, written so it parses as one argument, that begins with ``typed``.

    Inside a quote the user opened, the name is closed with the same quote; otherwise its special characters are escaped
    with backslashes, so ``my`` completes to ``my\\ job.yaml`` and the suggestion still begins with what was typed.
    """
    quote = typed[:1] if typed[:1] in ("'", '"') else ""
    written = [f"{quote}{name}{quote}" if quote else SHELL_SPECIAL.sub(r"\\\1", name) for name in job_names if not quote or quote not in name]
    return [name for name in written if name.startswith(typed) and name != typed]


class CommandSuggester(Suggester):
    """Suggests the rest of a command name, a job file name, or a filter word; the job names are read on each call."""

    def __init__(self, job_names: Callable[[], list[str]]) -> None:
        # No cache: the job names change when the data directory is read again.
        super().__init__(use_cache=False, case_sensitive=True)
        self.job_names = job_names

    async def get_suggestion(self, value: str) -> str | None:
        found = completions(value, self.job_names())
        return found[0] if found else None
