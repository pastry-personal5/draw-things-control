"""The Status widget."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from draw_things_control.tui.live_run import LiveRun
from draw_things_control.tui.text.status import status_lines


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
