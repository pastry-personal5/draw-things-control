"""The main screen, which lays out the panes and runs the commands, and the confirmation dialog."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, RichLog, Rule, Static

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.events import JobEvent, JobStarted, RunFinished
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.services.history import HistoryReader
from draw_things_control.services.job_catalog import JobCatalog, JobListing
from draw_things_control.services.job_details import JobDetails, add_plan, read_details
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.tui.commands import CommandSuggester
from draw_things_control.tui.controller import CommandController
from draw_things_control.tui.desktop import copy_text, reveal_run
from draw_things_control.tui.job_sort import SortPreference
from draw_things_control.tui.panes.cli_output import CliPane
from draw_things_control.tui.panes.execution import ExecutionPane
from draw_things_control.tui.panes.history import HistoryPane
from draw_things_control.tui.panes.job_definitions import JobDefinitionPane
from draw_things_control.tui.panes.status import StatusPane
from draw_things_control.tui.reader import PaneHistory
from draw_things_control.tui.text.arguments import parameters_text, run_arguments
from draw_things_control.tui.text.events import event_text, result_text
from draw_things_control.tui.text.execution import execution_text
from draw_things_control.tui.text.jobs import details_text, jobs_text
from draw_things_control.tui.text.prompts import prompts_text
from draw_things_control.tui.text.status import status_line_text
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
        self.reader: PaneHistory | None = None
        self.store: StoreProvider | None = None
        self.commands = CommandController(self)

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
        self.store = StoreProvider(self.dtc.paths, self.dtc.settings.history_retention_days)
        self.reader = PaneHistory(HistoryReader(self.dtc.paths, self.store))
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield StatusPane(id="status")
                yield CliPane(id="cli")
                yield MessageLog(id="messages", max_lines=MAX_MESSAGE_LINES, wrap=True, min_width=20)
            with Vertical(id="right"):
                # First in the right column, so Tab goes command line, Job Definition, Execution History, Execution.
                self.catalog = JobCatalog(self.dtc.data_directory, self.dtc.settings, self.dtc.paths, self.store)
                yield JobDefinitionPane(self.catalog, SortPreference(self.store), describe=self.describe_job, run=self.dtc.start_flow, announce=self.announce_jobs, leave=self.focus_command_line, id="jobs")
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
        if self.store is not None:
            self.store.close()

    def on_resize(self, event: events.Resize) -> None:
        # The draw-things-cli pane gives up lines, down to its least, so Messages keeps its least on a short terminal.
        left = event.size.height - BOTTOM_LINES - STATUS_LINES
        self.cli.styles.height = max(CLI_PANE_MIN_LINES, min(CLI_PANE_LINES, left - MESSAGES_MIN_LINES))

    def say(self, text: Text | str, style: str = "", *, block: bool = False) -> None:
        """Write to Messages; ``block`` starts a block of its own (a command echo, the job's result) after a blank line."""
        self.query_one(MessageLog).say(text, style, block=block)

    def execution_ids(self) -> list[str]:
        """The loaded executions' IDs, newest first, for completion."""
        return [row.label for row in self.history.executions.values()]

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
        self.commands.submit(line)

    def describe_job(self, path: Path) -> None:
        self.load_details(path, self.dtc.settings, self.dtc.job_executor, self.dtc.executable)

    # Workers: jobs, the detail, and reveal

    def announce_jobs(self, listing: JobListing) -> None:
        """/get jobs: the listing the Job Definition widget just read, in Messages."""
        running = self.dtc.live.path.name if self.dtc.job_running and self.dtc.live is not None else None
        self.say(jobs_text(listing.rows, running, listing.message))
        if listing.id_error is not None:
            self.say(listing.id_error, "yellow")

    @work(thread=True, group="details")
    def load_details(self, path: Path, settings: GlobalConfig, executor: JobExecutor, executable: str) -> None:
        details = read_details(path, settings, self.dtc.paths)
        if details.job is not None:
            details = add_plan(details, executor, executable)
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
        self.app.call_from_thread(self.say, Text(execution, style="red") if isinstance(execution, str) else execution_text(execution))

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
            text_to_copy, what = copied
            self.app.call_from_thread(self.say, *copy_text(text_to_copy, what, ask_terminal=lambda text: self.app.call_from_thread(self.app.copy_to_clipboard, text)))

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

    def job_event(self, event: JobEvent) -> None:
        """Log the event, update the draw-things-cli pane, and refresh the history where the store changed."""
        text = event_text(event, *run_arguments(self.dtc.live, event))
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
        self.status.show(self.dtc.live, self.history.other_process_running, message=self.history.other_process_message)

    def tick(self) -> None:
        """Update the elapsed time, the cooldown countdown, the Status widget, and the status line."""
        self.cli.tick(self.dtc.live)
        self.render_status()
        self.query_one("#status-line", Static).update(status_line_text(self.dtc.data_directory, self.dtc.live, self.dtc.job_running, self.dtc.quit_armed))
