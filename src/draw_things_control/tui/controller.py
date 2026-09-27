"""The typed ``/`` commands of the main screen: their arguments are read here, and what does the work is asked of the screen and the app."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

from draw_things_control.core.errors import DtcError
from draw_things_control.services.history import STATUSES, HistoryFilter
from draw_things_control.state.ids import EXECUTION_LETTER, JOB_LETTER, execution_id_text, parse_bare_number, parse_typed_id
from draw_things_control.tui.commands import GET_WORDS, SORT_DIRECTIONS, SORT_KEYS, CommandError, help_text, parse, usage
from draw_things_control.tui.confirm import ConfirmScreen
from draw_things_control.tui.panes.job_definitions import natural_descending
from draw_things_control.tui.text.jobs import question_text
from draw_things_control.tui.widgets import MessageLog

if TYPE_CHECKING:
    from draw_things_control.tui.screens import MainScreen


class CommandController:
    """Runs the commands a person types on the command line, for the main screen."""

    def __init__(self, screen: MainScreen) -> None:
        self.screen = screen

    def submit(self, line: str) -> None:
        """Parse ``line`` (already echoed) and run it; a line that cannot be run says why."""
        try:
            command = parse(line)
        except CommandError as error:
            self.screen.say(str(error), "red")
            return
        if command is not None:
            self.run_command(command.name, command.arguments)

    def run_command(self, name: str, arguments: tuple[str, ...]) -> None:
        """Call ``command_<name>``; arguments that do not fit its signature, or a CommandError, print the reason."""
        handler = getattr(self, f"command_{name}")
        try:
            inspect.signature(handler).bind(*arguments)
        except TypeError:
            self.screen.say(f"Usage: {usage(name)}", "red")
            return
        try:
            handler(*arguments)
        except CommandError as error:
            self.screen.say(str(error), "red")

    def command_help(self) -> None:
        self.screen.say(help_text())

    def command_clear(self) -> None:
        self.screen.query_one(MessageLog).clear()

    def command_quit(self) -> None:
        self.screen.call_later(self.screen.dtc.action_quit)

    def command_get(self, what: str, *arguments: str) -> None:
        """/get jobs, /get history, and an execution's prompts or draw-things-cli arguments."""
        word = what.lower()
        if word == "jobs" and not arguments:
            self.screen.jobs.load(fresh=True, announce=True)
        elif word == "history" and not arguments:
            self.screen.history.load()
        elif word in ("prompts", "positive", "negative", "param", "parameters") and 1 <= len(arguments) <= 2:
            execution = self.execution_number(arguments[0], "get", word)
            run = self.number(arguments[1], "get", word) if len(arguments) == 2 else None
            self.screen.show_part(word, execution, run)
        else:
            raise CommandError(f"Usage: {usage('get', word if word in GET_WORDS else None)}")

    def command_describe(self, what: str, *arguments: str) -> None:
        """/describe job JOB: the summary, prompt pairs, and dry-run plan of a job file; /describe execution ID: one
        execution as it ran. The noun may be left out when the ID shows it: /describe J0001, /describe e12."""
        word = what.lower()
        if not arguments and word not in ("job", "execution"):
            if parse_typed_id(what, JOB_LETTER) is not None:
                word, arguments = "job", (what,)
            elif parse_typed_id(what, EXECUTION_LETTER) is not None:
                word, arguments = "execution", (what,)
        if word == "job" and len(arguments) == 1:
            self.screen.describe_job(self.job_path(arguments[0]))
        elif word == "execution" and len(arguments) == 1:
            self.screen.show_execution(self.execution_number(arguments[0], "describe", "execution"))
        else:
            raise CommandError(f"Usage: {usage('describe', word if word in ('job', 'execution') else None)}")

    def command_sort(self, what: str, key: str, direction: str | None = None) -> None:
        """/sort jobs KEY [asc|desc]: the Job Definition widget's order, kept across sessions."""
        key = key.lower()
        if what.lower() != "jobs" or key not in SORT_KEYS or (direction is not None and direction.lower() not in SORT_DIRECTIONS):
            raise CommandError(f"Usage: {usage('sort')}; KEY is {', '.join(SORT_KEYS)}")
        descending = direction.lower() == "desc" if direction is not None else natural_descending(key)
        self.screen.jobs.set_sort(key, descending)
        self.screen.say(f"Job Definition: by {key}, {'descending' if descending else 'ascending'}")

    def command_apply(self, name: str) -> None:
        self.screen.dtc.start_flow(self.job_path(name))

    def command_stop(self) -> None:
        live = self.screen.dtc.live
        if not self.screen.dtc.job_running or live is None:
            self.screen.say("No job is running", "yellow")
            return
        if live.stop_requested:
            self.screen.say("Already stopping", "yellow")
            return
        if any(isinstance(screen, ConfirmScreen) for screen in self.screen.app.screen_stack):
            return
        self.screen.app.push_screen(ConfirmScreen(question_text("Stop the job?", "stop it", "keep it running"), purpose="stop"), self.confirm_stop)

    def confirm_stop(self, stop: bool | None) -> None:
        if stop:
            self.screen.dtc.request_stop()

    def command_filter(self, *arguments: str) -> None:
        current = self.screen.history.history_filter
        if arguments == ("off",):
            history_filter = HistoryFilter()
        elif len(arguments) == 2 and arguments[0] == "status":
            if arguments[1] not in STATUSES:
                raise CommandError(f"Unknown status '{arguments[1]}'; use one of {', '.join(STATUSES)}")
            history_filter = HistoryFilter(arguments[1], current.name)
        elif len(arguments) == 2 and arguments[0] == "name" and arguments[1]:
            history_filter = HistoryFilter(current.status, arguments[1])
        else:
            raise CommandError(f"Usage: {usage('filter')}")
        self.screen.say(f"Execution History: {history_filter.text() or 'all executions'}")
        self.screen.history.set_filter(history_filter)

    def command_reveal(self, execution_id: str, run: str | None = None) -> None:
        self.screen.reveal(self.execution_number(execution_id, "reveal"), self.number(run, "reveal") if run is not None else None)

    def job_path(self, name: str) -> Path:
        assert self.screen.catalog is not None
        try:
            return self.screen.catalog.find(name, self.screen.jobs.job_rows)
        except DtcError as error:
            raise CommandError(str(error)) from error

    @staticmethod
    def execution_number(text: str, command: str, word: str | None = None) -> int:
        """The number of an execution ID as typed (E0012, e12), or why not: a bare number names the E form."""
        number = parse_typed_id(text, EXECUTION_LETTER)
        if number is not None:
            return number
        bare = parse_bare_number(text)
        if bare is not None:
            raise CommandError(f"Use {execution_id_text(bare)}: an execution ID begins with {EXECUTION_LETTER}")
        raise CommandError(f"Usage: {usage(command, word)}")

    @staticmethod
    def number(text: str, command: str, word: str | None = None) -> int:
        """A run number, or a usage error for ``command`` (and its ``word``, for /get)."""
        number = parse_bare_number(text)
        if number is None:
            raise CommandError(f"Usage: {usage(command, word)}")
        return number
