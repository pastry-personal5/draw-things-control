"""The typed ``/`` commands of the main screen: their arguments are read here, and what does the work is asked of the screen and the app."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, cast

from draw_things_control.core.errors import DtcError
from draw_things_control.services.history import STATUSES, HistoryFilter
from draw_things_control.state.ids import EXECUTION_LETTER, JOB_LETTER, QUEUE_LETTER, execution_id_text, parse_bare_number, parse_typed_id, queue_id_text
from draw_things_control.tui.commands import DELETE_WORDS, GET_WORDS, SORT_DIRECTIONS, SORT_KEYS, VERBOSE_WORDS, CommandError, help_text, parse, usage
from draw_things_control.tui.deletion import DeleteSelection
from draw_things_control.tui.panes.job_definitions import natural_descending
from draw_things_control.tui.widgets import MessageLog

if TYPE_CHECKING:
    from draw_things_control.tui.preferences import VerboseLevel
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
        """/get jobs, /get queue, /get history, and an execution's prompts or draw-things-cli arguments."""
        word = what.lower()
        if word == "jobs" and not arguments:
            self.screen.jobs.load(fresh=True, announce=True)
        elif word == "queue" and not arguments:
            self.screen.dtc.show_queue()
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
        execution as it ran; /describe Q0007: one queue entry. The noun may be left out when the ID shows it:
        /describe J0001, /describe e12, /describe Q7."""
        word = what.lower()
        if not arguments and word not in ("job", "execution", "queue"):
            if parse_typed_id(what, JOB_LETTER) is not None:
                word, arguments = "job", (what,)
            elif parse_typed_id(what, EXECUTION_LETTER) is not None:
                word, arguments = "execution", (what,)
            elif parse_typed_id(what, QUEUE_LETTER) is not None:
                word, arguments = "queue", (what,)
        if word == "job" and len(arguments) == 1:
            self.screen.describe_job(self.job_path(arguments[0]))
        elif word == "execution" and len(arguments) == 1:
            self.screen.show_execution(self.execution_number(arguments[0], "describe", "execution"))
        elif word == "queue" and len(arguments) == 1:
            self.screen.dtc.describe_queue_entry(self.queue_id(arguments[0]))
        else:
            raise CommandError(f"Usage: {usage('describe', word if word in ('job', 'execution', 'queue') else None)}")

    def command_sort(self, what: str, key: str, direction: str | None = None) -> None:
        """/sort jobs KEY [asc|desc]: the Job Definition widget's order, kept across sessions."""
        key = key.lower()
        if what.lower() != "jobs" or key not in SORT_KEYS or (direction is not None and direction.lower() not in SORT_DIRECTIONS):
            raise CommandError(f"Usage: {usage('sort')}; KEY is {', '.join(SORT_KEYS)}")
        descending = direction.lower() == "desc" if direction is not None else natural_descending(key)
        self.screen.jobs.set_sort(key, descending)
        self.screen.say(f"Job Definition: by {key}, {'descending' if descending else 'ascending'}")

    def command_verbose(self, level: str = "") -> None:
        """/verbose [high|medium|low]: show the level, or set it (kept across sessions)."""
        word = level.lower()
        if not word:
            self.screen.say(f"Verbose level: {self.screen.dtc.verbose_level}.")
        elif word in VERBOSE_WORDS:
            self.screen.dtc.set_verbose_level(cast("VerboseLevel", word))
        else:
            raise CommandError(f"Usage: {usage('verbose')}")

    def command_apply(self, name: str | None = None) -> None:
        """/apply [JOB]: an alias for /queue add, the current Job Definition file when JOB is left out."""
        self._queue_add(name)

    def command_queue(self, action: str, *arguments: str) -> None:
        """/queue add [JOB], cancel, resume, park, and unpark <Queue ID>, hold, and release: each calls the API, no
        confirmation (a submission is undone with /queue cancel, a park with /queue unpark, a hold with /queue release;
        the server, not this process, runs anything)."""
        word = action.lower()
        dtc = self.screen.dtc
        if word == "add" and len(arguments) <= 1:
            self._queue_add(arguments[0] if arguments else None)
        elif word == "cancel" and len(arguments) == 1:
            dtc.cancel_entry(self.queue_id(arguments[0]))
        elif word == "resume" and len(arguments) == 1:
            dtc.resume_entry(self.queue_id(arguments[0]))
        elif word == "park" and len(arguments) == 1:
            dtc.park_entry(self.queue_id(arguments[0]))
        elif word == "unpark" and len(arguments) == 1:
            dtc.unpark_entry(self.queue_id(arguments[0]))
        elif word == "hold" and not arguments:
            dtc.hold_queue()
        elif word == "release" and not arguments:
            dtc.release_queue()
        else:
            raise CommandError(f"Usage: {usage('queue')}")

    def _queue_add(self, name: str | None) -> None:
        if name is None:
            row = self.screen.jobs.selected
            if row is None:
                raise CommandError("No job is selected")
            self.screen.dtc.start_flow(row.path)
            return
        self.screen.dtc.start_flow(self.job_path(name))

    def command_stop(self) -> None:
        """/stop: an alias for /queue cancel on the entry the draw-things-cli pane is following, no confirmation
        (the same rule /queue cancel and c already give)."""
        live = self.screen.dtc.live
        if live is None or live.ended or live.queue_id is None:
            self.screen.say("No job is running", "yellow")
            return
        if live.stop_requested:
            self.screen.say("Already stopping", "yellow")
            return
        self.screen.dtc.cancel_entry(live.queue_id)

    def command_park(self) -> None:
        """/park: /queue park on the entry the draw-things-cli pane is following. On one already parking it parks again
        only when a release has ended the hold, which that holds again."""
        dtc = self.screen.dtc
        live = dtc.live
        if live is None or live.ended or live.queue_id is None:
            self.screen.say("No job is running", "yellow")
            return
        if live.stop_requested:
            self.screen.say("Already stopping", "yellow")
            return
        if live.park_requested and dtc.queue_hold.held:
            self.screen.say("Already parking", "yellow")
            return
        dtc.park_entry(live.queue_id)

    def command_unpark(self) -> None:
        """/unpark: /queue unpark on the entry the draw-things-cli pane is following."""
        live = self.screen.dtc.live
        if live is None or live.ended or live.queue_id is None:
            self.screen.say("No job is running", "yellow")
            return
        if not live.park_requested:
            self.screen.say("Not parking", "yellow")
            return
        self.screen.dtc.unpark_entry(live.queue_id)

    def command_hold(self) -> None:
        """/hold: the same as /queue hold."""
        self.screen.dtc.hold_queue()

    def command_release(self) -> None:
        """/release: the same as /queue release."""
        self.screen.dtc.release_queue()

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

    def command_delete(self, what: str, *arguments: str) -> None:
        """/delete execution <IDs...>, /delete filtered, and /delete all (Milestone 06): each asks first, in a dialog,
        and deletes through the API. The whole history needs ``all`` written out, so a filter left off by mistake
        cannot select it."""
        word = what.lower()
        dtc = self.screen.dtc
        if word == "execution" and arguments:
            dtc.delete_executions(DeleteSelection(tuple(dict.fromkeys(self.execution_number(argument, "delete", "execution") for argument in arguments))))
        elif word == "filtered" and not arguments:
            history_filter = self.screen.history.history_filter
            if not history_filter.text():
                raise CommandError("No filter is set; use /delete all to delete the whole history")
            dtc.delete_executions(DeleteSelection(history_filter=history_filter))
        elif word == "all" and not arguments:
            dtc.delete_executions(DeleteSelection(history_filter=HistoryFilter()))
        else:
            raise CommandError(f"Usage: {usage('delete', word if word in DELETE_WORDS else None)}")

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
    def queue_id(text: str) -> str:
        """A queue entry's ID (Q0007), normalized to that form; refuses anything else."""
        number = parse_typed_id(text, QUEUE_LETTER)
        if number is None:
            raise CommandError(f"'{text}' is not a queue ID; it should look like {queue_id_text(1)}")
        return queue_id_text(number)

    @staticmethod
    def number(text: str, command: str, word: str | None = None) -> int:
        """A run number, or a usage error for ``command`` (and its ``word``, for /get)."""
        number = parse_bare_number(text)
        if number is None:
            raise CommandError(f"Usage: {usage(command, word)}")
        return number
