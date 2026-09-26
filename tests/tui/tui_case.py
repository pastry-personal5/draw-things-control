"""A base for headless TUI tests: temporary data, params, and state directories, typed commands, and the messages as written."""

from __future__ import annotations

import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from loguru import logger
from rich.text import Text
from textual.widgets import RichLog, Static

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.job_service import JobService
from draw_things_control.state.store import Store
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.panes import HistoryPane
from draw_things_control.tui.widgets import CommandInput, MessageLog
from tests.fixtures import JobTestCase, job_data


class TuiTestCase(JobTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.root / "data"
        self.data.mkdir()
        self.state = self.root / "state"
        for target, value in (("draw_things_control.core.generation_config.PARAMS_DIRECTORY", self.params), ("draw_things_control.core.run_lock.STATE_DIRECTORY", self.state)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # As dtc tui leaves it: no sink writes to the terminal while the app runs.
        logger.remove()
        self.addCleanup(logger.add, sys.stderr)
        # Every message as plain text, in order, across apps.
        self.said: list[str] = []
        original = MessageLog.say

        def say(log: MessageLog, text: Text | str, style: str = "") -> None:
            self.said.append(str(text))
            original(log, text, style)

        patcher = mock.patch.object(MessageLog, "say", say)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_data_job(self, name: str, **changes: object) -> Path:
        self.write_job(job_data(**changes), name=f"data/{name}")
        return self.data / name

    def make_app(self, service: JobService, *, data: Path | None = None, executable: str = "draw-things-cli", settings: GlobalConfig | None = None, shutdown_grace: float = 10.0) -> DrawThingsApp:
        return DrawThingsApp(settings=settings or self.global_config, data_directory=data or self.data, executable=executable, job_service=service, shutdown_grace=shutdown_grace)

    async def wait_for(self, pilot: Any, condition: Callable[[], object], what: str, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.02)

    @staticmethod
    async def settle(pilot: Any) -> None:
        """Wait for the workers that read jobs and history; never for the job itself, which may be blocked on purpose."""
        for _ in range(2):
            readers = [worker for worker in pilot.app.workers if worker.group != "job"]
            # An empty list would mean every worker to wait_for_complete.
            if readers:
                await pilot.app.workers.wait_for_complete(readers)
            await pilot.pause()

    async def command(self, pilot: Any, line: str, *, settle: bool = True) -> None:
        """Type ``line`` on the command line and press Enter."""
        field = pilot.app.screen.query_one(CommandInput)
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
    def text(app: DrawThingsApp, widget_id: str) -> str:
        return str(app.screen.query_one(f"#{widget_id}", Static).content)

    @staticmethod
    def pane(app: DrawThingsApp) -> list[str]:
        return [strip.text.rstrip() for strip in app.screen.query_one("#cli-output", RichLog).lines]

    @staticmethod
    def history(app: DrawThingsApp) -> list[list[str]]:
        table = app.screen.query_one(HistoryPane)
        return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]

    def executions(self) -> list[dict[str, Any]]:
        store = Store()
        try:
            return [store.get_execution(row["id"]) for row in store.list_executions()]  # type: ignore[misc]
        finally:
            store.close()

    def snapshot(self) -> dict[Path, tuple[int, int]]:
        return {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in self.root.rglob("*")}
