"""The Execution History widget."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable
from textual.widgets.data_table import ColumnKey
from textual.worker import get_current_worker

from draw_things_control.jobs.events import JobStatus
from draw_things_control.services.history import PAGE_SIZE, HistoryFilter, HistoryPage
from draw_things_control.state.executions import ExecutionRow
from draw_things_control.tui.panes.base import SidewaysTable
from draw_things_control.tui.reader import PaneHistory
from draw_things_control.tui.text.history import history_cells

# How often the history is checked while another process runs a job, and the data directory for changed job files when
# they cannot all be watched or an invalid job is listed.
HISTORY_POLL_SECONDS = 5


# The history's height with its border and header: 5 rows show (owner decision); a horizontal scrollbar adds a line.
HISTORY_LINES = 8


class HistoryPane(SidewaysTable):
    """The execution history, newest first: paged, filtered, and kept current from the state store.

    ``busy`` says whether this TUI is running a job: it then refreshes the pane from that job's events, and holds the
    run lock itself, so polling waits. Only the newest read is shown, so a stale page never lands under a new filter.
    """

    # The history's height with its border and header: 5 rows show (owner decision).
    HEIGHT_LINES = HISTORY_LINES
    BINDINGS = [
        Binding("escape", "leave", "Command line", show=False),
        Binding("space", "toggle_mark", "Mark", show=False),
        Binding("d", "delete", "Delete", show=False),
    ]

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

    def __init__(self, reader: PaneHistory, *, busy: Callable[[], bool], leave: Callable[[], None], delete: Callable[[list[int]], None] = lambda numbers: None, **options: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **options)
        self.reader = reader
        self.busy = busy
        self.leave = leave
        # Asks to delete these executions, by number, in history order (Milestone 06).
        self.delete = delete
        # The executions marked for deletion, by number: kept across re-reads, cleared by a filter change or a deletion.
        self.marks: set[int] = set()
        # An execution's public number (E0012) to move the cursor to once a read shows it: this TUI's new job.
        self.select_when_shown: int | None = None
        # Not ``filter``, ``rows``, or ``loading``: DataTable and Widget already use those names.
        self.history_filter = HistoryFilter()
        # The executions shown, by ID.
        self.executions: dict[int, ExecutionRow] = {}
        # Whether no older execution is left to read.
        self.all_read = True
        self.reading = False
        # Counts reads; only the newest one's result is shown, and only it ends the reading.
        self.read_count = 0
        self.column_keys: list[ColumnKey] = []
        # Whether the last poll found another process holding the run lock; one more check follows its release.
        self.other_process_running = False
        # The busy message that other process would get, read without taking the lock; None while it is not held.
        self.other_process_message: str | None = None

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

    def action_toggle_mark(self) -> None:
        """``Space``: mark the selected row for deletion, or unmark it."""
        row_id = self.selected
        if row_id is None:
            return
        number = self.executions[row_id].execution_number
        self.marks.symmetric_difference_update({number})
        # update_width: the ID column is only as wide as its IDs, so the mark would crop the last digit.
        self.update_cell(str(row_id), self.column_keys[0], history_cells(self.executions[row_id], marked=number in self.marks)[0], update_width=True)

    def action_delete(self) -> None:
        """``d``: delete the marked rows, or the selected row when none is marked, asking first."""
        if self.marks:
            numbers = [row.execution_number for row in self.executions.values() if row.execution_number in self.marks]
        elif self.selected is not None:
            numbers = [self.executions[self.selected].execution_number]
        else:
            return
        self.delete(numbers)

    def check_lock(self) -> bool:
        """Whether another process holds the run lock now; posts LockChanged when that changes."""
        held = not self.busy() and not self.reader.lock_is_free()
        self.other_process_message = self.reader.lock_message() if held else None
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
        # The marked rows may no longer show.
        self.marks = set()
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
        if not self.current(request):
            return
        self.reading = False
        selected = None
        if page.offset == 0:
            selected = self.selected
            self.clear()
            self.executions = {}
        for row in page.rows:
            if row.id not in self.executions:
                self.executions[row.id] = row
                self.add_row(*history_cells(row, marked=row.execution_number in self.marks), key=str(row.id))
        self.all_read = page.complete
        if page.offset == 0:
            # A marked execution that left the history some other way (retention, another TUI) drops its mark.
            self.marks &= {row.execution_number for row in self.executions.values()}
        # Text, not str: a str title is parsed as markup, and the filter and error texts are the user's or the store's.
        self.border_title = Text(f"Execution History: {history_filter.text()}" if history_filter.text() else "Execution History")
        self.border_subtitle = Text(page.message or "")
        if page.offset == 0 and selected is not None and selected in self.executions:
            self.move_cursor(row=self.get_row_index(str(selected)))
        wanted, self.select_when_shown = self.select_when_shown, None
        # An execution number (E0012), not a row id: the feed only ever names an execution by its public label
        # (Milestone 03), and this page's own rows already carry both, so no extra lookup is needed to match one.
        # A filter that hides the new execution leaves the cursor where it is.
        if page.offset == 0 and wanted is not None:
            match = next((row for row in page.rows if row.execution_number == wanted), None)
            if match is not None:
                self.move_cursor(row=self.get_row_index(str(match.id)))
        if page.offset == 0:
            self.post_message(self.PageShown(self.selected, page.message))

    def row_id_for(self, execution_number: int) -> int | None:
        """The row id (this pane's own DataTable key) of an already-shown execution, by its public number
        (E0012); None when it is not currently shown."""
        return next((row_id for row_id, row in self.executions.items() if row.execution_number == execution_number), None)

    def remove_rows(self, numbers: list[int]) -> None:
        """After a deletion: drop the deleted executions' rows at once, move the cursor to the nearest remaining row,
        clear the marks, and read the history again."""
        cursor = self.cursor_row
        gone = set(numbers)
        gone_ids = [row_id for row_id, row in self.executions.items() if row.execution_number in gone]
        # The rows deleted above the cursor shift it: it stays on its execution, or, when that one went, on the next.
        above = sum(1 for row_id in gone_ids if self.get_row_index(str(row_id)) < cursor)
        for row_id in gone_ids:
            del self.executions[row_id]
            self.remove_row(str(row_id))
        marked, self.marks = self.marks, set()
        for row_id, row in self.executions.items():
            if row.execution_number in marked:
                self.update_cell(str(row_id), self.column_keys[0], history_cells(row)[0])
        if self.row_count:
            self.move_cursor(row=min(cursor - above, self.row_count - 1))
        self.load()

    def refresh_rows(self, execution_ids: list[int]) -> None:
        """Update rows already shown, in place, without reading the whole pane again."""
        self.read_rows(self.read_count, execution_ids)

    @work(thread=True, group="history-rows")
    def read_rows(self, request: int, execution_ids: list[int]) -> None:
        rows = self.reader.rows(execution_ids)
        if not isinstance(rows, str):
            self.app.call_from_thread(self.update_rows, request, rows)

    def update_rows(self, request: int, rows: list[ExecutionRow]) -> None:
        if not self.current(request):
            return
        for row in rows:
            if row.id not in self.executions:
                continue
            self.executions[row.id] = row
            for column, cell in zip(self.column_keys, history_cells(row, marked=row.execution_number in self.marks), strict=True):
                self.update_cell(str(row.id), column, cell, update_width=True)
        self.post_message(self.RowsUpdated([row.id for row in rows]))

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
            self.check(self.read_count, self.history_filter, [execution_id for execution_id, row in self.executions.items() if row.status == JobStatus.RUNNING])

    @work(thread=True, exclusive=True, group="history-poll")
    def check(self, request: int, history_filter: HistoryFilter, running: list[int]) -> None:
        """A newer execution means reading the pane again; otherwise only the running rows are read."""
        newest = self.reader.newest_id(history_filter)
        if not isinstance(newest, str):
            self.app.call_from_thread(self.after_check, request, newest, running)

    def after_check(self, request: int, newest: int | None, running: list[int]) -> None:
        if not self.current(request):
            return
        if newest is not None and newest not in self.executions:
            self.load()
        elif running:
            self.refresh_rows(running)
