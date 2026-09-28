"""The Queue widget: the running entry first, then the queued ones in order, then up to 5 finished ones newest
first -- live from the gRPC feed while the server is up, or the state store's own read-only fallback while it is
down (Milestone 03)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from textual import work
from textual.binding import Binding
from textual.widgets import DataTable
from textual.worker import get_current_worker

from draw_things_control.services.queue_reader import QueueReader
from draw_things_control.tui.panes.base import SidewaysTable
from draw_things_control.tui.text.queue import queue_cells, queue_row_to_dict

if TYPE_CHECKING:
    from draw_things_control.tui.app import DrawThingsApp

# How often the fallback reader checks the store while the feed is down; the feed itself pushes every change while
# it is up, so polling then does nothing (owner decision, mirrors HistoryPane's HISTORY_POLL_SECONDS).
QUEUE_POLL_SECONDS = 5
# The pane's height with its border and header: 5 rows show (owner decision), a horizontal scrollbar adds a line.
QUEUE_LINES = 8
MAX_FINISHED_ROWS = 5


class QueuePane(SidewaysTable):
    """Submits, cancels, and resumes through the API; browsing itself writes nothing."""

    HEIGHT_LINES = QUEUE_LINES
    BINDINGS = [
        Binding("escape", "leave", "Command line", show=False),
        Binding("c", "cancel_selected", "Cancel", show=False),
    ]

    def __init__(self, reader: QueueReader, *, feed_connected: Callable[[], bool], leave: Callable[[], None], **options: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **options)
        self.reader = reader
        self.feed_connected = feed_connected
        self.leave = leave
        # The rows shown, by queue ID, in the API's own dict shape (queue_row_to_dict adapts a store QueueRow to it).
        self.rows_by_id: dict[str, dict[str, Any]] = {}

    def on_mount(self) -> None:
        self.border_title = "Queue"
        self.add_columns("ID", "Job", "State", "Runs")
        self.set_interval(QUEUE_POLL_SECONDS, self.poll)
        self.poll()

    def action_leave(self) -> None:
        self.leave()

    def action_cancel_selected(self) -> None:
        """``c``: the same as /queue cancel on the selected row's ID, no confirmation, as that command has none."""
        if self.row_count == 0:
            return
        queue_id = str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value)
        cast("DrawThingsApp", self.app).cancel_entry(queue_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: the same as /describe on its ID."""
        if event.row_key.value is not None:
            cast("DrawThingsApp", self.app).describe_queue_entry(str(event.row_key.value))

    def show_entries(self, entries: list[dict[str, Any]]) -> None:
        """Shown directly by the gRPC feed worker (the same asyncio loop as this widget, so no thread hop is
        needed): every active entry, and up to ``MAX_FINISHED_ROWS`` finished ones, in the order given. Counted as
        a read too, so a slower fallback read already in flight cannot land after it and overwrite it with a stale
        store snapshot."""
        self.read_count += 1
        self._show(entries)

    def poll(self) -> None:
        """While the feed is down, the store is the only way to see the queue; while it is up, the feed itself
        pushes every relevant change directly (show_entries), so polling would only duplicate that."""
        if not self.feed_connected():
            self.read_count += 1
            self.read_fallback(self.read_count)

    @work(thread=True, exclusive=True, group="queue-fallback")
    def read_fallback(self, request: int) -> None:
        snapshot = self.reader.snapshot()
        entries = [queue_row_to_dict(row) for row in (*snapshot.active, *snapshot.finished[:MAX_FINISHED_ROWS])]
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._show_if_current, request, entries)

    def _show_if_current(self, request: int, entries: list[dict[str, Any]]) -> None:
        if self.current(request):
            self._show(entries)

    def _show(self, entries: list[dict[str, Any]]) -> None:
        selected = str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value) if self.row_count else None
        self.clear()
        self.rows_by_id = {entry["queue_id"]: entry for entry in entries}
        for entry in entries:
            self.add_row(*queue_cells(entry), key=entry["queue_id"])
        if selected is not None and selected in self.rows_by_id:
            self.move_cursor(row=self.get_row_index(selected))
