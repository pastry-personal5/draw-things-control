"""The panes that keep state of their own: the Status widget, the draw-things-cli pane, the job definitions, the history, and the execution detail."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widgets import DataTable, RichLog, Static
from textual.widgets.data_table import ColumnKey
from textual.worker import get_current_worker

from draw_things_control.core.run_lock import run_lock_is_free
from draw_things_control.jobs.job_events import JobEvent, RunOutput
from draw_things_control.tui.commands import SORT_KEYS
from draw_things_control.tui.history import PAGE_SIZE, HistoryFilter, HistoryPage, HistoryReader, execution_label, reveal_run
from draw_things_control.tui.job_files import JobCatalog, JobListing, JobRow
from draw_things_control.tui.job_watch import JobWatcher
from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, LiveRun
from draw_things_control.tui.text import STATUS_STYLE, ExecutionDetail, execution_detail, execution_detail_text, history_cells, job_definition_cells, job_display_names, run_line_text, sort_job_rows, status_lines

# How often the history is checked while another process runs a job, and the data directory for changed job files when
# they cannot all be watched or an invalid job is listed.
HISTORY_POLL_SECONDS = 5
JOBS_POLL_SECONDS = 5
# How long the job list waits after a file event for more, since one save can report several.
JOBS_WATCH_PAUSE_SECONDS = 0.3
# The Job Definition widget's height with its border and header: 8 job rows show (owner decision).
JOB_DEFINITION_LINES = 11
# The history's height with its border and header: 5 rows show (owner decision); a horizontal scrollbar adds a line.
HISTORY_LINES = 8
# How long the detail waits after the history cursor stops, so holding an arrow key does not read every row it passes.
DETAIL_PAUSE_SECONDS = 0.2
# The detail's lines above its runs (model, refiner, size, and the header), and the lines each run takes.
DETAIL_HEAD_LINES = 4
DETAIL_RUN_LINES = 2


class StatusPane(Static):
    """The job at a glance: its phase, the job and run (or wait) bars with their end times, the details, and the last run."""

    def on_mount(self) -> None:
        self.border_title = "Status"

    def show(self, live: LiveRun | None, other_process: bool) -> None:
        width = self.content_size.width or 40
        text = Text("\n").join(status_lines(live, other_process, width))
        text.no_wrap = True
        text.overflow = "ellipsis"
        self.update(text)


class CliPane(Vertical):
    """The run line and the child's output, rendered from the app's LiveRun; it writes only the lines not yet written."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        # Lines of the LiveRun's output already written.
        self.written = 0

    def compose(self) -> ComposeResult:
        yield Static(id="run-line")
        yield RichLog(id="cli-output", max_lines=MAX_OUTPUT_LINES, wrap=True, min_width=20)

    def on_mount(self) -> None:
        self.border_title = "draw-things-cli"

    def new_job(self, live: LiveRun) -> None:
        """A job starts: its output replaces the last job's."""
        self.query_one(RichLog).clear()
        self.written = 0
        self.show(live)

    def show(self, live: LiveRun | None, event: JobEvent | None = None) -> None:
        """Show the LiveRun as it is now; a progress line updates only the run line."""
        if not isinstance(event, RunOutput) or event.progress is None and event.percent is None:
            self.write_output(live)
        self.tick(live)

    def tick(self, live: LiveRun | None) -> None:
        """Update the elapsed time and the cooldown countdown."""
        self.query_one("#run-line", Static).update(run_line_text(live))

    def write_output(self, live: LiveRun | None) -> None:
        if live is None:
            return
        pane = self.query_one(RichLog)
        new = min(live.output_count - self.written, len(live.output))
        # islice, not a copy of the whole deque, for every line the child prints.
        for line in itertools.islice(live.output, len(live.output) - new, None):
            # Text, never markup: the child's lines and the prompts in them contain brackets.
            pane.write(Text(line.text, style="red" if line.stream == "stderr" else ""))
        self.written = live.output_count


