"""The Job Definition widget."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import DataTable
from textual.worker import get_current_worker

from draw_things_control.services.job_catalog import JobCatalog, JobListing, JobRow
from draw_things_control.tui.commands import SORT_KEYS
from draw_things_control.tui.job_sort import SortPreference
from draw_things_control.tui.job_watch import JobWatcher
from draw_things_control.tui.panes.base import SidewaysTable
from draw_things_control.tui.text.jobs import job_definition_cells, job_display_names, sort_job_rows

JOBS_POLL_SECONDS = 5


# How long the job list waits after a file event for more, since one save can report several.
JOBS_WATCH_PAUSE_SECONDS = 0.3


# The Job Definition widget's height with its border and header: 8 job rows show (owner decision).
JOB_DEFINITION_LINES = 11


class JobDefinitionPane(SidewaysTable):
    """The job files in the data directory, with their job IDs, sorted by any column; read again when a job file or a file
    a valid job reads changes. Every 5 seconds instead while that cannot be watched or an invalid job is listed, since an
    invalid job's inputs are not known.

    Enter describes the selected job and ``a`` asks to run it, through the screen's callbacks; ``s`` sorts by the next
    column and ``r`` reverses the order, and the sort is kept in the state store across sessions.
    """

    HEIGHT_LINES = JOB_DEFINITION_LINES
    BINDINGS = [
        Binding("s", "next_sort", "Sort by the next column", show=False),
        Binding("r", "reverse_sort", "Reverse the order", show=False),
        Binding("a", "run_selected", "Run the job", show=False),
        Binding("escape", "leave", "Command line", show=False),
    ]

    def __init__(self, catalog: JobCatalog, sort_preference: SortPreference, *, describe: Callable[[Path], None], run: Callable[[Path], None], announce: Callable[[JobListing], None], leave: Callable[[], None], **options: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **options)
        self.catalog = catalog
        self.sort_preference = sort_preference
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

    def load(self, *, fresh: bool = False, announce: bool = False) -> None:
        """Read the directory again; ``fresh`` reads every file, and ``announce`` also writes the list to Messages."""
        self.read_count += 1
        self.read_jobs(self.read_count, fresh, announce)

    @work(thread=True, exclusive=True, group="job-definitions-sort")
    def read_sort(self) -> None:
        key, descending = self.sort_preference.sort()
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
        watched = self.watcher.follow(listing.reads) if self.is_newest(request) else False
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_listing, request, listing, announce, watched)

    def show_listing(self, request: int, listing: JobListing, announce: bool, watched: bool = False) -> None:
        if not self.is_attached:
            return
        if self.is_newest(request) and self.check_timer is not None:
            if watched and not listing.any_invalid:
                self.check_timer.pause()
            else:
                self.check_timer.resume()
        # /get jobs writes the list; otherwise a message (no files, no directory) is written once, when it first appears.
        if announce or (listing.message is not None and listing.message != self.announced):
            self.announce_listing(listing)
        self.announced = listing.message
        if not self.is_newest(request):
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
        error = self.sort_preference.keep(key, descending, choice)
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
