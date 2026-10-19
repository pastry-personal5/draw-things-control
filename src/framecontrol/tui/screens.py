"""The main screen, which lays out the panes and runs the commands. The one dialog, asking before a deletion, is
``tui/delete_dialog.py``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, RichLog, Rule, Static

from framecontrol.core.global_config import GlobalConfig
from framecontrol.core.network import is_loopback_host
from framecontrol.core.run_lock import CHILD_EXECUTABLE_NAME
from framecontrol.services.history import HistoryReader
from framecontrol.services.job_catalog import JobCatalog, JobListing
from framecontrol.services.job_details import JobDetails, add_plan, read_details
from framecontrol.services.queue_reader import QueueReader
from framecontrol.services.toolkit import Toolkit
from framecontrol.tui.commands import CommandSuggester
from framecontrol.tui.controller import CommandController
from framecontrol.tui.desktop import copy_text, reveal_run
from framecontrol.tui.job_sort import SortPreference
from framecontrol.tui.panes.cli_output import GenerationOutput
from framecontrol.tui.panes.execution import ExecutionDetailPane
from framecontrol.tui.panes.history import ExecutionHistoryList
from framecontrol.tui.panes.job_definitions import JobsList
from framecontrol.tui.panes.queue import QueueList
from framecontrol.tui.panes.status import WorkStatus
from framecontrol.tui.reader import PaneHistory
from framecontrol.tui.running_job import RunningJobView
from framecontrol.tui.text.arguments import parameters_text
from framecontrol.tui.text.execution import execution_text
from framecontrol.tui.text.jobs import details_text, jobs_text
from framecontrol.tui.text.prompts import prompts_text
from framecontrol.tui.text.status import status_line_text
from framecontrol.tui.widgets import MAX_MESSAGE_LINES, ActivityLog, CommandField, SteadyText

if TYPE_CHECKING:
    from framecontrol.tui.app import FramecontrolApp

# The generation output's height with its border, and the least it and the activity log may shrink to on a short terminal.
GENERATION_OUTPUT_LINES = 15
GENERATION_OUTPUT_MIN_LINES = 7
ACTIVITY_LOG_MIN_LINES = 6
# The rows under the panes: a rule, the command field, a rule, and the status line.
BOTTOM_LINES = 4
# The work status above the generation output, with its border (styles.tcss sets the same height).
WORK_STATUS_LINES = 7


class MainScreen(Screen[None]):
    """Lays out the workspace, runs commands, and passes job events to its displays."""

    def __init__(self) -> None:
        super().__init__()
        # The data directory's job files and their IDs; the Jobs list reads through it.
        self.catalog: JobCatalog | None = None
        self.reader: PaneHistory | None = None
        self.commands = CommandController(self)
        self.running = RunningJobView(self)
        # The completions Tab is stepping through in the command field, and the one in it.
        self.completions: tuple[list[str], int] = ([], 0)

    @property
    def fc(self) -> FramecontrolApp:
        return cast("FramecontrolApp", self.app)

    @property
    def command_field(self) -> CommandField:
        return self.query_one(CommandField)

    @property
    def jobs_list(self) -> JobsList:
        return self.query_one(JobsList)

    @property
    def execution_history(self) -> ExecutionHistoryList:
        return self.query_one(ExecutionHistoryList)

    @property
    def generation_output(self) -> GenerationOutput:
        return self.query_one(GenerationOutput)

    @property
    def execution_detail(self) -> ExecutionDetailPane:
        return self.query_one(ExecutionDetailPane)

    @property
    def work_status(self) -> WorkStatus:
        return self.query_one(WorkStatus)

    @property
    def queue_list(self) -> QueueList:
        return self.query_one(QueueList)

    def compose(self) -> ComposeResult:
        # The app's own store, shared with the Queue list's fallback and the gRPC feed's past_run reads; closed
        # by the app, not this screen, since it outlives any one screen.
        store = self.fc.store_provider
        self.reader = PaneHistory(HistoryReader(self.fc.paths, store))
        queue_reader = QueueReader(self.fc.paths, store)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield WorkStatus(id="work-status")
                yield GenerationOutput(id="generation-output")
                yield ActivityLog(id="activity-log", max_lines=MAX_MESSAGE_LINES, wrap=True, min_width=20)
            with Vertical(id="right"):
                # First in the management column, so Tab follows the visible hierarchy after the command field.
                self.catalog = JobCatalog(self.fc.data_directory, self.fc.settings, self.fc.paths, store)
                yield JobsList(self.catalog, SortPreference(store), log=self.describe_job, run=self.fc.start_flow, announce=self.announce_jobs, leave=self.focus_command_field, id="jobs-list")
                yield QueueList(queue_reader, feed_connected=lambda: self.fc.feed_connected, leave=self.focus_command_field, id="queue-list")
                yield ExecutionHistoryList(self.reader, busy=lambda: self.fc.feed_connected, leave=self.focus_command_field, delete=self.fc.ask_to_delete, id="execution-history-list")
                yield ExecutionDetailPane(self.reader, say=self.say, leave=self.focus_command_field, file_opener=self.fc.settings.file_opener, id="execution-detail")
        yield Rule(line_style="solid", classes="command-rule")
        with Horizontal(id="command-line"):
            yield Static("> ", id="command-prompt")
            yield CommandField(id="command-field", history=self.fc.command_history, compact=True, suggester=CommandSuggester(lambda: self.jobs_list.job_names, lambda: self.jobs_list.job_ids, self.execution_ids, self.queue_ids))
        yield Rule(line_style="solid", classes="command-rule")
        yield SteadyText(id="status-line")

    def on_mount(self) -> None:
        self.query_one(ActivityLog).border_title = "Activity log"
        # Tab moves from the command field through the selectable lists and detail; logs scroll with the mouse.
        for log in self.query(RichLog):
            log.can_focus = False
        self.command_field.focus()
        self.say(Text("Type /help for the commands.", style="dim"))
        # A feed that connected before this screen was mounted could not tell the person yet.
        if self.fc.feed_connected:
            self.call_after_refresh(self.fc.on_feed_connection_changed)
        self.set_interval(1, self.running.tick)
        if self.fc.live is not None:
            # The feed followed a job before this screen was mounted (a fast local server): show it as an attach, since
            # whatever it printed until now was never shown.
            self.running.job_started(seeded=True)
        else:
            self.running.render_live()

    def on_resize(self, event: events.Resize) -> None:
        # Generation output gives up lines first, so the activity log keeps its minimum on a short terminal.
        left = event.size.height - BOTTOM_LINES - WORK_STATUS_LINES
        self.generation_output.styles.height = max(GENERATION_OUTPUT_MIN_LINES, min(GENERATION_OUTPUT_LINES, left - ACTIVITY_LOG_MIN_LINES))

    def say(self, text: Text | str, style: str = "", *, block: bool = False) -> None:
        """Write to the activity log; ``block`` starts a distinct command or result block."""
        self.query_one(ActivityLog).say(text, style, block=block)

    def execution_ids(self) -> list[str]:
        """The loaded executions' IDs, newest first, for completion."""
        return [row.label for row in self.execution_history.executions.values()]

    def queue_ids(self) -> list[str]:
        """The queue entries currently shown, for completion."""
        return list(self.queue_list.rows_by_id)

    def focus_command_field(self) -> None:
        self.command_field.focus()

    def shortcut_help_available(self) -> bool:
        """Whether the focused main-screen component can show shortcut help."""
        return self.focused in (self.jobs_list, self.queue_list, self.execution_history, self.execution_detail)

    def refresh_status_line(self) -> None:
        """Render the focus-sensitive footer without waiting for the periodic job tick."""
        if not self.is_mounted:
            return
        fc = self.fc
        self.query_one("#status-line", SteadyText).show_text(status_line_text(fc.data_directory, fc.live, fc.job_running, fc.quit_armed, shortcut_help=self.shortcut_help_available(), server=fc.feed_connected, local_validation=is_loopback_host(urlsplit(fc.server_url).hostname or ""), completions=self.completions))

    def show_completions(self, choices: list[str], index: int) -> None:
        self.completions = (choices, index)
        self.refresh_status_line()

    # Commands

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.clear()
        line = event.value.strip()
        if not line:
            return
        self.command_field.remember(line)
        self.fc.keep_command_history(list(self.command_field.recalled))
        self.say(Text(f"> {line}", style="bold"), block=True)
        self.commands.submit(line)

    def describe_job(self, path: Path) -> None:
        """/describe job, Enter or a click on the Jobs list, and ``l``: the details in the Activity log."""
        self.load_details(path, self.fc.settings, self.fc.toolkit)

    # Workers: jobs, the detail, and reveal

    def announce_jobs(self, listing: JobListing) -> None:
        """/get jobs: write the just-read Jobs list to the activity log."""
        running = self.fc.live.path.name if self.fc.job_running and self.fc.live is not None and self.fc.live.path is not None else None
        self.say(jobs_text(listing.rows, running, listing.message))
        if listing.id_error is not None:
            self.say(listing.id_error, "yellow")

    @work(thread=True, group="details")
    def load_details(self, path: Path, settings: GlobalConfig, toolkit: Toolkit) -> None:
        details = read_details(path, settings, self.fc.paths)
        if details.job is not None:
            details = add_plan(details, toolkit, CHILD_EXECUTABLE_NAME)
        self.app.call_from_thread(self.show_details, path, details)

    def show_details(self, path: Path, details: JobDetails) -> None:
        if not self.is_attached:
            return
        if details.job is None:
            text = Text(f"Invalid job: {path}\n{details.error}", style="red")
        else:
            row = next((row for row in self.jobs_list.job_rows if row.path == path), None)
            text = details_text(details.job, details, row.job_id if row is not None else None)
        self.say(text)

    # The history and the detail

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The detail follows the history's cursor, after a pause.
        if event.data_table is self.execution_history and event.row_key.value is not None:
            self.execution_detail.follow(int(str(event.row_key.value)))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on the history moves to the detail, at its first run; /describe execution still writes the full detail to activity log.
        if event.data_table is not self.execution_history:
            return
        execution_id = int(str(event.row_key.value))
        detail = self.execution_detail
        detail.focus()
        if detail.execution_id == execution_id:
            detail.action_jump(0)
        else:
            detail.follow(execution_id, pause=False)

    def on_execution_history_list_page_shown(self, event: ExecutionHistoryList.PageShown) -> None:
        # A re-read may keep the cursor where it was, which highlights nothing; read the detail again all the same.
        if event.execution_id is None:
            self.execution_detail.follow(None)
            self.execution_detail.show_message(event.message or "No execution selected")
        else:
            self.execution_detail.follow(event.execution_id)

    def on_execution_history_list_rows_updated(self, event: ExecutionHistoryList.RowsUpdated) -> None:
        if self.execution_detail.execution_id in event.execution_ids:
            self.execution_detail.follow(self.execution_detail.execution_id, pause=False)

    def on_execution_history_list_lock_changed(self, event: ExecutionHistoryList.LockChanged) -> None:
        self.running.render_status()

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
