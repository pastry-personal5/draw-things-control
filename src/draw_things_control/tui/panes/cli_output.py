"""The draw-things-cli pane: the run line and the child's output."""

from __future__ import annotations

import itertools
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog

from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, LiveRun
from draw_things_control.tui.preferences import MEDIUM_OUTPUT_WINDOW_SECONDS, VerboseLevel
from draw_things_control.tui.text.status import run_line_text
from draw_things_control.tui.widgets import SteadyText

# Why the pane went quiet, written in dim text: at medium once per closed window; at low whenever the pane's content
# is (re)established, since no line ever arrives there to be skipped.
MEDIUM_MARKER = "(output hidden: verbose medium, after 1 min)"
LOW_MARKER = "(output hidden: verbose low; status updates once a minute)"


class CliPane(Vertical):
    """The run line and the child's output, rendered from the app's LiveRun; it writes only the lines not yet written."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        # Lines of the LiveRun's output already written.
        self.written = 0
        # The window start the medium marker was last written for: it is written once per closed window.
        self._marked_window: float | None = None

    def compose(self) -> ComposeResult:
        yield SteadyText(id="run-line")
        yield RichLog(id="cli-output", max_lines=MAX_OUTPUT_LINES, wrap=True, min_width=20)

    def on_mount(self) -> None:
        self.border_title = "draw-things-cli"

    def new_job(self, live: LiveRun, level: VerboseLevel = "high", *, seeded: bool = False) -> None:
        """A job starts: its output replaces the last job's. ``seeded`` (attaching to one already running,
        Milestone 03) marks the pane, since the state store never held draw-things-cli's own output: only what
        arrives from here on is ever shown for it."""
        log = self.query_one(RichLog)
        log.clear()
        self.written = 0
        self._marked_window = None
        if seeded:
            self.write_marker("(earlier output not shown)")
        if level == "low":
            self.write_marker(LOW_MARKER)
        self.show(live, level)

    def write_marker(self, text: str) -> None:
        self.query_one(RichLog).write(Text(text, style="dim"))

    def show(self, live: LiveRun | None, level: VerboseLevel = "high") -> None:
        """Show the LiveRun as it is now: any new output lines (progress lines included), and the run line."""
        self.write_output(live, level)
        self.tick(live, level)

    def tick(self, live: LiveRun | None, level: VerboseLevel = "high") -> None:
        """Update the elapsed time and the cooldown countdown."""
        self.query_one("#run-line", SteadyText).show_text(run_line_text(live, level))

    def write_output(self, live: LiveRun | None, level: VerboseLevel = "high") -> None:
        """Write the lines not yet written, unless the level hides them: low always, medium after its window. A hidden
        line is skipped for good (``written`` still advances), never written late."""
        if live is None:
            return
        pane = self.query_one(RichLog)
        new = min(live.output_count - self.written, len(live.output))
        window_start = self._closed_window(live, level)
        if new and window_start is not None and window_start != self._marked_window:
            self._marked_window = window_start
            self.write_marker(MEDIUM_MARKER)
        if window_start is None and level != "low":
            # islice, not a copy of the whole deque, for every line the child prints.
            for line in itertools.islice(live.output, len(live.output) - new, None):
                # Text, never markup: the child's lines and the prompts in them contain brackets.
                pane.write(Text(line.text, style="red" if line.stream == "stderr" else ""))
        self.written = live.output_count

    @staticmethod
    def _closed_window(live: LiveRun, level: VerboseLevel) -> float | None:
        """At medium, the start of the window that has closed, or None while it is open (or at any other level).
        The window starts at the active run's start or the moment /verbose medium was typed, whichever is later;
        between runs, with neither known, nothing says the window has closed."""
        if level != "medium":
            return None
        starts = [start for start in (live.run_started_at, live.output_window_start) if start is not None]
        if not starts:
            return None
        start = max(starts)
        return start if live.now() - start > MEDIUM_OUTPUT_WINDOW_SECONDS else None
