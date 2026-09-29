"""The Status widget."""

from __future__ import annotations

from rich.text import Text

from draw_things_control.tui.live_run import LiveRun
from draw_things_control.tui.text.status import status_lines
from draw_things_control.tui.widgets import SteadyText


class StatusPane(SteadyText):
    """The job at a glance: its phase, the job and run (or wait) bars with their end times, the details, and the last run."""

    def on_mount(self) -> None:
        self.border_title = "Status"

    def show(self, live: LiveRun | None, other_process: bool, *, message: str | None = None) -> None:
        width = self.content_size.width or 40
        # styles.tcss keeps each line to one row, ending in an ellipsis.
        self.show_text(Text("\n").join(status_lines(live, other_process, width, message=message)))
