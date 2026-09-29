"""The Textual application: its collaborators, the quit key, its one screen, and the queue entry it follows."""

from __future__ import annotations

import signal
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding

from draw_things_control.core.client_config import job_argument
from draw_things_control.core.exit_codes import exit_code_for_signal
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.services.toolkit import Toolkit
from draw_things_control.tui.feed import QueueFeed
from draw_things_control.tui.live_run import LiveRun, PastRun, latest_past_run
from draw_things_control.tui.panes.cli_output import LOW_MARKER
from draw_things_control.tui.preferences import VerboseLevel, load_verbose_level, save_verbose_level, wants_output
from draw_things_control.tui.queue_client import ApiError, cancel, list_queue, resume, show, submit
from draw_things_control.tui.screens import MainScreen
from draw_things_control.tui.signals import QuitPress, SignalGuard
from draw_things_control.tui.text.queue import queue_entry_detail_text, queue_listing_text

if TYPE_CHECKING:
    import httpx

    from draw_things_control.tui.client import GrpcStubFactory

# How long a first Ctrl-C waits for the second that quits.
QUIT_PRESS_SECONDS = 2.0
QUEUE_LIST_LIMIT = 5
LOW_NOTICE = "Verbose level: low. Bare output is hidden and status updates once a minute."