class JobDefinitionPane(DataTable[Text]):
    """The job files in the data directory, with their job IDs, sorted by any column; read again when a job file or a file
    a valid job reads changes. Every 5 seconds instead while that cannot be watched or an invalid job is listed, since an
    invalid job's inputs are not known.

    Enter describes the selected job and ``a`` asks to run it, through the screen's callbacks; ``s`` sorts by the next
    column and ``r`` reverses the order, and the sort is kept in the state store across sessions.
    """

    BINDINGS = [
        Binding("s", "next_sort", "Sort by the next column", show=False),
        Binding("r", "reverse_sort", "Reverse the order", show=False),
        Binding("a", "run_selected", "Run the job", show=False),
        Binding("escape", "leave", "Command line", show=False),
    ]

    def __init__(self, catalog: JobCatalog, *, describe: Callable[[Path], None], run: Callable[[Path], None], announce: Callable[[JobListing], None], leave: Callable[[], None], **options: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **options)
        self.catalog = catalog
        self.describe_job = describe
        self.run_job = run
        self.announce_listing = announce
        self.leave = leave
        # Not ``rows``: DataTable already uses that name.
        self.job_rows: list[JobRow] = []
        self.sort_key, self.descending = "changed", True
        self.note: str | None = None
        # The last listing's message, so a later check does not write it again.
        self.announced: str | None = None
        # Counts reads; only the newest one's result is shown.
        self.read_count = 0
        # Counts sort choices, so a saved one never overwrites a later one; and what is shown, so an unchanged check
        # does not redraw the table (which would reset a view scrolled with the mouse).
        self.sort_choices = 0
        self.shown_key: tuple[Any, ...] | None = None
        self.watcher = JobWatcher(catalog.directory, self.files_changed)
        self.check_timer: Timer | None = None
        self.watch_pause: Timer | None = None

    @property
    def job_names(self) -> list[str]:
        return [row.path.name for row in self.job_rows]

    @property
    def job_ids(self) -> list[str]:
        return [row.job_id for row in self.job_rows if row.job_id is not None]

    @property
    def selected(self) -> JobRow | None:
        if self.row_count == 0:
            return None
        name = str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value)
        return next((row for row in self.job_rows if row.path.name == name), None)

    def on_mount(self) -> None:
        self.border_title = "Job Definition"
        self.add_columns("ID", "Name", "Changed", "Mode", "Runs")
        # Until the first listing says what can be watched.
        self.check_timer = self.set_interval(JOBS_POLL_SECONDS, self.load)
        self.read_sort()

    def on_unmount(self) -> None:
        self.watcher.stop()

    def files_changed(self) -> None:
        """On the watcher's thread: read the directory once the events stop."""
        try:
            self.app.call_from_thread(self.read_after_pause)
        except RuntimeError:
            # The app is closing.
            pass

    def read_after_pause(self) -> None:
        if not self.is_attached:
            return
        if self.watch_pause is not None:
            self.watch_pause.stop()
        self.watch_pause = self.set_timer(JOBS_WATCH_PAUSE_SECONDS, self.load)

    def watch_show_horizontal_scrollbar(self, shown: bool) -> None:
        # As in the history: a narrow column scrolls sideways, and its scrollbar takes a line of its own, so 8 rows still show.
        self.styles.height = JOB_DEFINITION_LINES + (1 if shown else 0)

    def load(self, *, fresh: bool = False, announce: bool = False) -> None:
        """Read the directory again; ``fresh`` reads every file, and ``announce`` also writes the list to Messages."""
        self.read_count += 1
        self.read_jobs(self.read_count, fresh, announce)

    @work(thread=True, exclusive=True, group="job-definitions-sort")
    def read_sort(self) -> None:
        key, descending = self.catalog.sort()
        self.app.call_from_thread(self.apply_sort, key, descending)

    def apply_sort(self, key: str, descending: bool) -> None:
        # The saved sort arrives after the first frame; a choice made before it wins.
        if not self.sort_choices:
            self.sort_key, self.descending = key, descending
        self.load()

    @work(thread=True, group="job-definitions")
    def read_jobs(self, request: int, fresh: bool, announce: bool) -> None:
        listing = self.catalog.read(fresh=fresh)
        # Only the newest read says what to watch; an older one finishing last would drop a newer job's inputs.
        watched = self.watcher.follow(listing.reads) if request == self.read_count else False
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_listing, request, listing, announce, watched)

    def show_listing(self, request: int, listing: JobListing, announce: bool, watched: bool = False) -> None:
        if not self.is_attached:
            return
        if request == self.read_count and self.check_timer is not None:
            if watched and not listing.any_invalid:
                self.check_timer.pause()
            else:
                self.check_timer.resume()
        # /get jobs writes the list; otherwise a message (no files, no directory) is written once, when it first appears.
        if announce or (listing.message is not None and listing.message != self.announced):
            self.announce_listing(listing)
        self.announced = listing.message
        if request != self.read_count:
            return
        self.job_rows = listing.rows
        self.note = listing.message or listing.id_error
        self.render_rows()

    def render_rows(self) -> None:
        """Show the rows in the current sort, keeping the selection on the same job; nothing to do when nothing changed."""
        shown = (self.sort_key, self.descending, self.note, tuple((row.path, row.number, row.changed, row.job, row.error) for row in self.job_rows))
        if shown == self.shown_key:
            return
        self.shown_key = shown
        selected = self.selected
        self.clear()
        names = job_display_names([row.path for row in self.job_rows])
        for row in sort_job_rows(self.job_rows, self.sort_key, self.descending):
            self.add_row(*job_definition_cells(row, names[row.path]), key=row.path.name)
        direction = "newest first" if self.sort_key == "changed" and self.descending else ("descending" if self.descending else "ascending")
        # Text, not str: a str title is parsed as markup.
        self.border_subtitle = Text(self.note or f"by {self.sort_key}, {direction}")
        if selected is not None and any(row.path == selected.path for row in self.job_rows):
            self.move_cursor(row=self.get_row_index(selected.path.name))

    def set_sort(self, key: str, descending: bool) -> None:
        """Sort by ``key``, and keep the choice across sessions."""
        self.sort_key, self.descending = key, descending
        self.sort_choices += 1
        self.render_rows()
        self.keep_sort(key, descending, self.sort_choices)

    @work(thread=True, group="job-definitions-keep")
    def keep_sort(self, key: str, descending: bool, choice: int) -> None:
        error = self.catalog.keep_sort(key, descending, choice)
        if error is not None:
            self.app.call_from_thread(self.set_message, error)

    def set_message(self, message: str) -> None:
        self.note = message
        self.shown_key = None
        self.border_subtitle = Text(message)

    def action_next_sort(self) -> None:
        key = SORT_KEYS[(SORT_KEYS.index(self.sort_key) + 1) % len(SORT_KEYS)] if self.sort_key in SORT_KEYS else SORT_KEYS[0]
        self.set_sort(key, natural_descending(key))

    def action_reverse_sort(self) -> None:
        self.set_sort(self.sort_key, not self.descending)

    def action_run_selected(self) -> None:
        row = self.selected
        if row is not None:
            self.run_job(row.path)

    def action_leave(self) -> None:
        self.leave()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter describes the job; the screen's handler is for the history.
        event.stop()
        row = self.selected
        if row is not None:
            self.describe_job(row.path)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The history's detail follows its own cursor, not this one.
        event.stop()


