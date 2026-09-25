"""The two panes that keep state of their own: the draw-things-cli pane and the history pane."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, RichLog, Static
from textual.widgets.data_table import ColumnKey
from textual.worker import get_current_worker

from draw_things_control.core.run_lock import run_lock_is_free
from draw_things_control.jobs.job_events import JobEvent, RunOutput
from draw_things_control.tui.history import PAGE_SIZE, HistoryFilter, HistoryPage, HistoryReader
from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, LiveRun
from draw_things_control.tui.text import history_cells, run_line_text

# How often the history is checked while another process runs a job.
HISTORY_POLL_SECONDS = 5


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


class HistoryPane(DataTable[Text]):
    """The execution history, newest first: paged, filtered, and kept current from the state store.

    ``busy`` says whether this TUI is running a job: it then refreshes the pane from that job's events, and holds the
    run lock itself, so polling waits. Only the newest read is shown, so a stale page never lands under a new filter.
    """

    BINDINGS = [Binding("escape", "leave", "Command line", show=False)]

    def __init__(self, reader: HistoryReader, *, busy: Callable[[], bool], **options: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **options)
        self.reader = reader
        self.busy = busy
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
        self.border_title = "History"
        self.column_keys = self.add_columns("ID", "Job", "Status", "Started", "Runs")
        self.set_interval(HISTORY_POLL_SECONDS, self.poll)
        self.load()

    def action_leave(self) -> None:
        self.screen.focus_next()

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
        self.border_title = Text(f"History: {history_filter.text()}" if history_filter.text() else "History")
        self.border_subtitle = Text(page.message or "")
        if page.offset == 0 and selected is not None and selected in self.executions:
            self.move_cursor(row=self.get_row_index(str(selected)))

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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The next page loads when the cursor reaches the last row.
        if event.cursor_row == self.row_count - 1 and not self.all_read and not self.reading:
            self.load(self.row_count)

    def poll(self) -> None:
        """While another process runs a job, the store is the only way to see it: whether the lock is held says when."""
        if self.busy() or self.reading:
            return
        held = not run_lock_is_free()
        if held or self.other_process_running:
            self.check(self.read_count, self.history_filter, [execution_id for execution_id, row in self.executions.items() if row["status"] == "running"])
        self.other_process_running = held

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
