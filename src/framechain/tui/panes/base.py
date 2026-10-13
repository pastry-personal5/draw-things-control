"""What the table panes share: the height rule for a table that scrolls sideways, and reads whose newest result wins."""

from __future__ import annotations

from typing import cast

from rich.text import Text
from textual.widget import Widget
from textual.widgets import DataTable


class Reads:
    """A pane that reads on workers: each read is counted, and only the newest one's result is shown, so a stale page never
    lands under a new filter."""

    read_count = 0

    def is_newest(self, request: int) -> bool:
        return request == self.read_count

    def current(self, request: int) -> bool:
        """Whether a worker's result for ``request`` may be shown: the pane is still on screen and no newer read began."""
        # A mixin for Textual widgets, which have is_attached.
        return cast(Widget, self).is_attached and self.is_newest(request)


class SidewaysTable(DataTable[Text], Reads):
    """A table whose narrow column scrolls sideways; the scrollbar then takes a line of its own, so its rows still show."""

    # The height with its border and header, without a scrollbar.
    HEIGHT_LINES = 8

    def watch_show_horizontal_scrollbar(self, shown: bool) -> None:
        self.styles.height = self.HEIGHT_LINES + (1 if shown else 0)