def natural_descending(key: str) -> bool:
    """The direction a sort key takes when none is given: newest first for ``changed``, ascending for the others."""
    return key == "changed"


class HistoryPane(DataTable[Text]):
    """The execution history, newest first: paged, filtered, and kept current from the state store.

    ``busy`` says whether this TUI is running a job: it then refreshes the pane from that job's events, and holds the
    run lock itself, so polling waits. Only the newest read is shown, so a stale page never lands under a new filter.
    """

    BINDINGS = [Binding("escape", "leave", "Command line", show=False)]

    class RowsUpdated(Message):
        """Rows already shown were read again: a run of theirs may have finished."""

        def __init__(self, execution_ids: list[int]) -> None:
            super().__init__()
            self.execution_ids = execution_ids

    class LockChanged(Message):
        """Whether another process holds the run lock changed."""

    class PageShown(Message):
        """A read from the top was shown: the execution under the cursor, or None and why there is none."""

        def __init__(self, execution_id: int | None, message: str | None) -> None:
            super().__init__()
            self.execution_id = execution_id
            self.message = message

    def __init__(self, reader: HistoryReader, *, busy: Callable[[], bool], leave: Callable[[], None], **options: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **options)
        self.reader = reader
        self.busy = busy
        self.leave = leave
        # An execution to move the cursor to once a read shows it: this TUI's new job.
        self.select_when_shown: int | None = None
        # Not ``filter``, ``rows``, or ``loading``: DataTable and Widget already use those names.
        self.history_filter = HistoryFilter()
        # The executions shown, by ID.
        self.executions: dict[int, dict[str, Any]] = {}
        # Whether no older execution is left to read.
        self.all_read = True
        self.reading = False
        # Counts reads; only the newest one's result is shown, and only it ends the reading.
        self.read_count = 0
        self.column_keys: list[ColumnKey] = []
        # Whether the last poll found another process holding the run lock; one more check follows its release.
        self.other_process_running = False

    def on_mount(self) -> None:
        self.border_title = "Execution History"
        self.column_keys = self.add_columns("ID", "Job", "Status", "Started", "Runs")
        self.set_interval(HISTORY_POLL_SECONDS, self.poll)
        # At once, so a job another process was already running when the TUI opened is shown without waiting for a poll.
        self.check_lock()
        self.load()

    def action_leave(self) -> None:
        # Not focus_next: the Execution widget follows the history in the Tab order.
        self.leave()

    def watch_show_horizontal_scrollbar(self, shown: bool) -> None:
        # A narrow column scrolls sideways; its scrollbar takes a line of its own, so 5 rows still show.
        self.styles.height = HISTORY_LINES + (1 if shown else 0)

    def check_lock(self) -> bool:
        """Whether another process holds the run lock now; posts LockChanged when that changes."""
        held = not self.busy() and not run_lock_is_free()
        if held != self.other_process_running:
            self.other_process_running = held
            self.post_message(self.LockChanged())
        return held

    @property
    def selected(self) -> int | None:
        if self.row_count == 0:
            return None
        return int(str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value))

    def set_filter(self, history_filter: HistoryFilter) -> None:
        self.history_filter = history_filter
        # The old filter's rows go at once, so a page read for the new filter is never appended to them.
        self.clear()
        self.executions = {}
        self.load()

    def load(self, offset: int = 0) -> None:
        """Read from ``offset`` on; a re-read from the top reads as many rows as are loaded, so the pane keeps its place."""
        self.read_count += 1
        self.reading = True
        limit = PAGE_SIZE if offset else max(PAGE_SIZE, len(self.executions))
        self.read_page(self.read_count, self.history_filter, offset, limit)

    @work(thread=True, exclusive=True, group="history")
    def read_page(self, request: int, history_filter: HistoryFilter, offset: int, limit: int) -> None:
        page = self.reader.page(history_filter, offset, limit)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_page, request, history_filter, page)

    def show_page(self, request: int, history_filter: HistoryFilter, page: HistoryPage) -> None:
        if not self.is_attached or request != self.read_count:
            return
        self.reading = False
        if page.offset == 0:
            selected = self.selected
            self.clear()
            self.executions = {}
        for row in page.rows:
            if row["id"] not in self.executions:
                self.executions[row["id"]] = row
                self.add_row(*history_cells(row), key=str(row["id"]))
        self.all_read = page.complete
        # Text, not str: a str title is parsed as markup, and the filter and error texts are the user's or the store's.
        self.border_title = Text(f"Execution History: {history_filter.text()}" if history_filter.text() else "Execution History")
        self.border_subtitle = Text(page.message or "")
        if page.offset == 0 and selected is not None and selected in self.executions:
            self.move_cursor(row=self.get_row_index(str(selected)))
        wanted, self.select_when_shown = self.select_when_shown, None
        # A filter that hides the new execution leaves the cursor where it is.
        if page.offset == 0 and wanted is not None and wanted in self.executions:
            self.move_cursor(row=self.get_row_index(str(wanted)))
        if page.offset == 0:
            self.post_message(self.PageShown(self.selected, page.message))

    def refresh_rows(self, execution_ids: list[int]) -> None:
        """Update rows already shown, in place, without reading the whole pane again."""
        self.read_rows(self.read_count, execution_ids)

    @work(thread=True, group="history-rows")
    def read_rows(self, request: int, execution_ids: list[int]) -> None:
        rows = self.reader.rows(execution_ids)
        if not isinstance(rows, str):
            self.app.call_from_thread(self.update_rows, request, rows)

    def update_rows(self, request: int, rows: list[dict[str, Any]]) -> None:
        if not self.is_attached or request != self.read_count:
            return
        for row in rows:
            if row["id"] not in self.executions:
                continue
            self.executions[row["id"]] = row
            for column, cell in zip(self.column_keys, history_cells(row), strict=True):
                self.update_cell(str(row["id"]), column, cell, update_width=True)
        self.post_message(self.RowsUpdated([row["id"] for row in rows]))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The next page loads when the cursor reaches the last row.
        if event.cursor_row == self.row_count - 1 and not self.all_read and not self.reading:
            self.load(self.row_count)

    def poll(self) -> None:
        """While another process runs a job, the store is the only way to see it: whether the lock is held says when."""
        if self.busy() or self.reading:
            return
        was_held = self.other_process_running
        held = self.check_lock()
        if held or was_held:
            self.check(self.read_count, self.history_filter, [execution_id for execution_id, row in self.executions.items() if row["status"] == "running"])

    @work(thread=True, exclusive=True, group="history-poll")
    def check(self, request: int, history_filter: HistoryFilter, running: list[int]) -> None:
        """A newer execution means reading the pane again; otherwise only the running rows are read."""
        newest = self.reader.newest_id(history_filter)
        if not isinstance(newest, str):
            self.app.call_from_thread(self.after_check, request, newest, running)

    def after_check(self, request: int, newest: int | None, running: list[int]) -> None:
        if not self.is_attached or request != self.read_count:
            return
        if newest is not None and newest not in self.executions:
            self.load()
        elif running:
            self.refresh_rows(running)


