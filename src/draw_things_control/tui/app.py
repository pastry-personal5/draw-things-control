"""The Textual application: its collaborators, the quit key, its one screen, and the job it runs."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sqlite3
import threading
import time
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.message import Message

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_events import JobEvent, JobStarted, combine_observers
from draw_things_control.jobs.job_report import read_job
from draw_things_control.jobs.job_service import JobService
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import StateError, Store
from draw_things_control.tui.job_files import error_text
from draw_things_control.tui.live_run import JobEventMessage, JobWorkerEnded, LiveRun
from draw_things_control.tui.screens import ConfirmScreen, MainScreen
from draw_things_control.tui.text import confirm_run_text, question_text

# Stopped as the CLI is: the job ends as interrupted by the signal, and dtc tui exits with 128+N.
HANDLED_SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
# How long a first Ctrl-C waits for the second that quits.
QUIT_PRESS_SECONDS = 2.0


class ReadForRun(Message):
    """A job file read again, to confirm and run."""

    def __init__(self, path: Path, job: JobDefinition) -> None:
        super().__init__()
        self.path = path
        self.job = job


class ReadForRunFailed(Message):
    """A job file that cannot be run, and why."""

    def __init__(self, path: Path, error: str) -> None:
        super().__init__()
        self.path = path
        self.error = error


class DrawThingsApp(App[None]):
    """Browse the jobs in a data directory and their history, and run one job at a time; browsing writes nothing."""

    TITLE = "Draw Things Control"
    CSS_PATH = "styles.tcss"
    # Commands are typed on the command line; the palette would be a second way, and a way to change the theme.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        # Before any widget can take the key: Textual's own ctrl+c only explains how to quit, and Input's copies.
        Binding("ctrl+c", "interrupt", "Quit", show=False, priority=True),
    ]

    def __init__(self, *, settings: GlobalConfig, data_directory: Path, executable: str, job_service: JobService, shutdown_grace: float = 10.0) -> None:
        super().__init__()
        self.settings = settings
        self.data_directory = data_directory
        self.executable = executable
        self.job_service = job_service
        self.shutdown_grace = shutdown_grace
        # The running job, or the last one started in this session.
        self.live: LiveRun | None = None
        # The signal a stop was requested with; the worker reads it, so a stop before the job has begun still reaches it.
        self.stop_signal: signal.Signals | None = None
        self.quit_when_stopped = False
        self.exit_signal: signal.Signals | None = None
        self._previous_handlers: dict[signal.Signals, object] = {}
        self._unmounted = False
        # Records the running job; set on the worker thread, read on the main thread once JobStarted arrives.
        self._recorder: ExecutionRecorder | None = None
        # Monotonic time a first Ctrl-C stops waiting for the second.
        self._quit_armed_until = 0.0
        self.theme = "textual-dark"
        # Set by the worker thread as its last step; with _unmounted, decides who hands the signal handlers back.
        self._worker_done = threading.Event()

    @property
    def job_running(self) -> bool:
        """From confirmation until the worker has released the lock, not until JobFinished."""
        return self.live is not None and not self.live.worker_ended

    def on_mount(self) -> None:
        self.install_signal_handlers()
        self.push_screen(MainScreen())

    def on_unmount(self) -> None:
        self._unmounted = True
        if not self.job_running or self._worker_done.is_set():
            self.remove_signal_handlers()
            return
        # However the app ends, asyncio then waits for the job's thread: stop the job so that wait ends within the shutdown grace.
        # Set first, so a worker that has not reached JobService.run yet cancels on JobStarted.
        if self.stop_signal is None:
            self.stop_signal = signal.SIGINT
        self.job_service.cancel(self.stop_signal)
        # Until the worker ends, a second signal must not kill dtc and leave draw-things-cli running; the job is already stopping.
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            for received in self._previous_handlers:
                loop.add_signal_handler(received, self.ignore_signal, received)

    def ignore_signal(self, received: signal.Signals) -> None:
        """A signal after the app has gone, while its job stops: nothing more to do."""

    def install_signal_handlers(self) -> None:
        """Handle the signals that would otherwise kill dtc and leave draw-things-cli running."""
        try:
            loop = asyncio.get_running_loop()
            for received in HANDLED_SIGNALS:
                self._previous_handlers[received] = signal.getsignal(received)
                loop.add_signal_handler(received, self.handle_signal, received)
        except (NotImplementedError, RuntimeError, ValueError):
            # Not on the main thread, or no signals on this platform: nothing to register.
            pass

    def remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for received, previous in self._previous_handlers.items():
            loop.remove_signal_handler(received)
            if previous is not None:
                signal.signal(received, previous)
        self._previous_handlers.clear()

    def handle_signal(self, received: signal.Signals) -> None:
        """Stop the job as the signal would stop the CLI, then quit; with no job, quit now. dtc tui exits with 128+N.

        A signal that arrives while the job is already stopping changes nothing: dtc tui exits with the code of the signal that stopped it.
        """
        if self.exit_signal is None:
            self.exit_signal = self.stop_signal if self.stop_signal is not None else received
        self.quit_when_stopped = True
        if self.job_running:
            self.request_stop(received)
        else:
            self.exit(return_code=128 + self.exit_signal.value)

    @property
    def main(self) -> MainScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen) and screen.is_mounted:
                return screen
        return None

    def say(self, text: str, style: str = "") -> None:
        """Write a notice to the messages."""
        if self.main is not None:
            self.main.say(text, style)

    @property
    def execution_id(self) -> int | None:
        """The state store's row for the running or last job, or None when it is not recorded."""
        return self._recorder.execution_id if self._recorder is not None else None

    @property
    def quit_armed(self) -> bool:
        """Whether a first Ctrl-C is waiting for the second."""
        return time.monotonic() < self._quit_armed_until

    async def action_interrupt(self) -> None:
        """Ctrl-C: clear the command line if it holds text; otherwise quit on a second press within QUIT_PRESS_SECONDS."""
        if isinstance(self.screen, ConfirmScreen):
            return
        if self.quit_when_stopped:
            self.say("Waiting for the job to stop", "yellow")
            return
        main = self.main
        if main is not None and main.command_line.value:
            main.command_line.action_clear_line()
            return
        if self.quit_armed:
            self._quit_armed_until = 0.0
            self.show_status()
            await self.action_quit()
            return
        self._quit_armed_until = time.monotonic() + QUIT_PRESS_SECONDS
        self.show_status()
        self.set_timer(QUIT_PRESS_SECONDS, self.show_status)

    def show_status(self) -> None:
        if self.main is not None:
            self.main.tick()

    async def action_quit(self) -> None:
        if not self.job_running:
            self.exit()
            return
        if self.quit_when_stopped:
            self.say("Waiting for the job to stop", "yellow")
            return
        if any(isinstance(screen, ConfirmScreen) and screen.purpose == "quit" for screen in self.screen_stack):
            return
        self.push_screen(ConfirmScreen(question_text("A job is running. Stop the job and quit?", "stop the job and quit", "stay"), purpose="quit"), self.confirm_quit)

    def confirm_quit(self, quit_app: bool | None) -> None:
        if not quit_app:
            return
        if not self.job_running:
            self.exit()
            return
        self.quit_when_stopped = True
        self.request_stop()

    def start_flow(self, path: Path) -> None:
        """Read the job again and ask to run it."""
        if self.job_running:
            self.say("A job is already running", "yellow")
            return
        if isinstance(self.screen, ConfirmScreen):
            return
        self.read_for_run(path)

    @work(thread=True, exclusive=True, group="read-for-run")
    def read_for_run(self, path: Path) -> None:
        # Read now, so an edit since the list was read is used; the input is decoded when run 1's copy is written, as run-job does.
        try:
            job = read_job(path, self.settings, decode_input=False)[0]
        except (ValueError, OSError) as error:
            self.post_message(ReadForRunFailed(path, error_text(path, error)))
            return
        self.post_message(ReadForRun(path, job))

    def on_read_for_run_failed(self, message: ReadForRunFailed) -> None:
        self.say(f"Invalid job {message.path.name}: {message.error}", "red")

    def on_read_for_run(self, message: ReadForRun) -> None:
        if self.job_running:
            self.say("A job is already running", "yellow")
            return
        if isinstance(self.screen, ConfirmScreen):
            return
        # The job read for the dialog is the one that runs.
        self.push_screen(ConfirmScreen(confirm_run_text(message.job, self.executable), purpose="run", enter_confirms=False), lambda run: self.start_job(message.path, message.job) if run else None)

    def start_job(self, path: Path, job: JobDefinition) -> None:
        if self.job_running:
            self.say("A job is already running", "yellow")
            return
        self.live = LiveRun(job, path)
        self._recorder = None
        self.stop_signal = None
        self._worker_done.clear()
        self.say(f"Starting {path.name}")
        if self.main is not None:
            self.main.job_started()
        self.run_job(job)

    def request_stop(self, received: signal.Signals = signal.SIGINT) -> None:
        """Stop the running job as ``received`` would stop the CLI; a second request sends nothing."""
        if not self.job_running or self.live is None or self.live.stop_requested:
            return
        self.stop_signal = received
        self.live.request_stop()
        self.job_service.cancel(received)
        self.say(f"Stopping the job ({received.name})", "yellow")
        if self.main is not None:
            self.main.render_live()

    @work(thread=True, group="job")
    def run_job(self, job: JobDefinition) -> None:
        """Run the job as run-job does; never raises, since a failed worker closes the app."""
        error: str | None = None
        try:
            self.execute(job)
        except Exception as exception:
            error = str(exception) or type(exception).__name__
        finally:
            self._worker_done.set()
            # Last, after the lock is released, so a job counts as running until the next one can take the lock.
            with contextlib.suppress(RuntimeError):
                self.post_message(JobWorkerEnded(error))
            # If the app has unmounted meanwhile, nothing handles that message; this hands the signals back. A closed loop needs neither.
            with contextlib.suppress(RuntimeError, AttributeError):
                self._loop.call_soon_threadsafe(self.after_worker)  # type: ignore[union-attr]

    def after_worker(self) -> None:
        if self._unmounted:
            self.remove_signal_handlers()

    def execute(self, job: JobDefinition) -> None:
        """Take the lock, sweep, and run the job with its events recorded and posted; runs on the worker thread."""
        lock = RunLock("tui")
        lock.acquire()
        try:
            # The store keeps one connection per thread, so this thread opens and closes its own.
            store = Store(retention_days=self.settings.history_retention_days)
            try:
                try:
                    store.sweep_interrupted()
                except sqlite3.Error as error:
                    raise StateError(f"Cannot use the state database {store.path}: {error}") from error
                # The recorder comes first, so its row exists before JobStarted is posted.
                self._recorder = ExecutionRecorder(store)
                observer = combine_observers(self._recorder, self.post_event, self.stop_if_requested)
                self.job_service.run(job, executable=self.executable, shutdown_grace=self.shutdown_grace, write_records=self.settings.write_job_records, observer=observer, on_child_start=lock.record_child)
            finally:
                store.close()
        finally:
            lock.release()

    def post_event(self, event: JobEvent) -> None:
        # Thread-safe and non-blocking; returns False once the app is closing.
        self.post_message(JobEventMessage(event))

    def stop_if_requested(self, event: JobEvent) -> None:
        """A stop requested before the job had begun found nothing to cancel; cancel it now, before run 1."""
        if isinstance(event, JobStarted) and self.stop_signal is not None:
            self.job_service.cancel(self.stop_signal)

    def on_job_event_message(self, message: JobEventMessage) -> None:
        if self.live is not None:
            self.live.apply(message.event)
            if self.main is not None:
                self.main.job_event(message.event)

    def on_job_worker_ended(self, message: JobWorkerEnded) -> None:
        if self.live is not None:
            self.live.end(message.error)
        self.stop_signal = None
        if self.main is not None:
            self.main.job_ended()
        if self.quit_when_stopped:
            self.exit(return_code=128 + self.exit_signal.value if self.exit_signal is not None else 0)
