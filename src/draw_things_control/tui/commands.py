"""The command line's `/` commands: parsing, usage, help text, and completion."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.suggester import Suggester

from draw_things_control.services.history import STATUSES

PREFIX = "/"
# Name, arguments, and what it does, in the order help lists them. For /get and /describe, the first argument names
# what the command acts on.
COMMANDS = (
    ("help", "", "List the commands and keys"),
    ("get", "jobs", "List the job files and whether each is valid"),
    ("get", "queue", "List the queue's entries"),
    ("describe", "job <Job ID>", "The summary, prompt pairs, and dry-run plan of a job"),
    ("describe", "queue <Queue ID>", "One queue entry: its state, execution, current run, and whether it can be resumed"),
    ("sort", "jobs KEY [asc|desc]", "Sort the Job Definition widget by id, name, changed, mode, or runs"),
    ("apply", "[<Job ID>]", "Read the job again and submit it to the queue (the selected job, if none is given)"),
    ("queue", "add [<Job ID>]", "The same as /apply"),
    ("queue", "cancel <Queue ID>", "Cancel a queued or running entry, losing its current run if it has one"),
    ("queue", "resume <Queue ID>", "Resume an interrupted, failed, or cancelled entry from its last succeeded run"),
    ("stop", "", "Cancel the entry the draw-things-cli pane is following"),
    ("get", "history", "Read the execution history again"),
    ("get", "prompts <Execution ID> [RUN]", "An execution's positive and negative prompts (every pair, or one run's)"),
    ("get", "positive <Execution ID> [RUN]", "An execution's positive prompts (every pair, or one run's)"),
    ("get", "negative <Execution ID> [RUN]", "An execution's negative prompts (every pair, or one run's)"),
    ("get", "param <Execution ID> [RUN]", "A run's draw-things-cli arguments without the prompts, with overridden values (default: its first run)"),
    ("get", "parameters <Execution ID> [RUN]", "The same as /get param"),
    ("describe", "execution <Execution ID>", "The detail of one execution"),
    ("filter", "status STATUS", f"Show only {', '.join(STATUSES)} executions"),
    ("filter", "name TEXT", "Show only executions whose job name or file name contains TEXT"),
    ("filter", "off", "Remove the history filters"),
    ("reveal", "<Execution ID> [RUN]", "Reveal a run's output in Finder (default: the last output)"),
    ("clear", "", "Clear the messages"),
    ("quit", "", "Quit; dtc serve keeps running whatever it is running"),
)
COMMAND_NAMES = tuple(dict.fromkeys(name for name, _, _ in COMMANDS))
# The words after /get, /describe, and /queue, in the order help lists them.
GET_WORDS = tuple(dict.fromkeys(arguments.split()[0] for name, arguments, _ in COMMANDS if name == "get"))
DESCRIBE_WORDS = ("job", "queue", "execution")
QUEUE_WORDS = ("add", "cancel", "resume")
# The Job Definition widget's sort keys, in the order `s` moves through them, and the directions.
SORT_KEYS = ("id", "name", "changed", "mode", "runs")
SORT_DIRECTIONS = ("asc", "desc")
FILTER_WORDS = ("status", "name", "off")
# Characters a shell would split or interpret, escaped with a backslash in a completed job file name.
SHELL_SPECIAL = re.compile(r"([^\w@%+=:,./-])")
KEYS = (
    ("Enter", "Run the command; describe the job (Job Definition); describe the entry (Queue); move to the detail (Execution History); reveal the selected run (Execution)"),
    ("Tab", "Complete the command line, or move to Job Definition, Queue, Execution History, then Execution"),
    ("Up/Down", "Recall this session's commands (command line), move (the widgets)"),
    ("a", "Ask to submit the selected job (Job Definition)"),
    ("c", "Cancel the selected entry (Queue)"),
    ("s / r", "Sort by the next column / reverse the order (Job Definition)"),
    ("Escape", "Clear the command line, or go back to it (the widgets)"),
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


def usage(name: str, word: str | None = None) -> str:
    """Every form of a command, as help lists them; with ``word``, only the forms that begin with it (``/get positive``)."""
    forms = [arguments for command, arguments, _ in COMMANDS if command == name and (word is None or arguments.split()[:1] == [word])]
    return " | ".join(f"{PREFIX}{name} {arguments}".strip() for arguments in forms)


def help_text() -> Text:
    """The commands and keys, for the messages."""
    text = Text("Commands\n", style="bold")
    forms = [(f"{PREFIX}{name} {arguments}".strip(), action) for name, arguments, action in COMMANDS]
    width = max(len(form) for form, _ in forms)
    for form, action in forms:
        text.append(f"  {form.ljust(width)}  ", style="bold")
        text.append(f"{action}\n")
    text.append("<Job ID> is a job ID (J0001), a job file name in the data directory, or its name without the suffix. Quote a name with spaces.\n", style="dim")
    text.append("<Execution ID> is an execution ID (E0012), and RUN one of its run numbers. The letter's case and the leading zeros do not matter.\n", style="dim")
    text.append("Keys\n", style="bold")
    width = max(len(key) for key, _ in KEYS)
    for key, action in KEYS:
        text.append(f"  {key.ljust(width)}  ", style="bold")
        text.append(f"{action}\n")
    text.rstrip()
    return text


def _job_name_completion(line: str, jobs: Sequence[str]) -> list[str] | None:
    """Completions for /apply, /describe job, and /queue add's own JOB argument; None when ``line`` is none of those."""
    command, space, rest = line.partition(" ")
    if space and command.lower() == f"{PREFIX}apply":
        return [f"{command} {name}" for name in job_completions(rest, jobs)]
    word, space_after_word, name = rest.partition(" ")
    if space and space_after_word and command.lower() == f"{PREFIX}describe" and word.lower() == "job":
        return [f"{command} {word} {completed}" for completed in job_completions(name, jobs)]
    if space and space_after_word and command.lower() == f"{PREFIX}queue" and word.lower() == "add":
        return [f"{command} {word} {completed}" for completed in job_completions(name, jobs)]
    return None