class DrawThingsApp(App[None]):
    """Browse the jobs in a data directory and the server's queue and history; browsing writes nothing.

    Milestone 03: this process never starts ``draw-things-cli`` itself. ``/apply`` (and ``/queue add``) submit to
    the queue over the HTTP API; the running job's own events, and every queue change, arrive over one gRPC
    ``WatchEvents`` connection (``tui/feed.py``), fed by the server's worker, the only thing that still runs one.
    """

    TITLE = "Draw Things Control"
    CSS_PATH = "styles.tcss"
    # Commands are typed on the command line; the palette would be a second way, and a way to change the theme.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        # Before any widget can take the key: Textual's own ctrl+c only explains how to quit, and Input's copies.
        Binding("ctrl+c", "interrupt", "Quit", show=False, priority=True),
    ]

    def __init__(self, *, settings: GlobalConfig, paths: ProjectPaths, data_directory: Path, server_url: str, token_file: Path, allow_remote_server: bool = False, toolkit: Toolkit | None = None, http_transport: "httpx.AsyncBaseTransport | None" = None, grpc_stub_factory: "GrpcStubFactory | None" = None) -> None:
        super().__init__()
        self.settings = settings
        self.paths = paths
        self.data_directory = data_directory
        self.server_url = server_url
        self.token_file = token_file
        self.allow_remote_server = allow_remote_server
        # Only for the Job Definition widget's offline dry-run plan preview (services/job_details.py's add_plan):
        # the TUI never runs a job through it any more (Milestone 03), so a test's own FakeToolkit needs no runner.
        self.toolkit = toolkit or Toolkit()
        self.http_transport = http_transport
        self.grpc_stub_factory = grpc_stub_factory
        self.store_provider = StoreProvider(paths, settings.history_retention_days)
        # The entry this session follows, or the last one it saw before it ended.
        self.live: LiveRun | None = None
        # The queue entry a live JobStarted, when it arrives, belongs to: set from queue_entry_changed's own
        # ordering guarantee (it always precedes that entry's JobStarted), read once and then cleared.
        self.pending_queue_id: str | None = None
        self.feed_connected = False
        # How much of draw-things-cli's output the TUI streams and shows (Milestone 04); read once, changed by /verbose.
        self.verbose_level: VerboseLevel = load_verbose_level(paths.tui_preferences)
        # Low at startup is told once, as the feed first connects (a later reconnect says nothing).
        self._low_notice_due = self.verbose_level == "low"
        self._feed = QueueFeed(self)
        self.signals = SignalGuard(self.handle_signal)
        self.quit_press = QuitPress(QUIT_PRESS_SECONDS)
        self.theme = "textual-dark"

    @property
    def job_running(self) -> bool:
        """From a live JobStarted (or a seeding read finding one) until the entry is known to be over."""
        return self.live is not None and not self.live.ended

    def on_mount(self) -> None:
        self.signals.install()
        self.push_screen(MainScreen())
        self.run_feed()

    def on_unmount(self) -> None:
        self.store_provider.close()

    @work(exclusive=True, group="feed")
    async def run_feed(self) -> None:
        await self._feed.run()

    def on_feed_connection_changed(self) -> None:
        """The gRPC feed connected or dropped: the Status widget's busy-lock line reads the run lock only while
        the feed cannot itself say the server is up (``dtc serve`` holds that lock for its whole lifetime, not
        only while running a job, so a feed that is up already says everything that line otherwise would)."""
        if self.main is not None:
            self.main.history.check_lock()
        if self.feed_connected and self._low_notice_due and self.main is not None:
            self._low_notice_due = False
            self.say(LOW_NOTICE)

    def set_verbose_level(self, level: VerboseLevel) -> None:
        """/verbose LEVEL: apply it for the session, keep it for the next, and reconnect the shared stream when the
        level crosses the low boundary (``include_output`` differs); high and medium request the same lines."""
        old = self.verbose_level
        if level == old:
            self.say(f"Verbose level: {level}.")
            return
        self.verbose_level = level
        self._low_notice_due = False
        crossed = wants_output(level) != wants_output(old)
        connected = self.feed_connected
        if crossed and not connected:
            self.say(f"Verbose level: {level}. It applies when dtc serve is reachable.")
        else:
            self.say(LOW_NOTICE if level == "low" else f"Verbose level: {level}.")
        if crossed and connected:
            self.say(f"Reconnecting to dtc serve for verbose {level}...")
            self.run_feed()
        self._save_verbose_level(level)
        live, main = self.live, self.main
        if live is not None and self.job_running:
            if level == "medium":
                live.output_window_start = live.now()
            elif level == "low":
                live.forget_progress()
        if main is None:
            return
        if level == "low" and self.job_running:
            main.cli.write_marker(LOW_MARKER)
        # At once, not at low's next once-a-minute refresh: the run line and the Status widget change with the level.
        main.running.tick(force=True)

    def _save_verbose_level(self, level: VerboseLevel) -> None:
        try:
            save_verbose_level(self.paths.tui_preferences, level)
        except OSError as error:
            self.say(f"The verbose level could not be saved ({error}); it applies to this session only.", "yellow")

    def handle_signal(self, received: signal.Signals) -> None:
        """SIGHUP, SIGINT, or SIGTERM: exit at once, with the code the CLI would give the same signal. Nothing here
        needs stopping first -- dtc serve is the only thing that ever runs a job now, and quitting the TUI does not
        touch it (Milestone 03)."""
        self.exit(return_code=exit_code_for_signal(received))

    @property
    def main(self) -> MainScreen | None:
        return next((screen for screen in self.screen_stack if isinstance(screen, MainScreen) and screen.is_mounted), None)

    def say(self, text: Text | str, style: str = "") -> None:
        """Write a notice to the messages."""
        if self.main is not None:
            self.main.say(text, style)

    @property
    def quit_armed(self) -> bool:
        """Whether a first Ctrl-C is waiting for the second."""
        return self.quit_press.armed

    async def action_interrupt(self) -> None:
        """Ctrl-C: clear the command line if it holds text; otherwise quit on a second press within QUIT_PRESS_SECONDS."""
        main = self.main
        if main is not None and main.command_line.value:
            main.command_line.action_clear_line()
            return
        if self.quit_press.armed:
            self.quit_press.disarm()
            self.show_status()
            await self.action_quit()
            return
        self.quit_press.arm()
        self.show_status()
        self.set_timer(QUIT_PRESS_SECONDS, self.show_status)

    def show_status(self) -> None:
        if self.main is not None:
            self.main.running.tick(force=True)

    async def action_quit(self) -> None:
        """Ends the app at once, whether or not a job is running: dtc serve is what runs it, and quitting the TUI
        does not stop that (Milestone 03, the same change /apply's own retirement makes)."""
        self.exit()

    def start_flow(self, path: Path) -> None:
        """/apply <job>: submit it to the queue. No confirmation: queuing is reversible (/queue cancel undoes it),
        and dtc serve, not this process, is what actually runs it."""
        self.submit_job(job_argument(str(path), self.paths))

    @work(exclusive=True, group="submit")
    async def submit_job(self, job: str) -> None:
        try:
            entry = await submit(self.server_url, self.token_file, self.http_transport, job)
        except ApiError as error:
            self.say(error.text, "red")
            return
        self.say(f"{entry['queue_id']} queued: {Path(entry['job_path']).name}")
        self.refresh_queue()

    @work(exclusive=True, group="queue-refresh")
    async def refresh_queue(self) -> None:
        try:
            entries = await list_queue(self.server_url, self.token_file, self.http_transport, limit=QUEUE_LIST_LIMIT)
        except ApiError:
            return
        if self.main is not None:
            self.main.queue.show_entries(entries)

    @work(exclusive=True, group="queue-listing")
    async def show_queue(self) -> None:
        """/get queue: the entries in Messages, not only the widget's own rows."""
        try:
            entries = await list_queue(self.server_url, self.token_file, self.http_transport, limit=QUEUE_LIST_LIMIT)
        except ApiError as error:
            self.say(error.text, "red")
            return
        self.say(queue_listing_text(entries))

    @work(exclusive=True, group="cancel")
    async def cancel_entry(self, queue_id: str) -> None:
        try:
            entry = await cancel(self.server_url, self.token_file, self.http_transport, queue_id)
        except ApiError as error:
            self.say(error.text, "red")
            return
        if self.live is not None and self.live.queue_id == queue_id:
            self.live.request_stop()
            # "stopping" shows at once at every level, not at low's next once-a-minute refresh.
            self.show_status()
        self.say(f"{entry['queue_id']} {entry['state']}")
        self.refresh_queue()

    @work(exclusive=True, group="resume")
    async def resume_entry(self, queue_id: str) -> None:
        try:
            entry = await resume(self.server_url, self.token_file, self.http_transport, queue_id)
        except ApiError as error:
            self.say(error.text, "red")
            return
        self.say(f"{entry['queue_id']} queued (resumed from {queue_id}): {Path(entry['job_path']).name}")
        self.refresh_queue()

    @work(exclusive=True, group="queue-detail")
    async def describe_queue_entry(self, queue_id: str) -> None:
        """/describe Q0007, and Enter on its row (QueuePane): over the API, not the store fallback (its own
        resumability needs a live read: whether some other entry already resumes it can change between polls)."""
        try:
            entry = await show(self.server_url, self.token_file, self.http_transport, queue_id)
        except ApiError as error:
            self.say(error.text, "red")
            return
        self.say(queue_entry_detail_text(entry))

    def read_past_run(self) -> PastRun | None:
        """The latest successful run of any job, to estimate a newly started (or seeded) one from; called off the
        event loop (``asyncio.to_thread``), since it is a blocking read. None when the store cannot say."""
        try:
            store = self.store_provider.get(create=False)
        except Exception:
            return None
        return latest_past_run(store) if store is not None else None
