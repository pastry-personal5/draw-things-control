"""Work status: the TUI's multi-line current-work summary."""

from __future__ import annotations

from rich.text import Text

from framechain.services.queue_hold import HoldState
from framechain.tui.live_run import LiveRun
from framechain.tui.text.status import work_status_lines
from framechain.tui.widgets import SteadyText


class WorkStatus(SteadyText):
    """The job at a glance: its phase, the job and run (or wait) bars with their end times, the details, and the last run."""

    def on_mount(self) -> None:
        self.border_title = "Work status"

    def show(self, live: LiveRun | None, other_process: bool, *, message: str | None = None, hold: HoldState | None = None) -> None:
        width = self.content_size.width or 40
        # styles.tcss keeps each line to one row, ending in an ellipsis.
        self.show_text(Text("\n").join(work_status_lines(live, other_process, width, message=message, hold=hold)))
