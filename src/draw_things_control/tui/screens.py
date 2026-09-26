"""The main screen, which lays out the panes and runs the commands, and the confirmation dialog."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, RichLog, Rule, Static

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_events import JobEvent, JobStarted, RunFinished, RunStarted
from draw_things_control.jobs.job_service import JobService
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id
from draw_things_control.tui.commands import GET_WORDS, SORT_DIRECTIONS, SORT_KEYS, CommandError, CommandSuggester, help_text, parse, usage
from draw_things_control.tui.history import STATUSES, HistoryFilter, HistoryReader, copy_to_pasteboard, execution_label, parse_id, reveal_run
from draw_things_control.tui.job_files import JobCatalog, JobDetails, JobListing, add_plan, read_details
from draw_things_control.tui.panes import CliPane, ExecutionPane, HistoryPane, JobDefinitionPane, StatusPane, natural_descending
from draw_things_control.tui.text import Arguments, PreviousRun, argument_rows, details_text, event_text, execution_text, jobs_text, override_notes, parameters_text, prompts_text, question_text, result_text, status_line_text
from draw_things_control.tui.widgets import MAX_MESSAGE_LINES, CommandInput, MessageLog

if TYPE_CHECKING:
    from draw_things_control.tui.app import DrawThingsApp

# The draw-things-cli pane's height with its border, and the least it and Messages may shrink to on a short terminal.
CLI_PANE_LINES = 15
CLI_PANE_MIN_LINES = 7
MESSAGES_MIN_LINES = 6
# The rows under the panes: a rule, the command line, a rule, and the status line.
BOTTOM_LINES = 4
# The Status widget above the draw-things-cli pane, with its border (styles.tcss sets the same height).
STATUS_LINES = 7


class MainScreen(Screen[None]):
    """Lays out the panes, runs the typed commands, and passes the running job's events to the panes.

    The draw-things-cli pane and the status line render from the app's LiveRun; Messages keeps the command output and the job's log.
    """

    def __init__(self) -> None:
        super().__init__()
        # The data directory's job files and their IDs; the Job Definition widget reads through it.
        self.catalog: JobCatalog | None = None
        self.reader: HistoryReader | None = None

    @property
    def dtc(self) -> DrawThingsApp:
        return cast("DrawThingsApp", self.app)

    @property
    def command_line(self) -> CommandInput:
        return self.query_one(CommandInput)

    @property
    def jobs(self) -> JobDefinitionPane:
        return self.query_one(JobDefinitionPane)

    @property
    def history(self) -> HistoryPane:
        return self.query_one(HistoryPane)

    @property
    def cli(self) -> CliPane:
        return self.query_one(CliPane)

    @property
    def detail(self) -> ExecutionPane:
        return self.query_one(ExecutionPane)

    @property
    def status(self) -> StatusPane:
        return self.query_one(StatusPane)

    def compose(self) -> ComposeResult:
        # The history pane reads through it; the detail and reveal commands too. Closed when the screen goes.
        self.reader = HistoryReader(self.dtc.settings.history_retention_days)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield StatusPane(id="status")
                yield CliPane(id="cli")
                yield MessageLog(id="messages", max_lines=MAX_MESSAGE_LINES, wrap=True, min_width=20)
            with Vertical(id="right"):
                # First in the right column, so Tab goes command line, Job Definition, Execution History, Execution.
                self.catalog = JobCatalog(self.dtc.data_directory, self.dtc.settings)
                yield JobDefinitionPane(self.catalog, describe=self.describe_job, run=self.dtc.start_flow, announce=self.announce_jobs, leave=self.focus_command_line, id="jobs")
                yield HistoryPane(self.reader, busy=lambda: self.dtc.job_running, leave=self.focus_command_line, id="history")
                yield ExecutionPane(self.reader, say=self.say, leave=self.focus_command_line, id="execution")
        yield Rule(line_style="solid", classes="command-rule")
        with Horizontal(id="command-line"):
            yield Static("> ", id="prompt")
            yield CommandInput(id="command", compact=True, suggester=CommandSuggester(lambda: self.jobs.job_names, lambda: self.jobs.job_ids, self.execution_ids))
        yield Rule(line_style="solid", classes="command-rule")
        yield Static(id="status-line")

    def on_mount(self) -> None:
        self.query_one(MessageLog).border_title = "Messages"
        # Tab moves from the command line to the history and the detail; the logs scroll with the mouse.
        for log in self.query(RichLog):
            log.can_focus = False
        self.command_line.focus()
        self.say(Text("Type /help for the commands.", style="dim"))
        self.set_interval(1, self.tick)
        self.render_live()

    def on_unmount(self) -> None:
        if self.reader is not None:
            self.reader.close()
        if self.catalog is not None:
            self.catalog.close()

    def on_resize(self, event: events.Resize) -> None:
        # The draw-things-cli pane gives up lines, down to its least, so Messages keeps its least on a short terminal.
        left = event.size.height - BOTTOM_LINES - STATUS_LINES
        self.cli.styles.height = max(CLI_PANE_MIN_LINES, min(CLI_PANE_LINES, left - MESSAGES_MIN_LINES))

    def say(self, text: Text | str, style: str = "", *, block: bool = False) -> None:
        """Write to Messages; ``block`` starts a block of its own (a command echo, the job's result) after a blank line."""
        self.query_one(MessageLog).say(text, style, block=block)

    def execution_ids(self) -> list[str]:
        """The loaded executions' IDs, newest first, for completion."""
        return [execution_label(row) for row in self.history.executions.values()]

    def focus_command_line(self) -> None:
        self.command_line.focus()

    # Commands

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.clear()
        line = event.value.strip()
        if not line:
            return
        self.command_line.remember(line)
        self.say(Text(f"> {line}", style="bold"), block=True)
        try:
            command = parse(line)
        except CommandError as error:
            self.say(str(error), "red")
            return
        if command is not None:
            self.run_command(command.name, command.arguments)

    def run_command(self, name: str, arguments: tuple[str, ...]) -> None:
        """Call ``command_<name>``; arguments that do not fit its signature, or a CommandError, print the reason."""
        handler = getattr(self, f"command_{name}")
        try:
            inspect.signature(handler).bind(*arguments)
        except TypeError:
            self.say(f"Usage: {usage(name)}", "red")
            return
        try:
            handler(*arguments)
        except CommandError as error:
            self.say(str(error), "red")

    def command_help(self) -> None:
        self.say(help_text())

    def command_clear(self) -> None:
        self.query_one(MessageLog).clear()

    def command_quit(self) -> None:
        self.call_later(self.dtc.action_quit)

    def command_get(self, what: str, *arguments: str) -> None:
        """/get jobs, /get history, and an execution's prompts or draw-things-cli arguments."""
        word = what.lower()
        if word == "jobs" and not arguments:
            self.jobs.load(fresh=True, announce=True)
        elif word == "history" and not arguments:
            self.history.load()
        elif word in ("prompts", "positive", "negative", "param", "parameters") and 1 <= len(arguments) <= 2:
            execution = self.execution_number(arguments[0], "get", word)
            run = self.number(arguments[1], "get", word) if len(arguments) == 2 else None
            self.show_part(word, execution, run)
        else:
            raise CommandError(f"Usage: {usage('get', word if word in GET_WORDS else None)}")

    def command_describe(self, what: str, *arguments: str) -> None:
        """/describe job JOB: the summary, prompt pairs, and dry-run plan of a job file; /describe execution ID: one
        execution as it ran."""
        word = what.lower()
        if word == "job" and len(arguments) == 1:
            self.describe_job(self.job_path(arguments[0]))
        elif word == "execution" and len(arguments) == 1:
            self.show_execution(self.execution_number(arguments[0], "describe", "execution"))
        else:
            raise CommandError(f"Usage: {usage('describe', word if word in ('job', 'execution') else None)}")

    def describe_job(self, path: Path) -> None:
        self.load_details(path, self.dtc.settings, self.dtc.job_service, self.dtc.executable)

    def command_sort(self, what: str, key: str, direction: str | None = None) -> None:
        """/sort jobs KEY [asc|desc]: the Job Definition widget's order, kept across sessions."""
        key = key.lower()
        if what.lower() != "jobs" or key not in SORT_KEYS or (direction is not None and direction.lower() not in SORT_DIRECTIONS):
            raise CommandError(f"Usage: {usage('sort')}; KEY is {', '.join(SORT_KEYS)}")
        descending = direction.lower() == "desc" if direction is not None else natural_descending(key)
        self.jobs.set_sort(key, descending)
        self.say(f"Job Definition: by {key}, {'descending' if descending else 'ascending'}")

    def command_apply(self, name: str) -> None:
        self.dtc.start_flow(self.job_path(name))

    def command_stop(self) -> None:
        live = self.dtc.live
        if not self.dtc.job_running or live is None:
            self.say("No job is running", "yellow")
            return
        if live.stop_requested:
            self.say("Already stopping", "yellow")
            return
        if any(isinstance(screen, ConfirmScreen) for screen in self.app.screen_stack):
            return
        self.app.push_screen(ConfirmScreen(question_text("Stop the job?", "stop it", "keep it running"), purpose="stop"), self.confirm_stop)

    def confirm_stop(self, stop: bool | None) -> None:
        if stop:
            self.dtc.request_stop()

    def command_filter(self, *arguments: str) -> None:
        current = self.history.history_filter
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
        self.say(f"Execution History: {history_filter.text() or 'all executions'}")
        self.history.set_filter(history_filter)

    def command_reveal(self, execution_id: str, run: str | None = None) -> None:
        self.reveal(self.execution_number(execution_id, "reveal"), self.number(run, "reveal") if run is not None else None)

    def job_path(self, name: str) -> Path:
        assert self.catalog is not None
        path = self.catalog.find(name, self.jobs.job_rows)
        if isinstance(path, str):
            raise CommandError(path)
        return path

    @staticmethod
    def execution_number(text: str, command: str, word: str | None = None) -> int:
        """The number of an execution ID as typed (E0012, e12), or why not: a bare number names the E form."""
        number = parse_typed_id(text, EXECUTION_LETTER)
        if number is not None:
            return number
        bare = parse_id(text)
        if bare is not None:
            raise CommandError(f"Use {execution_id_text(bare)}: an execution ID begins with {EXECUTION_LETTER}")
        raise CommandError(f"Usage: {usage(command, word)}")

    @staticmethod
    def number(text: str, command: str, word: str | None = None) -> int:
        """A run number, or a usage error for ``command`` (and its ``word``, for /get)."""
        number = parse_id(text)
        if number is None:
            raise CommandError(f"Usage: {usage(command, word)}")
        return number

    # Workers: jobs, the detail, and reveal

    def announce_jobs(self, listing: JobListing) -> None:
        """/get jobs: the listing the Job Definition widget just read, in Messages."""
        running = self.dtc.live.path.name if self.dtc.job_running and self.dtc.live is not None else None
        self.say(jobs_text(listing.rows, running, listing.message))
        if listing.id_error is not None:
            self.say(listing.id_error, "yellow")

    @work(thread=True, group="details")
    def load_details(self, path: Path, settings: GlobalConfig, service: JobService, executable: str) -> None:
        details = read_details(path, settings)
        if details.job is not None:
            details = add_plan(details, service, executable)
        self.app.call_from_thread(self.show_details, path, details)

    def show_details(self, path: Path, details: JobDetails) -> None:
        if not self.is_attached:
            return
        if details.job is None:
            self.say(Text(f"Invalid job: {path}\n{details.error}", style="red"))
            return
        row = next((row for row in self.jobs.job_rows if row.path == path), None)
        self.say(details_text(details.job, details, row.job_id if row is not None else None))

    # The history and the detail

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The detail follows the history's cursor, after a pause.
        if event.data_table is self.history and event.row_key.value is not None:
            self.detail.follow(int(str(event.row_key.value)))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on the history moves to the detail, at its first run; /describe execution still writes the full detail to Messages.
        if event.data_table is not self.history:
            return
        execution_id = int(str(event.row_key.value))
        detail = self.detail
        detail.focus()
        if detail.execution_id == execution_id:
            detail.action_jump(0)
        else:
            detail.follow(execution_id, pause=False)

    def on_history_pane_page_shown(self, event: HistoryPane.PageShown) -> None:
        # A re-read may keep the cursor where it was, which highlights nothing; read the detail again all the same.
        if event.execution_id is None:
            self.detail.follow(None)
            self.detail.show_message(event.message or "No execution selected")
        else:
            self.detail.follow(event.execution_id)

    def on_history_pane_rows_updated(self, event: HistoryPane.RowsUpdated) -> None:
        if self.detail.execution_id in event.execution_ids:
            self.detail.follow(self.detail.execution_id, pause=False)

    def on_history_pane_lock_changed(self, event: HistoryPane.LockChanged) -> None:
        self.render_status()

    @work(thread=True, group="execution")
    def show_execution(self, number: int) -> None:
        assert self.reader is not None
        execution = self.reader.numbered(number)
        self.app.call_from_thread(self.say, execution_text(execution) if isinstance(execution, dict) else Text(execution, style="red"))

    @work(thread=True, group="execution")
    def show_part(self, word: str, number: int, run: int | None) -> None:
        """An execution's prompts (``prompts``, ``positive``, ``negative``) or its arguments (``param``, ``parameters``)."""
        assert self.reader is not None
        execution = self.reader.numbered(number)
        if isinstance(execution, str):
            self.app.call_from_thread(self.say, execution, "red")
            return
        if word in ("param", "parameters"):
            self.app.call_from_thread(self.say, parameters_text(execution, run))
            return
        text, copied = prompts_text(execution, word, run)
        self.app.call_from_thread(self.say, text)
        if copied is not None:
            copied, what = copied
            error = copy_to_pasteboard(copied)
            if error is None:
                self.app.call_from_thread(self.say, f"Copied the {what} to the clipboard", "dim")
            else:
                # Without pbcopy, the terminal is asked to copy (OSC 52); not every terminal does, so it cannot be confirmed.
                self.app.call_from_thread(self.app.copy_to_clipboard, copied)
                self.app.call_from_thread(self.say, f"Asked the terminal to copy the {what} ({error})", "dim")

    @work(thread=True, group="reveal")
    def reveal(self, number: int, run: int | None) -> None:
        assert self.reader is not None
        self.app.call_from_thread(self.say, *reveal_run(self.reader.numbered(number), run))

    # The running job

    def job_started(self) -> None:
        """A job starts: its output replaces the last job's."""
        if self.dtc.live is not None:
            self.cli.new_job(self.dtc.live)
        self.tick()

    def run_arguments(self, event: JobEvent) -> tuple[Arguments | None, PreviousRun | None]:
        """For a run with a command: its argument rows, and the last such run of this job to compare them with. The last
        run is kept on the job's LiveRun, which each job starts afresh; a run without a command is never compared with."""
        live = self.dtc.live
        if not isinstance(event, RunStarted) or not event.command:
            return None, None
        started = live.started if live is not None else None
        notes = override_notes(started.config_override, started.input_resize is not None) if started is not None else {}
        arguments = argument_rows(event.command, notes)
        if live is None:
            return arguments, None
        previous, live.previous_arguments = live.previous_arguments, (event.number, arguments)
        return arguments, previous

    def job_event(self, event: JobEvent) -> None:
        """Log the event, update the draw-things-cli pane, and refresh the history where the store changed."""
        text = event_text(event, *self.run_arguments(event))
        if text is not None:
            self.say(text)
        self.render_live(event)
        # A new execution needs its row; a finished run changes only that row. The end of the job reads the pane again.
        if isinstance(event, JobStarted):
            # The cursor moves to the new execution, unless the person is browsing the history or the detail.
            if self.focused not in (self.history, self.detail):
                self.history.select_when_shown = self.dtc.execution_id
            self.history.load()
        elif isinstance(event, RunFinished) and self.dtc.execution_id is not None:
            self.history.refresh_rows([self.dtc.execution_id])

    def job_ended(self) -> None:
        live = self.dtc.live
        if live is not None:
            self.say(result_text(live), block=True)
        self.render_live()
        self.history.load()

    def render_live(self, event: JobEvent | None = None) -> None:
        self.cli.show(self.dtc.live, event)
        self.tick()

    def render_status(self) -> None:
        self.status.show(self.dtc.live, self.history.other_process_running)

    def tick(self) -> None:
        """Update the elapsed time, the cooldown countdown, the Status widget, and the status line."""
        self.cli.tick(self.dtc.live)
        self.render_status()
        self.query_one("#status-line", Static).update(status_line_text(self.dtc.data_directory, self.dtc.live, self.dtc.job_running, self.dtc.quit_armed))


class ConfirmScreen(ModalScreen[bool]):
    """A yes-or-no question over the current screen; ``purpose`` names it (``run``, ``stop``, ``quit``).

    With ``enter_confirms`` false, only ``y`` answers yes: Enter also submits the command that opened the dialog,
    so a second Enter, or a held key, must not answer it.
    """

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("enter", "enter", "Yes", show=False),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, text: Text, *, purpose: str, enter_confirms: bool = True) -> None:
        super().__init__()
        self.text = text
        self.purpose = purpose
        self.enter_confirms = enter_confirms

    def compose(self) -> ComposeResult:
        yield Static(self.text, id="confirm")

    def action_enter(self) -> None:
        if self.enter_confirms:
            self.dismiss(True)

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)
