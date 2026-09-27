"""The Textual application: its collaborators, the quit key, its one screen, and the job it runs."""

from __future__ import annotations

import contextlib
import signal
import threading
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.message import Message

from draw_things_control.core.exit_codes import exit_code_for_signal
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import ProjectPaths
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import JobEvent, JobStarted
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.jobs.files import read_job
from draw_things_control.services.job_details import error_text
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import Store
from draw_things_control.tui.confirm import ConfirmScreen
from draw_things_control.tui.live_run import JobEventMessage, JobWorkerEnded, LiveRun, PastRunFound, latest_past_run
from draw_things_control.tui.screens import MainScreen
from draw_things_control.tui.signals import QuitPress, SignalGuard
from draw_things_control.tui.text.jobs import confirm_run_text, question_text

# Stopped as the CLI is: the job ends as interrupted by the signal, and dtc tui exits with 128+N.
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

    def __init__(self, *, settings: GlobalConfig, paths: ProjectPaths, data_directory: Path, executable: str, job_executor: JobExecutor, shutdown_grace: float = 10.0) -> None:
        super().__init__()
        self.settings = settings
        self.paths = paths
        self.data_directory = data_directory
        self.executable = executable
        self.job_executor = job_executor
        self.shutdown_grace = shutdown_grace
        # The running job, or the last one started in this session.
        self.live: LiveRun | None = None
        # The signal a stop was requested with; the worker reads it, so a stop before the job has begun still reaches it.
        self.stop_signal: signal.Signals | None = None
        self.quit_when_stopped = False
        self.exit_signal: signal.Signals | None = None
        self.signals = SignalGuard(self.handle_signal)
        self._unmounted = False
        # Records the running job; set on the worker thread, read on the main thread once JobStarted arrives.
        self._recorder: ExecutionRecorder | None = None
        self.quit_press = QuitPress(QUIT_PRESS_SECONDS)
        self.theme = "textual-dark"
        # Set by the worker thread as its last step; with _unmounted, decides who hands the signal handlers back.
        self._worker_done = threading.Event()

    @property
    def job_running(self) -> bool:
        """From confirmation until the worker has released the lock, not until JobFinished."""
        return self.live is not None and not self.live.worker_ended

    def on_mount(self) -> None:
        self.signals.install()
        self.push_screen(MainScreen())

    def on_unmount(self) -> None:
        self._unmounted = True
        if not self.job_running or self._worker_done.is_set():
            self.signals.remove()
            return
        # However the app ends, asyncio then waits for the job's thread: stop the job so that wait ends within the shutdown grace.
        # Set first, so a worker that has not reached JobExecutor.run yet cancels on JobStarted.
        if self.stop_signal is None:
            self.stop_signal = signal.SIGINT
        self.job_executor.cancel(self.stop_signal)
        # Until the worker ends, a second signal must not kill dtc and leave draw-things-cli running; the job is already stopping.
        self.signals.ignore_until_removed()

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
            self.exit(return_code=exit_code_for_signal(self.exit_signal))

    @property
    def main(self) -> MainScreen | None:
        return next((screen for screen in self.screen_stack if isinstance(screen, MainScreen) and screen.is_mounted), None)

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
        return self.quit_press.armed

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
            job = read_job(path, self.settings, self.paths, decode_input=False)[0]
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
        self.job_executor.cancel(received)
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
            self.signals.remove()

    def execute(self, job: JobDefinition) -> None:
        """Run the job with its events recorded and posted, under the run lock; runs on the worker thread."""
        session = JobRunSession(self.paths, self.job_executor, self.settings)

        def before_run(store: Store, recorder: ExecutionRecorder) -> None:
            # Posted before any event, so the run can be estimated from its first moment; the recorder is known before JobStarted.
            self.post_message(PastRunFound(latest_past_run(store)))
            self._recorder = recorder

        session.run(job, holder="tui", executable=self.executable, shutdown_grace=self.shutdown_grace, observers=(self.post_event, self.stop_if_requested), before_run=before_run)

    def on_past_run_found(self, message: PastRunFound) -> None:
        if self.live is not None:
            self.live.past_run = message.past_run

    def post_event(self, event: JobEvent) -> None:
        # Thread-safe and non-blocking; returns False once the app is closing.
        self.post_message(JobEventMessage(event))

    def stop_if_requested(self, event: JobEvent) -> None:
        """A stop requested before the job had begun found nothing to cancel; cancel it now, before run 1."""
        if isinstance(event, JobStarted) and self.stop_signal is not None:
            self.job_executor.cancel(self.stop_signal)

    def on_job_event_message(self, message: JobEventMessage) -> None:
        if self.live is not None:
            # Read again at every event: the recorder wrote the row before JobStarted was posted, and it says None from the
            # moment the store fails, so the Status widget never names a row that stopped being recorded.
            self.live.execution_id = self._recorder.execution_label if self._recorder is not None else None
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
            self.exit(return_code=exit_code_for_signal(self.exit_signal) if self.exit_signal is not None else 0)
