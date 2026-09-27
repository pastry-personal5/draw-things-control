"""The Execution widget: an execution's settings and its successful runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Static
from textual.worker import get_current_worker

from draw_things_control.state.executions import ExecutionRow
from draw_things_control.tui.desktop import reveal_run
from draw_things_control.tui.panes.base import Reads
from draw_things_control.tui.reader import PaneHistory
from draw_things_control.tui.text.common import STATUS_STYLE
from draw_things_control.tui.text.execution import ExecutionDetail, execution_detail, execution_detail_text

# How long the detail waits after the history cursor stops, so holding an arrow key does not read every row it passes.
DETAIL_PAUSE_SECONDS = 0.2


# The detail's lines above its runs (model, refiner, size, and the header), and the lines each run takes.
DETAIL_HEAD_LINES = 4


DETAIL_RUN_LINES = 2


class ExecutionBody(Static):
    """The detail's text; its file-name links run ``reveal`` here, which hands the numbers to the pane."""

    def action_reveal(self, execution_id: int, run_number: int) -> None:
        pane = self.parent
        if isinstance(pane, ExecutionPane):
            pane.select(run_number)
            pane.reveal(execution_id, run_number)


class ExecutionPane(VerticalScroll, Reads):
    """The execution under the history cursor: its settings and its successful runs, each with a link that reveals its output.

    Up and Down select a run, and Enter or a click on its file name reveals it in Finder. Reads go through the history's
    reader on a worker thread, and only the newest read is shown, as in the history.
    """

    BINDINGS = [
        Binding("up", "move(-1)", "Earlier run", show=False),
        Binding("down", "move(1)", "Later run", show=False),
        Binding("pageup", "page(-1)", "Page up", show=False),
        Binding("pagedown", "page(1)", "Page down", show=False),
        Binding("home", "jump(0)", "First run", show=False),
        Binding("end", "jump(-1)", "Last run", show=False),
        Binding("enter", "reveal_selected", "Reveal", show=False),
        Binding("escape", "leave", "Command line", show=False),
    ]

    def __init__(self, reader: PaneHistory, *, say: Callable[[Text | str, str], None], leave: Callable[[], None], **options: Any) -> None:
        super().__init__(**options)
        self.reader = reader
        self.say = say
        self.leave = leave
        self.execution: ExecutionRow | None = None
        # What the widget shows of the execution, read with it.
        self.detail: ExecutionDetail | None = None
        # The run numbers listed, in order, and the one selected.
        self.shown: list[int] = []
        self.selected: int | None = None
        self.read_count = 0
        self._pause: Timer | None = None
        # What shows when there is no execution: the history's message, or an error.
        self.message = Text("")

    @property
    def execution_id(self) -> int | None:
        return self.execution.id if self.execution is not None else None

    def compose(self) -> ComposeResult:
        yield ExecutionBody(id="execution-body")

    def on_mount(self) -> None:
        self.border_title = "Execution"

    def follow(self, execution_id: int | None, *, pause: bool = True) -> None:
        """Show ``execution_id``, after DETAIL_PAUSE_SECONDS unless ``pause`` is false; None shows the message instead."""
        if self._pause is not None:
            self._pause.stop()
            self._pause = None
        if execution_id is None:
            self.read_count += 1
            self.execution, self.shown, self.selected = None, [], None
            self.render_detail()
            return
        if pause:
            self._pause = self.set_timer(DETAIL_PAUSE_SECONDS, lambda: self.load(execution_id))
        else:
            self.load(execution_id)

    def show_message(self, message: str) -> None:
        self.message = Text(message, style="dim")
        if self.execution is None:
            self.render_detail()

    def load(self, execution_id: int) -> None:
        self._pause = None
        self.read_count += 1
        self.read_execution(self.read_count, execution_id)

    @work(thread=True, exclusive=True, group="execution-detail")
    def read_execution(self, request: int, execution_id: int) -> None:
        execution = self.reader.execution(execution_id)
        # Read here, off the UI thread: it looks for every output file, which a redraw must not.
        detail = None if isinstance(execution, str) else execution_detail(execution)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_execution, request, execution_id, execution, detail)

    def show_execution(self, request: int, execution_id: int, execution: ExecutionRow | str, detail: ExecutionDetail | None = None) -> None:
        if not self.current(request):
            return
        if isinstance(execution, str):
            self.execution, self.shown, self.selected = None, [], None
            self.message = Text(execution, style="red")
            self.render_detail()
            return
        # The same execution read again keeps its selected run; another one starts at its first run.
        keep = self.selected if self.execution_id == execution_id else None
        self.execution, self.detail = execution, detail if detail is not None else execution_detail(execution)
        self.shown = [run.number for run in self.detail.runs]
        self.selected = keep if keep in self.shown else (self.shown[0] if self.shown else None)
        self.render_detail()

    def render_detail(self) -> None:
        body = self.query_one(ExecutionBody)
        if self.execution is None or self.detail is None:
            self.border_title = "Execution"
            body.update(self.message)
            return
        # Text, not str: a str title is parsed as markup, and the job name is the user's.
        title = Text(f"Execution {self.execution.label}: {self.execution.job_name} ")
        title.append(self.execution.status, style=STATUS_STYLE.get(self.execution.status, ""))
        self.border_title = title
        text, self.shown = execution_detail_text(self.detail, self.scrollable_content_region.width, self.selected)
        body.update(text)

    def on_resize(self, event: events.Resize) -> None:
        self.render_detail()

    def select(self, run_number: int) -> None:
        if run_number in self.shown:
            self.selected = run_number
            self.render_detail()
            self.keep_in_view()

    def keep_in_view(self) -> None:
        if self.selected is None:
            return
        top = DETAIL_HEAD_LINES + self.shown.index(self.selected) * DETAIL_RUN_LINES
        height = self.scrollable_content_region.height
        if top < self.scroll_y:
            self.scroll_to(y=top, animate=False)
        elif top + DETAIL_RUN_LINES > self.scroll_y + height:
            self.scroll_to(y=top + DETAIL_RUN_LINES - height, animate=False)

    def action_move(self, step: int) -> None:
        if not self.shown:
            return
        index = self.shown.index(self.selected) if self.selected in self.shown else 0
        self.select(self.shown[min(max(index + step, 0), len(self.shown) - 1)])

    def action_page(self, step: int) -> None:
        self.action_move(step * max(1, self.scrollable_content_region.height // DETAIL_RUN_LINES))

    def action_jump(self, index: int) -> None:
        if self.shown:
            self.select(self.shown[index])

    def action_leave(self) -> None:
        self.leave()

    def action_reveal_selected(self) -> None:
        if self.execution_id is not None and self.selected is not None:
            self.reveal(self.execution_id, self.selected)

    def on_click(self, event: events.Click) -> None:
        # A click on a run's lines selects it; one on its file name has already revealed it, and stopped the click.
        offset = event.get_content_offset(self.query_one(ExecutionBody))
        if offset is None or offset.y < DETAIL_HEAD_LINES:
            return
        index = (offset.y - DETAIL_HEAD_LINES) // DETAIL_RUN_LINES
        if index < len(self.shown):
            self.select(self.shown[index])

    @work(thread=True, group="execution-reveal")
    def reveal(self, execution_id: int, run_number: int) -> None:
        """Reveal the run's output in Finder, as /reveal does; the file is looked up again, so one removed since is reported."""
        self.app.call_from_thread(self.say, *reveal_run(self.reader.execution(execution_id), run_number))
