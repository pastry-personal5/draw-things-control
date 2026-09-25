"""Run the TUI headless in its own process with a fake run that blocks, for the SIGTERM test; never draw-things-cli.

Usage: python -m tests.tui.sigterm_app ROOT STARTED_FILE. ROOT is a JobTestCase root with data/walk.yaml; STARTED_FILE is created once run 1 is running.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest import mock

from loguru import logger

from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.screens import ConfirmScreen, MainScreen
from draw_things_control.tui.widgets import CommandInput
from tests.tui.fake_runs import FakeRuns


def main() -> int:
    root, started = Path(sys.argv[1]), Path(sys.argv[2])
    logger.remove()
    runs = FakeRuns(block=True)
    threading.Thread(target=lambda: runs.started.wait(30) and started.touch(), daemon=True).start()
    app = DrawThingsApp(settings=GlobalConfig(input_directory=root / "input", output_directory=root / "output"), data_directory=root / "data", executable="draw-things-cli", job_service=runs.service)

    async def start_job(pilot) -> None:
        while not isinstance(app.screen, MainScreen):
            await asyncio.sleep(0.02)
        app.screen.query_one(CommandInput).value = "/run walk"
        await pilot.press("enter")
        while not isinstance(app.screen, ConfirmScreen):
            await asyncio.sleep(0.02)
        await pilot.press("y")
        while app.live is None:
            await asyncio.sleep(0.02)

    with mock.patch("draw_things_control.core.generation_config.DT_CONFIG_DIRECTORY", root / "dt-config"), mock.patch("draw_things_control.core.run_lock.STATE_DIRECTORY", root / "state"):
        app.run(headless=True, auto_pilot=start_job)
    return app.return_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
