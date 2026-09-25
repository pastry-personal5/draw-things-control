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
from textual.worker import get_current_worker

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_events import JobEvent, JobStarted, RunFinished
from draw_things_control.jobs.job_service import JobService
from draw_things_control.tui.commands import CommandError, CommandSuggester, help_text, parse, usage
from draw_things_control.tui.history import STATUSES, HistoryFilter, HistoryReader, parse_id, reveal_in_finder, reveal_target
from draw_things_control.tui.job_files import JobDetails, JobRow, add_plan, find_job, read_details, read_rows
from draw_things_control.tui.panes import CliPane, HistoryPane
from draw_things_control.tui.text import details_text, event_text, execution_text, jobs_text, question_text, result_text, status_line_text
from draw_things_control.tui.widgets import MAX_MESSAGE_LINES, CommandInput, MessageLog

if TYPE_CHECKING:
    from draw_things_control.tui.app import DrawThingsApp

# The draw-things-cli pane's height with its border, and the least it and Messages may shrink to on a short terminal.
CLI_PANE_LINES = 15
CLI_PANE_MIN_LINES = 7
MESSAGES_MIN_LINES = 6
# The rows under the panes: a rule, the command line, a rule, and the status line.
BOTTOM_LINES = 4


class MainScreen(Screen[None]):
    """Lays out the panes, runs the typed commands, and passes the running job's events to the panes.

    The draw-things-cli pane and the status line render from the app's LiveRun; Messages keeps the command output and the job's log.
    """

    def __init__(self) -> None:
        super().__init__()
        # Job file names for completion, from the last read of the data directory.
        self.job_names: list[str] = []
        self.reader: HistoryReader | None = None

    @property
    def dtc(self) -> DrawThingsApp:
        return cast("DrawThingsApp", self.app)

    @property
    def command_line(self) -> CommandInput:
        return self.query_one(CommandInput)

    @property
    def history(self) -> HistoryPane:
        return self.query_one(HistoryPane)

    @property
    def cli(self) -> CliPane:
        return self.query_one(CliPane)

    def compose(self) -> ComposeResult:
        # The history pane reads through it; the detail and reveal commands too. Closed when the screen goes.
        self.reader = HistoryReader(self.dtc.settings.history_retention_days)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield CliPane(id="cli")
                yield MessageLog(id="messages", max_lines=MAX_MESSAGE_LINES, wrap=True, min_width=20)
            yield HistoryPane(self.reader, busy=lambda: self.dtc.job_running, id="history")
        yield Rule(line_style="solid", classes="command-rule")
        with Horizontal(id="command-line"):
            yield Static("> ", id="prompt")
            yield CommandInput(id="command", compact=True, suggester=CommandSuggester(lambda: self.job_names))
        yield Rule(line_style="solid", classes="command-rule")
        yield Static(id="status-line")

    def on_mount(self) -> None:
        self.query_one(MessageLog).border_title = "Messages"
        # Tab moves between the command line and the history only; the logs scroll with the mouse.
        for log in self.query(RichLog):
            log.can_focus = False
        self.command_line.focus()
        self.say(Text("Type /help for the commands.", style="dim"))
        self.set_interval(1, self.tick)
        self.render_live()
        self.read_jobs(self.dtc.data_directory, self.dtc.settings, announce=False)

    def on_unmount(self) -> None:
        if self.reader is not None:
            self.reader.close()

    def on_resize(self, event: events.Resize) -> None:
        # The draw-things-cli pane gives up lines, down to its least, so Messages keeps its least on a short terminal.
        left = event.size.height - BOTTOM_LINES
        self.cli.styles.height = max(CLI_PANE_MIN_LINES, min(CLI_PANE_LINES, left - MESSAGES_MIN_LINES))

    def say(self, text: Text | str, style: str = "") -> None:
        self.query_one(MessageLog).say(text, style)

    # Commands

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.clear()
        line = event.value.strip()
        if not line:
            return
        self.command_line.remember(line)
        self.say(Text(f"> {line}", style="bold"))
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

    def command_jobs(self) -> None:
        self.read_jobs(self.dtc.data_directory, self.dtc.settings, announce=True)

    def command_job(self, name: str) -> None:
        self.load_details(self.job_path(name), self.dtc.settings, self.dtc.job_service, self.dtc.executable)

    def command_run(self, name: str) -> None:
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

    def command_history(self) -> None:
        self.history.load()

    def command_execution(self, execution_id: str) -> None:
        self.show_execution(self.number(execution_id, "execution"))

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
        self.say(f"History: {history_filter.text() or 'all executions'}")
        self.history.set_filter(history_filter)

    def command_reveal(self, execution_id: str, run: str | None = None) -> None:
        self.reveal(self.number(execution_id, "reveal"), self.number(run, "reveal") if run is not None else None)

    def job_path(self, name: str) -> Path:
        path = find_job(self.dtc.data_directory, name)
        if isinstance(path, str):
            raise CommandError(path)
        return path

    @staticmethod
    def number(text: str, command: str) -> int:
        """An execution or run number, or a usage error for ``command``."""
        number = parse_id(text)
        if number is None:
            raise CommandError(f"Usage: {usage(command)}")
        return number

    # Workers: jobs, the detail, and reveal

    @work(thread=True, exclusive=True, group="jobs")
    def read_jobs(self, directory: Path, settings: GlobalConfig, *, announce: bool) -> None:
        rows, message = read_rows(directory, settings)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_jobs, rows, message, announce)

    def show_jobs(self, rows: list[JobRow], message: str | None, announce: bool) -> None:
        if not self.is_attached:
            return
        self.job_names = [row.path.name for row in rows]
        if announce or message is not None:
            running = self.dtc.live.path.name if self.dtc.job_running and self.dtc.live is not None else None
            self.say(jobs_text([(row.path.name, row.job, row.error) for row in rows], running, message))

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
        self.say(details_text(details.job, details.plan, details.plan_error))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.show_execution(int(str(event.row_key.value)))

    @work(thread=True, group="execution")
    def show_execution(self, execution_id: int) -> None:
        assert self.reader is not None
        execution = self.reader.execution(execution_id)
        self.app.call_from_thread(self.say, execution_text(execution) if isinstance(execution, dict) else Text(execution, style="red"))

    @work(thread=True, group="reveal")
    def reveal(self, execution_id: int, run: int | None) -> None:
        assert self.reader is not None
        execution = self.reader.execution(execution_id)
        target = reveal_target(execution, run) if isinstance(execution, dict) else execution
        if isinstance(target, str):
            self.app.call_from_thread(self.say, target, "red")
            return
        error = reveal_in_finder(target)
        self.app.call_from_thread(self.say, error or f"Revealed {target}", "red" if error else "")

    # The running job

    def job_started(self) -> None:
        """A job starts: its output replaces the last job's."""
        if self.dtc.live is not None:
            self.cli.new_job(self.dtc.live)
        self.tick()

    def job_event(self, event: JobEvent) -> None:
        """Log the event, update the draw-things-cli pane, and refresh the history where the store changed."""
        text = event_text(event)
        if text is not None:
            self.say(text)
        self.render_live(event)
        # A new execution needs its row; a finished run changes only that row. The end of the job reads the pane again.
        if isinstance(event, JobStarted):
            self.history.load()
        elif isinstance(event, RunFinished) and self.dtc.execution_id is not None:
            self.history.refresh_rows([self.dtc.execution_id])

    def job_ended(self) -> None:
        live = self.dtc.live
        if live is not None:
            self.say(result_text(live))
        self.render_live()
        self.history.load()

    def render_live(self, event: JobEvent | None = None) -> None:
        self.cli.show(self.dtc.live, event)
        self.tick()

    def tick(self) -> None:
        """Update the elapsed time, the cooldown countdown, and the status line."""
        self.cli.tick(self.dtc.live)
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
