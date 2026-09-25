"""The Textual application: its collaborators, global keys, and first screen."""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_service import JobService
from draw_things_control.tui.screens import HelpScreen, JobDetails, JobListScreen


class DrawThingsApp(App[None]):
    """Browse the jobs in a data directory; reads files, never writes them."""

    TITLE = "Draw Things Control"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        # Textual's own ctrl+c only explains how to quit; here it quits, before any widget can take the key.
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, *, settings: GlobalConfig, data_directory: Path, executable: str, job_service: JobService) -> None:
        super().__init__()
        self.settings = settings
        self.data_directory = data_directory
        self.executable = executable
        self.job_service = job_service
        # Each opened job's details and plan, by file; cleared by the refresh key.
        self.details: dict[Path, JobDetails] = {}

    def on_mount(self) -> None:
        self.push_screen(JobListScreen())

    def action_help(self) -> None:
        # The help screen closes itself on ? and Escape.
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())