def completions(line: str, job_names: Sequence[str], job_ids: Sequence[str] = (), execution_ids: Sequence[str] = (), queue_ids: Sequence[str] = ()) -> list[str]:
    """Whole command lines that ``line`` could be completed to, in order; each one begins with ``line``, as Input needs.

    ``job_names`` and ``job_ids`` (J0001) complete a JOB; ``execution_ids`` (E0012, newest first) and ``queue_ids``
    (Q0007, newest first) each complete their own kind of ID.
    """
    job_completion = _job_name_completion(line, [*job_ids, *job_names])
    if job_completion is not None:
        return job_completion
    head, _, last = line.rpartition(" ")
    words = [word.lower() for word in head.split()]
    if not words:
        candidates = [f"{PREFIX}{name}" for name in COMMAND_NAMES] if not head else []
    elif words == [f"{PREFIX}get"]:
        candidates = list(GET_WORDS)
    elif words == [f"{PREFIX}queue"]:
        candidates = list(QUEUE_WORDS)
    elif words == [f"{PREFIX}describe"]:
        # The noun may be left out, so a typed J, E, or Q completes to an ID too (in any case, as after the noun).
        matching = [f"{head} {word}" for word in DESCRIBE_WORDS if word.startswith(last) and word != last]
        return [*matching, *_identifier_completions(head, last, (*job_ids, *execution_ids, *queue_ids))] if last else matching
    elif words in ([f"{PREFIX}describe", "execution"], [f"{PREFIX}reveal"]) or (len(words) == 2 and words[0] == f"{PREFIX}get" and words[1] in ("prompts", "positive", "negative", "param", "parameters")):
        return _identifier_completions(head, last, execution_ids)
    elif words in ([f"{PREFIX}describe", "queue"], [f"{PREFIX}queue", "cancel"], [f"{PREFIX}queue", "resume"]):
        return _identifier_completions(head, last, queue_ids)
    elif words == [f"{PREFIX}filter"]:
        candidates = list(FILTER_WORDS)
    elif words == [f"{PREFIX}filter", "status"]:
        candidates = list(STATUSES)
    elif words == [f"{PREFIX}sort"]:
        candidates = ["jobs"]
    elif words == [f"{PREFIX}sort", "jobs"]:
        candidates = list(SORT_KEYS)
    elif len(words) == 3 and words[:2] == [f"{PREFIX}sort", "jobs"]:
        candidates = list(SORT_DIRECTIONS)
    else:
        candidates = []
    prefix = f"{head} " if head or line.startswith(" ") else ""
    return [f"{prefix}{candidate}" for candidate in candidates if candidate.startswith(last) and candidate != last]


def _identifier_completions(head: str, last: str, identifiers: Sequence[str]) -> list[str]:
    """Whole command lines finishing an ID (J0001, E0012) that starts with ``last``, kept in the case it was typed."""
    return [f"{head} {last}{identifier[len(last) :]}" for identifier in identifiers if identifier.lower().startswith(last.lower()) and identifier.lower() != last.lower()]


def job_completions(typed: str, job_names: Sequence[str]) -> list[str]:
    """Each job file name, written so it parses as one argument, that begins with ``typed``.

    Inside a quote the user opened, the name is closed with the same quote; otherwise its special characters are escaped
    with backslashes, so ``my`` completes to ``my\\ job.yaml`` and the suggestion still begins with what was typed.
    """
    quote = typed[:1] if typed[:1] in ("'", '"') else ""
    written = [f"{quote}{name}{quote}" if quote else SHELL_SPECIAL.sub(r"\\\1", name) for name in job_names if not quote or quote not in name]
    return [name for name in written if name.startswith(typed) and name != typed]


class CommandSuggester(Suggester):
    """Suggests the rest of a command name, a job, an execution or queue ID, or a filter or sort word; each list is
    read on each call."""

    def __init__(self, job_names: Callable[[], list[str]], job_ids: Callable[[], list[str]] = list, execution_ids: Callable[[], list[str]] = list, queue_ids: Callable[[], list[str]] = list) -> None:
        # No cache: the lists change when the data directory, the history, and the queue are read again.
        super().__init__(use_cache=False, case_sensitive=True)
        self.job_names = job_names
        self.job_ids = job_ids
        self.execution_ids = execution_ids
        self.queue_ids = queue_ids

    async def get_suggestion(self, value: str) -> str | None:
        found = completions(value, self.job_names(), self.job_ids(), self.execution_ids(), self.queue_ids())
        return found[0] if found else None
