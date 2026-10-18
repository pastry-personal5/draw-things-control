"""Work status: the TUI's multi-line current-work summary."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rich.text import Text

from framecontrol.services.queue_hold import HoldState
from framecontrol.tui.live_run import LiveRun
from framecontrol.tui.text.status import work_status_lines
from framecontrol.tui.widgets import SteadyText


class WorkStatus(SteadyText):
    """The job at a glance: its phase, the job and run (or wait) bars with their end times, the details, and the last run."""

    def on_mount(self) -> None:
        self.border_title = "Work status"

    def show(self, live: LiveRun | None, other_process: bool, *, message: str | None = None, hold: HoldState | None = None, queue: Sequence[dict[str, Any]] | None = None) -> None:
        width = self.content_size.width or 40
        # styles.tcss keeps each line to one row, ending in an ellipsis.
        self.show_text(Text("\n").join(work_status_lines(live, other_process, width, message=message, hold=hold, queue=queue)))
