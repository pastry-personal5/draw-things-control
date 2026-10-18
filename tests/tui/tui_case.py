"""A base for headless TUI tests: temporary data, params, and state directories, typed commands, and the messages as written."""

from __future__ import annotations

import asyncio
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from loguru import logger
from rich.text import Text
from textual.widgets import RichLog, Static
from textual.worker import WorkerCancelled
from watchdog.observers.polling import PollingObserver

from framecontrol.core.global_config import GlobalConfig
from framecontrol.jobs.executor import JobExecutor
from framecontrol.state.executions import ExecutionRow
from framecontrol.state.store import Store
from framecontrol.tui.app import FramecontrolApp
from framecontrol.tui.panes.history import ExecutionHistoryList
from framecontrol.tui.widgets import ActivityLog, CommandField
from tests.fixtures import FakeToolkit, JobTestCase, job_data, to_version_2
from tests.tui.fake_server import TOKEN, FakeServer

SERVER_URL = "http://127.0.0.1:8765"


class TuiTestCase(JobTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Textual's headless tests legitimately schedule substantial redraws.
        # Keep asyncio's slow-callback diagnostic useful without printing a
        # warning for ordinary UI work on a loaded CI machine.
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def setUp(self) -> None:
        super().setUp()
        # Not the project's data/jobs/, whose files get job IDs (and so make the state database); a test about IDs uses it.
        self.data = self.root / "jobs-elsewhere"
        self.data.mkdir()
        self.state = self.paths.state
        # A fake fc serve, and the token file the app reads before it connects to it (nothing is ever reached over a network).
        self.server = FakeServer()
        self.paths.server_token.parent.mkdir(parents=True, exist_ok=True)
        self.paths.server_token.write_text(TOKEN, encoding="ascii")
        # As fc tui leaves it: no sink writes to the terminal while the app runs.
        logger.remove()
        # Every message as plain text, in order, across apps.
        self.said: list[str] = []
        # The desktop notifications the apps posted: never a real osascript.
        self.notifications: list[tuple[str, str]] = []
        original = ActivityLog.say

        def say(log: ActivityLog, text: Text | str, style: str = "", *, block: bool = False) -> None:
            # As Messages shows it: its own trailing newlines give way to the blank lines Messages adds.
            self.said.append(str(text).rstrip())
            original(log, text, style, block=block)

        patcher = mock.patch.object(ActivityLog, "say", say)
        patcher.start()
        self.addCleanup(patcher.stop)
        observer_patcher = mock.patch("framecontrol.tui.job_watch.Observer", PollingObserver)
        observer_patcher.start()
        self.addCleanup(observer_patcher.stop)

    def write_data_job(self, name: str, **changes: object) -> Path:
        path = self.data / name
        path.write_text(yaml.safe_dump(to_version_2(job_data(**changes), self.paths.params), sort_keys=False), encoding="utf-8")
        return path

    def make_app(self, service: JobExecutor | None = None, *, data: Path | None = None, settings: GlobalConfig | None = None) -> FramecontrolApp:
        """The app, talking only to ``self.server``; ``service`` is the executor the Job Definition widget's dry-run plan builds."""
        return FramecontrolApp(settings=settings or self.global_config, paths=self.paths, data_directory=data or self.data, server_url=SERVER_URL, token_file=self.paths.server_token, toolkit=FakeToolkit(service), http_transport=self.server.transport(), grpc_stub_factory=self.server.stub_factory, desktop_notifier=lambda title, message: self.notifications.append((title, message)))

    async def wait_for(self, pilot: Any, condition: Callable[[], object], what: str, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.02)

    @staticmethod
    async def settle(pilot: Any) -> None:
        """Wait for the workers that read jobs and history; never for the feed, which reads the server for as long as the app runs."""
        for _ in range(2):
            readers = [worker for worker in pilot.app.workers if worker.group != "feed"]
            # An empty list would mean every worker to wait_for_complete.
            if readers:
                try:
                    await pilot.app.workers.wait_for_complete(readers)
                except WorkerCancelled:
                    # A poll replaced a reader before it finished (a pane that polls every 0.1 s in a test): there is nothing
                    # left to wait for in the one cancelled, and the next pass waits for the one that replaced it.
                    pass
            await pilot.pause()

    async def command(self, pilot: Any, line: str, *, settle: bool = True) -> None:
        """Type ``line`` on the command line and press Enter."""
        field = pilot.app.screen.query_one(CommandField)
        field.focus()
        field.value = line
        await pilot.press("enter")
        if settle:
            await self.settle(pilot)

    def since(self, line: str) -> list[str]:
        """The messages after the last echo of ``line``."""
        echo = f"> {line}"
        index = len(self.said) - 1 - self.said[::-1].index(echo)
        return self.said[index + 1 :]

    @staticmethod
    def text(app: FramecontrolApp, widget_id: str) -> str:
        return str(app.screen.query_one(f"#{widget_id}", Static).content)

    @staticmethod
    def pane(app: FramecontrolApp) -> list[str]:
        return [strip.text.rstrip() for strip in app.screen.query_one("#output-log", RichLog).lines]

    @staticmethod
    def history(app: FramecontrolApp) -> list[list[str]]:
        table = app.screen.query_one(ExecutionHistoryList)
        return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]

    @staticmethod
    def marks_of(app: FramecontrolApp) -> list[str]:
        """The IDs of the history's marked rows, in order."""
        table = app.screen.query_one(ExecutionHistoryList)
        return [str(table.get_row_at(index)[0]).removeprefix("*") for index in range(table.row_count) if str(table.get_row_at(index)[0]).startswith("*")]

    def executions(self) -> list[ExecutionRow]:
        store = Store.open(self.paths.database)
        try:
            rows = (store.executions.get(row.id) for row in store.executions.page())
            return [row for row in rows if row is not None]
        finally:
            store.close()

    def snapshot(self) -> dict[Path, tuple[int, int]]:
        """Every file below the root with its modified time and size, except the TUI's own command history, which typing a command keeps
        (a directory is left out too: its time and size change when that file appears, and a new or changed file is found by itself)."""
        return {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in self.root.rglob("*") if path != self.paths.tui_command_history and not path.is_dir()}
