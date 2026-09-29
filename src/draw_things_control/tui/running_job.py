"""What the main screen does as the followed job runs: log its events, update the draw-things-cli pane, the Status
widget, and the status line, and (at verbose low) render the periodic ones only once a minute."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from textual.widgets import Static

from draw_things_control.jobs.events import JobEvent, RunFinished
from draw_things_control.state.ids import EXECUTION_LETTER, parse_typed_id
from draw_things_control.tui.preferences import LOW_STATUS_REFRESH_SECONDS
from draw_things_control.tui.text.arguments import run_arguments
from draw_things_control.tui.text.events import event_text, result_text
from draw_things_control.tui.text.status import status_line_text

if TYPE_CHECKING:
    from draw_things_control.tui.screens import MainScreen


class RunningJobView:
    """The main screen's running-job methods, in one place: its events arrive from the gRPC feed (``tui/feed.py``)."""

    def __init__(self, screen: MainScreen, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.screen = screen
        # A test's own clock, for the once-a-minute refresh at verbose low.
        self.clock = clock
        self._last_render = -math.inf

    def job_started(self, *, seeded: bool = False) -> None:
        """A job starts (live), or is attached to already running (``seeded``): either way its output replaces the
        last job's, marked "earlier output not shown" only when seeded, since only then is there earlier output
        this session never saw. A live start is logged in Messages, as every later event of the job is."""
        dtc = self.screen.dtc
        live = dtc.live
        if live is not None:
            if not seeded and live.started is not None:
                started = event_text(live.started)
                if started is not None:
                    self.screen.say(started)
            if dtc.verbose_level == "low":
                # A seeded step reading (attaching mid-run) would never move again at low.
                live.forget_progress()
            self.screen.cli.new_job(live, dtc.verbose_level, seeded=seeded)
        self.tick(force=True)
        self._show_new_execution()

    def _show_new_execution(self) -> None:
        """A new execution needs its row in the history. The cursor moves to it, unless the person is browsing the
        history or the detail."""
        screen = self.screen
        if screen.focused not in (screen.history, screen.detail):
            screen.history.select_when_shown = self._live_execution_number()
        screen.history.load()

    def job_event(self, event: JobEvent) -> None:
        """Log the event, update the draw-things-cli pane, and refresh the history where the store changed."""
        screen = self.screen
        text = event_text(event, *run_arguments(screen.dtc.live, event))
        if text is not None:
            screen.say(text)
        self.render_live()
        # A finished run changes only its row (the job's start, which adds the row, arrives through job_started; the
        # end of the job reads the pane again).
        number = self._live_execution_number()
        if isinstance(event, RunFinished) and number is not None:
            row_id = screen.history.row_id_for(number)
            if row_id is not None:
                screen.history.refresh_rows([row_id])

    def _live_execution_number(self) -> int | None:
        live = self.screen.dtc.live
        if live is None or live.execution_id is None:
            return None
        return parse_typed_id(live.execution_id, EXECUTION_LETTER)

    def job_ended(self) -> None:
        live = self.screen.dtc.live
        if live is not None:
            self.screen.say(result_text(live), block=True)
        self.render_live()
        self.screen.history.load()

    def render_live(self) -> None:
        dtc = self.screen.dtc
        self.screen.cli.show(dtc.live, dtc.verbose_level)
        self.tick(force=True)

    def render_status(self) -> None:
        screen = self.screen
        screen.status.show(screen.dtc.live, screen.history.other_process_running, message=screen.history.other_process_message)

    def tick(self, *, force: bool = False) -> None:
        """Update the elapsed time, the cooldown countdown, the Status widget, and the status line. The one caller
        that fires every second whatever happens (the screen's timer) leaves ``force`` off, so at verbose low it
        renders only once ``LOW_STATUS_REFRESH_SECONDS`` after the last render; every caller that runs because
        something worth showing just happened forces it."""
        dtc = self.screen.dtc
        if not force and dtc.verbose_level == "low" and self.clock() - self._last_render < LOW_STATUS_REFRESH_SECONDS:
            return
        self._last_render = self.clock()
        self.screen.cli.tick(dtc.live, dtc.verbose_level)
        self.render_status()
        self.screen.query_one("#status-line", Static).update(status_line_text(dtc.data_directory, dtc.live, dtc.job_running, dtc.quit_armed))
