"""The job list, the job detail view, and the help screen."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Static
from textual.worker import get_current_worker

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_report import PLACEHOLDER_SEED, job_files, plan_lines, read_job
from draw_things_control.jobs.job_service import JobService
from draw_things_control.tui.widgets import JobTable, keys_text, pairs_text, plan_text, summary_text

if TYPE_CHECKING:
    from draw_things_control.tui.app import DrawThingsApp


@dataclass(frozen=True)
class JobRow:
    """One job file in the list: the job, or why it is invalid."""

    path: Path
    job: JobDefinition | None
    error: str | None = None

    def cells(self) -> tuple[Text, ...]:
        # Text, not str: a table parses a str cell as markup, and a YAML error quotes the file's line, brackets and all.
        if self.job is None:
            return tuple(Text(cell) for cell in (self.path.name, "-", "-", "-", f"invalid: {self.error}"))
        return tuple(Text(cell) for cell in (self.path.name, self.job.name, str(self.job.mode), str(self.job.run_count), "valid"))


@dataclass(frozen=True)
class JobDetails:
    """What the detail view shows: the job or its error, and the plan (None while it is computed) or why there is none."""

    job: JobDefinition | None
    error: str | None = None
    plan: tuple[str, ...] | None = None
    plan_error: str | None = None


def error_text(path: Path, error: Exception) -> str:
    """The error without the job file's path in front, since the view already names the file."""
    message = str(error)
    prefix = f"{path.expanduser().resolve()}: "
    return message.removeprefix(prefix)


def read_rows(directory: Path, settings: GlobalConfig) -> tuple[list[JobRow], str | None]:
    """Validate every job file without decoding inputs; return the rows, and a message when there are none."""
    if not directory.is_dir():
        return [], f"Data directory not found: {directory}"
    try:
        paths = job_files(directory)
    except OSError as error:
        return [], f"Cannot read the data directory {directory}: {error.strerror}"
    rows: list[JobRow] = []
    for path in paths:
        try:
            rows.append(JobRow(path, read_job(path, settings, decode_input=False)[0]))
        except (ValueError, OSError) as error:
            rows.append(JobRow(path, None, error_text(path, error)))
    return rows, None if rows else f"No job files (*.yaml, *.yml) in {directory}"


def read_details(path: Path, settings: GlobalConfig) -> JobDetails:
    """Load the job, decoding its input, without its plan."""
    try:
        return JobDetails(read_job(path, settings)[0])
    except (ValueError, OSError) as error:
        return JobDetails(None, error_text(path, error))


def add_plan(details: JobDetails, service: JobService, executable: str) -> JobDetails:
    """The details with the job's dry-run plan, using the placeholder seed when the job sets none."""
    assert details.job is not None
    try:
        preview = service.preview(details.job, executable=executable, seed=PLACEHOLDER_SEED)
    except (ValueError, OSError) as error:
        return JobDetails(details.job, plan_error=str(error))
    return JobDetails(details.job, plan=tuple(plan_lines(details.job, preview)))


class JobListScreen(Screen[None]):
    """Every job file in the data directory, with its validation status."""

    BINDINGS = [Binding("r", "refresh", "Refresh")]

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, JobRow] = {}

    @property
    def dtc(self) -> DrawThingsApp:
        return cast("DrawThingsApp", self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="list-message")
        yield JobTable(id="jobs", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.dtc.data_directory)
        self.query_one(JobTable).add_columns("File", "Name", "Mode", "Runs", "Status")
        self.load_jobs()

    def action_refresh(self) -> None:
        self.dtc.details.clear()
        self.load_jobs()

    def load_jobs(self) -> None:
        self.query_one(JobTable).loading = True
        self.read_jobs(self.dtc.data_directory, self.dtc.settings)

    @work(thread=True, exclusive=True)
    def read_jobs(self, directory: Path, settings: GlobalConfig) -> None:
        rows, message = read_rows(directory, settings)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_rows, rows, message)

    def show_rows(self, rows: list[JobRow], message: str | None) -> None:
        if not self.is_attached:
            return
        table = self.query_one(JobTable)
        selected = self.selected_name()
        table.clear()
        self.rows = {row.path.name: row for row in rows}
        for row in rows:
            table.add_row(*row.cells(), key=row.path.name)
        notice = self.query_one("#list-message", Static)
        notice.update(Text(message or ""))
        notice.display = message is not None
        table.loading = False
        if selected in self.rows:
            table.move_cursor(row=table.get_row_index(selected))
        table.focus()

    def selected_name(self) -> str | None:
        table = self.query_one(JobTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = self.rows.get(str(event.row_key.value))
        if row is not None:
            self.app.push_screen(JobDetailScreen(row.path))


class JobDetailScreen(Screen[None]):
    """One job: its summary, prompt pairs, and dry-run plan; read-only."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    @property
    def dtc(self) -> DrawThingsApp:
        return cast("DrawThingsApp", self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="detail"):
            yield Static(id="summary")
            yield Static(id="pairs")
            yield Static(id="plan")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.path.name
        cached = self.dtc.details.get(self.path)
        if cached is not None:
            self.show(cached)
        else:
            self.query_one("#summary", Static).update("Loading...")
            self.load_details(self.dtc.settings, self.dtc.job_service, self.dtc.executable)

    def action_back(self) -> None:
        self.app.pop_screen()

    @work(thread=True, exclusive=True)
    def load_details(self, settings: GlobalConfig, service: JobService, executable: str) -> None:
        worker = get_current_worker()
        details = read_details(self.path, settings)
        if worker.is_cancelled:
            return
        self.app.call_from_thread(self.show, details)
        if details.job is None:
            self.app.call_from_thread(self.keep, details)
            return
        details = add_plan(details, service, executable)
        if not worker.is_cancelled:
            self.app.call_from_thread(self.keep, details)
            self.app.call_from_thread(self.show, details)

    def keep(self, details: JobDetails) -> None:
        self.dtc.details[self.path] = details

    def show(self, details: JobDetails) -> None:
        # The worker checks for cancellation before it queues this, but the screen can still be closed before it runs.
        if not self.is_attached:
            return
        summary = self.query_one("#summary", Static)
        pairs = self.query_one("#pairs", Static)
        plan = self.query_one("#plan", Static)
        if details.job is None:
            summary.update(Text(f"Invalid job: {self.path}\n\n{details.error}"))
            summary.add_class("error")
            pairs.display = plan.display = False
            return
        summary.update(summary_text(details.job))
        pairs.update(pairs_text(details.job))
        plan.update(plan_text(details.job, details.plan, details.plan_error))


class HelpScreen(ModalScreen[None]):
    """The keys, over the current screen."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close", show=False)]

    def compose(self) -> ComposeResult:
        yield Static(keys_text(), id="help")

    def action_close(self) -> None:
        self.app.pop_screen()