class ExecutionBody(Static):
    """The detail's text; its file-name links run ``reveal`` here, which hands the numbers to the pane."""

    def action_reveal(self, execution_id: int, run_number: int) -> None:
        pane = self.parent
        if isinstance(pane, ExecutionPane):
            pane.select(run_number)
            pane.reveal(execution_id, run_number)


class ExecutionPane(VerticalScroll):
    """The execution under the history cursor: its settings and its successful runs, each with a link that reveals its output.

    Up and Down select a run, and Enter or a click on its file name reveals it in Finder. Reads go through the history's
    reader on a worker thread, and only the newest read is shown, as in the history.
    """

    BINDINGS = [
        Binding("up", "move(-1)", "Earlier run", show=False),
        Binding("down", "move(1)", "Later run", show=False),
        Binding("pageup", "page(-1)", "Page up", show=False),
        Binding("pagedown", "page(1)", "Page down", show=False),
        Binding("home", "jump(0)", "First run", show=False),
        Binding("end", "jump(-1)", "Last run", show=False),
        Binding("enter", "reveal_selected", "Reveal", show=False),
        Binding("escape", "leave", "Command line", show=False),
    ]

    def __init__(self, reader: HistoryReader, *, say: Callable[[Text | str, str], None], leave: Callable[[], None], **options: Any) -> None:
        super().__init__(**options)
        self.reader = reader
        self.say = say
        self.leave = leave
        self.execution: dict[str, Any] | None = None
        # What the widget shows of the execution, read with it.
        self.detail: ExecutionDetail | None = None
        # The run numbers listed, in order, and the one selected.
        self.shown: list[int] = []
        self.selected: int | None = None
        self.read_count = 0
        self._pause: Timer | None = None
        # What shows when there is no execution: the history's message, or an error.
        self.message = Text("")

    @property
    def execution_id(self) -> int | None:
        return self.execution["id"] if self.execution is not None else None

    def compose(self) -> ComposeResult:
        yield ExecutionBody(id="execution-body")

    def on_mount(self) -> None:
        self.border_title = "Execution"

    def follow(self, execution_id: int | None, *, pause: bool = True) -> None:
        """Show ``execution_id``, after DETAIL_PAUSE_SECONDS unless ``pause`` is false; None shows the message instead."""
        if self._pause is not None:
            self._pause.stop()
            self._pause = None
        if execution_id is None:
            self.read_count += 1
            self.execution, self.shown, self.selected = None, [], None
            self.render_detail()
            return
        if pause:
            self._pause = self.set_timer(DETAIL_PAUSE_SECONDS, lambda: self.load(execution_id))
        else:
            self.load(execution_id)

    def show_message(self, message: str) -> None:
        self.message = Text(message, style="dim")
        if self.execution is None:
            self.render_detail()

    def load(self, execution_id: int) -> None:
        self._pause = None
        self.read_count += 1
        self.read_execution(self.read_count, execution_id)

    @work(thread=True, exclusive=True, group="execution-detail")
    def read_execution(self, request: int, execution_id: int) -> None:
        execution = self.reader.execution(execution_id)
        # Read here, off the UI thread: it looks for every output file, which a redraw must not.
        detail = execution_detail(execution) if isinstance(execution, dict) else None
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_execution, request, execution_id, execution, detail)

    def show_execution(self, request: int, execution_id: int, execution: dict[str, Any] | str, detail: ExecutionDetail | None = None) -> None:
        if not self.is_attached or request != self.read_count:
            return
        if isinstance(execution, str):
            self.execution, self.shown, self.selected = None, [], None
            self.message = Text(execution, style="red")
            self.render_detail()
            return
        # The same execution read again keeps its selected run; another one starts at its first run.
        keep = self.selected if self.execution_id == execution_id else None
        self.execution, self.detail = execution, detail if detail is not None else execution_detail(execution)
        self.shown = [run.number for run in self.detail.runs]
        self.selected = keep if keep in self.shown else (self.shown[0] if self.shown else None)
        self.render_detail()

    def render_detail(self) -> None:
        body = self.query_one(ExecutionBody)
        if self.execution is None or self.detail is None:
            self.border_title = "Execution"
            body.update(self.message)
            return
        # Text, not str: a str title is parsed as markup, and the job name is the user's.
        title = Text(f"Execution {execution_label(self.execution)}: {self.execution['job_name']} ")
        title.append(self.execution["status"], style=STATUS_STYLE.get(self.execution["status"], ""))
        self.border_title = title
        text, self.shown = execution_detail_text(self.detail, self.scrollable_content_region.width, self.selected)
        body.update(text)

    def on_resize(self, event: events.Resize) -> None:
        self.render_detail()

    def select(self, run_number: int) -> None:
        if run_number in self.shown:
            self.selected = run_number
            self.render_detail()
            self.keep_in_view()

    def keep_in_view(self) -> None:
        if self.selected is None:
            return
        top = DETAIL_HEAD_LINES + self.shown.index(self.selected) * DETAIL_RUN_LINES
        height = self.scrollable_content_region.height
        if top < self.scroll_y:
            self.scroll_to(y=top, animate=False)
        elif top + DETAIL_RUN_LINES > self.scroll_y + height:
            self.scroll_to(y=top + DETAIL_RUN_LINES - height, animate=False)

    def action_move(self, step: int) -> None:
        if not self.shown:
            return
        index = self.shown.index(self.selected) if self.selected in self.shown else 0
        self.select(self.shown[min(max(index + step, 0), len(self.shown) - 1)])

    def action_page(self, step: int) -> None:
        self.action_move(step * max(1, self.scrollable_content_region.height // DETAIL_RUN_LINES))

    def action_jump(self, index: int) -> None:
        if self.shown:
            self.select(self.shown[index])

    def action_leave(self) -> None:
        self.leave()

    def action_reveal_selected(self) -> None:
        if self.execution_id is not None and self.selected is not None:
            self.reveal(self.execution_id, self.selected)

    def on_click(self, event: events.Click) -> None:
        # A click on a run's lines selects it; one on its file name has already revealed it, and stopped the click.
        offset = event.get_content_offset(self.query_one(ExecutionBody))
        if offset is None or offset.y < DETAIL_HEAD_LINES:
            return
        index = (offset.y - DETAIL_HEAD_LINES) // DETAIL_RUN_LINES
        if index < len(self.shown):
            self.select(self.shown[index])

    @work(thread=True, group="execution-reveal")
    def reveal(self, execution_id: int, run_number: int) -> None:
        """Reveal the run's output in Finder, as /reveal does; the file is looked up again, so one removed since is reported."""
        self.app.call_from_thread(self.say, *reveal_run(self.reader.execution(execution_id), run_number))
