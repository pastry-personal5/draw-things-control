"""The draw-things-cli pane: the run line and the child's output."""

from __future__ import annotations

import itertools
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from draw_things_control.jobs.events import JobEvent
from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, LiveRun
from draw_things_control.tui.text.status import run_line_text


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

    def new_job(self, live: LiveRun, *, seeded: bool = False) -> None:
        """A job starts: its output replaces the last job's. ``seeded`` (attaching to one already running,
        Milestone 03) marks the pane, since the state store never held draw-things-cli's own output: only what
        arrives from here on is ever shown for it."""
        log = self.query_one(RichLog)
        log.clear()
        self.written = 0
        if seeded:
            log.write(Text("(earlier output not shown)", style="dim"))
        self.show(live)

    def show(self, live: LiveRun | None, event: JobEvent | None = None) -> None:
        """Show the LiveRun as it is now: any new output lines (progress lines included), and the run line."""
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
