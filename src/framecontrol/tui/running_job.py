"""What the main screen does as the followed job runs: log its events, update the generation output, the Status
widget, and the status line, and (at verbose low) render the periodic ones only once a minute."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from framecontrol.jobs.events import JobEvent, RunFinished
from framecontrol.state.ids import EXECUTION_LETTER, parse_typed_id
from framecontrol.tui.preferences import LOW_STATUS_REFRESH_SECONDS
from framecontrol.tui.text.arguments import run_arguments
from framecontrol.tui.text.events import event_text, result_text
from framecontrol.tui.text.status import stage_text

if TYPE_CHECKING:
    from framecontrol.tui.screens import MainScreen


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
        this session never saw. A live start is logged in activity log, as every later event of the job is."""
        fc = self.screen.fc
        live = fc.live
        if live is not None:
            if not seeded and live.started is not None:
                started = event_text(live.started)
                if started is not None:
                    self.screen.say(started)
            self.screen.generation_output.new_job(live, fc.verbose_level, seeded=seeded)
        self.tick(force=True)
        self._show_new_execution()

    def _show_new_execution(self) -> None:
        """A new execution needs its row in the history. The cursor moves to it, unless the person is browsing the
        history or the detail."""
        screen = self.screen
        if screen.focused not in (screen.execution_history, screen.execution_detail):
            screen.execution_history.select_when_shown = self._live_execution_number()
        screen.execution_history.load()

    def job_event(self, event: JobEvent) -> None:
        """Log the event, update the generation output, and refresh the history where the store changed."""
        screen = self.screen
        text = event_text(event, *run_arguments(screen.fc.live, event))
        if text is not None:
            screen.say(text)
        self.render_live()
        # A finished run changes only its row (the job's start, which adds the row, arrives through job_started; the
        # end of the job reads the pane again).
        number = self._live_execution_number()
        if isinstance(event, RunFinished) and number is not None:
            row_id = screen.execution_history.row_id_for(number)
            if row_id is not None:
                screen.execution_history.refresh_rows([row_id])

    def _live_execution_number(self) -> int | None:
        live = self.screen.fc.live
        if live is None or live.execution_id is None:
            return None
        return parse_typed_id(live.execution_id, EXECUTION_LETTER)

    def job_ended(self) -> None:
        live = self.screen.fc.live
        if live is not None:
            self.screen.say(result_text(live), block=True)
        self.render_live()
        self.screen.execution_history.load()

    def render_live(self) -> None:
        fc = self.screen.fc
        self.screen.generation_output.show(fc.live, fc.verbose_level)
        self.tick(force=True)

    def render_status(self) -> None:
        screen = self.screen
        screen.work_status.show(screen.fc.live, screen.execution_history.other_process_running, message=screen.execution_history.other_process_message, hold=screen.fc.queue_hold)

    def tick(self, *, force: bool = False) -> None:
        """Update the elapsed time, the cooldown countdown, the work status, and the status line. The one caller
        that fires every second whatever happens (the screen's timer) leaves ``force`` off, so at verbose low it
        renders only once ``LOW_STATUS_REFRESH_SECONDS`` after the last render; every caller that runs because
        something worth showing just happened forces it."""
        fc = self.screen.fc
        if not force and fc.verbose_level == "low" and self.clock() - self._last_render < LOW_STATUS_REFRESH_SECONDS:
            return
        self._last_render = self.clock()
        self.screen.generation_output.tick(fc.live, fc.verbose_level)
        self.render_status()
        live = fc.live
        running = live is not None and live.active_run is not None and not live.ended
        self.screen.execution_detail.show_live(live.execution_id if running and live is not None else None, stage_text(live.forecast) if running and live is not None else None)
        self.screen.refresh_status_line()
