"""Headless tests of running a job from the TUI, with fake runners, a temporary state directory, and no real draw-things-cli."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from loguru import logger
from textual.widgets import RichLog, Static

from draw_things_control.core import run_lock
from draw_things_control.core.generation_service import GenerationService
from draw_things_control.core.global_config import PROJECT_ROOT, GlobalConfig
from draw_things_control.core.run_lock import RunLock, run_lock_is_free
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_events import RunStarted
from draw_things_control.state.store import Store
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, JobEventMessage, LiveRun
from draw_things_control.tui.screens import ConfirmScreen, JobListScreen, LiveRunScreen
from draw_things_control.tui.widgets import JobTable
from tests.fixtures import JobTestCase, job_data
from tests.tui.fake_runs import FakeRuns

TWO_RUNS = {"run_count": 2, "prompt_pairs": [{"name": "walk", "positive": "walk", "runs": [1, 2]}], "cooldown_seconds": 0}


class LiveRunTests(JobTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.root / "data"
        self.data.mkdir()
        self.state = self.root / "state"
        for target, value in (("draw_things_control.core.generation_config.DT_CONFIG_DIRECTORY", self.dt_config), ("draw_things_control.core.run_lock.STATE_DIRECTORY", self.state)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # As dtc tui leaves it: no sink writes to the terminal while the app runs.
        logger.remove()
        self.addCleanup(logger.add, sys.stderr)

    def write_data_job(self, name: str = "walk.yaml", **changes: object) -> Path:
        self.write_job(job_data(**{**TWO_RUNS, **changes}), name=f"data/{name}")
        return self.data / name

    def app(self, runs: FakeRuns, settings: GlobalConfig | None = None) -> DrawThingsApp:
        return DrawThingsApp(settings=settings or self.global_config, data_directory=self.data, executable="draw-things-cli", job_service=runs.service, shutdown_grace=3)

    async def wait_for(self, pilot: Any, condition: Callable[[], object], what: str, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.02)

    async def start(self, pilot: Any, key: str = "x") -> None:
        """Wait for the list, run its selected job, and confirm."""
        app = pilot.app
        await self.wait_for(pilot, lambda: isinstance(app.screen, JobListScreen) and app.screen.query_one(JobTable).row_count > 0, "the job list")
        await pilot.press(key)
        await self.wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the run confirmation")
        await pilot.press("y")
        await self.wait_for(pilot, lambda: isinstance(app.screen, LiveRunScreen), "the live view")

    async def finish(self, pilot: Any) -> None:
        await self.wait_for(pilot, lambda: not pilot.app.job_running, "the job worker to end")
        await pilot.pause()

    @staticmethod
    def text(app: DrawThingsApp, widget_id: str) -> str:
        return str(app.screen.query_one(f"#{widget_id}", Static).content)

    @staticmethod
    def pane(app: DrawThingsApp) -> list[str]:
        return [strip.text.rstrip() for strip in app.screen.query_one(RichLog).lines]

    def executions(self) -> list[dict[str, Any]]:
        store = Store()
        try:
            return [store.get_execution(row["id"]) for row in store.list_executions()]  # type: ignore[misc]
        finally:
            store.close()

    async def test_a_job_runs_with_progress_and_ends_with_its_result(self) -> None:
        self.write_data_job()
        gate = threading.Event()
        runs = FakeRuns(gate=gate)
        app = self.app(runs)
        applied: list[str] = []
        original = LiveRun.apply

        def spy(live: LiveRun, event: Any) -> None:
            original(live, event)
            applied.append(type(event).__name__ + (f" {event.status}" if hasattr(event, "status") else ""))

        with mock.patch.object(LiveRun, "apply", spy):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.start(pilot)
                await self.wait_for(pilot, lambda: app.live is not None and app.live.progress == (4, 8), "run 1's progress")
                await pilot.pause()
                header, active = self.text(app, "live-header"), self.text(app, "live-active")
                table = app.screen.query_one("#live-runs")
                statuses = [str(table.get_row_at(index)[2]) for index in range(table.row_count)]
                gate.set()
                await self.finish(pilot)
                result, pane = self.text(app, "live-result"), self.pane(app)
        self.assertIn("sunset-walk  i2v  seed 42 (config_file)  running", header)
        self.assertIn("Run 1/2", active)
        self.assertIn("progress: 4/8, 50%", active)
        self.assertIn("draw-things-cli generate", active)
        self.assertEqual(statuses, ["running", "pending"])
        self.assertEqual(applied[0], "JobStarted")
        self.assertEqual([name for name in applied if not name.startswith("RunOutput")], ["JobStarted", "RunStarted", "RunFinished succeeded", "RunStarted", "RunFinished succeeded", "JobFinished succeeded"])
        self.assertIn("Job succeeded: 2/2 runs completed, exit code 0", result)
        self.assertIn("warning: [cache] missing [/bold]", pane)
        self.assertEqual(pane.count("Loading model base.ckpt"), 2)
        self.assertFalse(any("Sampling" in line for line in pane), pane)
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"], execution["seed"]), ("succeeded", 0, 42))
        self.assertEqual([run["status"] for run in execution["runs"]], ["succeeded", "succeeded"])
        self.assertTrue(run_lock_is_free())
        self.assertEqual([runner.grace for runner in runs.runners], [3, 3])

    async def test_the_confirmation_shows_the_job_and_n_cancels(self) -> None:
        self.write_data_job(cooldown_seconds=30)
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.wait_for(pilot, lambda: app.screen.query_one(JobTable).row_count > 0, "the job list")
            await pilot.press("x")
            await self.wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the run confirmation")
            confirm = self.text(app, "confirm")
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, JobListScreen)
        self.assertIn("Job: sunset-walk", confirm)
        self.assertIn("Mode: i2v", confirm)
        self.assertIn("Runs: 2", confirm)
        self.assertIn("Cooldown: 30 s between runs, from job (1 wait, 30 s total)", confirm)
        self.assertIn("Seed: 42 (config_file)", confirm)
        self.assertIn(f"Output directory: {self.output_directory}", confirm)
        self.assertIn("Executable: draw-things-cli", confirm)
        self.assertEqual(runs.runners, [])
        self.assertFalse((self.state / "run.lock").exists())

    async def test_the_job_is_read_again_when_x_is_pressed(self) -> None:
        path = self.write_data_job()
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.wait_for(pilot, lambda: app.screen.query_one(JobTable).row_count > 0, "the job list")
            path.write_text("name: [broken\n", encoding="utf-8")
            with mock.patch.object(app, "notify", wraps=app.notify) as notify:
                await pilot.press("x")
                await self.wait_for(pilot, lambda: notify.called, "the error")
            self.assertIsInstance(app.screen, JobListScreen)
        self.assertEqual(notify.call_args.kwargs["severity"], "error")
        self.assertIn("walk.yaml", notify.call_args.args[0])
        self.assertEqual(runs.runners, [])

    async def test_s_stops_a_run_once(self) -> None:
        self.write_data_job()
        # The gate holds the run after the stop, so "Stopping..." can be seen.
        gate = threading.Event()
        runs = FakeRuns(block=True, gate=gate)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            with mock.patch.object(runs.service, "cancel", wraps=runs.service.cancel) as cancel:
                await pilot.press("s")
                self.assertIsInstance(app.screen, ConfirmScreen)
                await pilot.press("y")
                await pilot.pause()
                self.assertIn("Stopping...", self.text(app, "live-header"))
                with mock.patch.object(app, "notify", wraps=app.notify) as notify:
                    await pilot.press("s")
                self.assertEqual([call.args[0] for call in notify.call_args_list], ["Already stopping"])
                self.assertIsInstance(app.screen, LiveRunScreen)
                gate.set()
                await self.finish(pilot)
                await pilot.press("s")
                self.assertIsInstance(app.screen, LiveRunScreen)
            result = self.text(app, "live-result")
        cancel.assert_called_once_with(signal.SIGINT)
        self.assertIn("Job interrupted: 0/2 runs completed, exit code 130, stopped by SIGINT", result)
        self.assertEqual(len(runs.runners), 1)
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"]), ("interrupted", 130))
        self.assertEqual([run["status"] for run in execution["runs"]], ["interrupted"])
        self.assertTrue(run_lock_is_free())

    async def test_s_during_a_cooldown_ends_it_at_once(self) -> None:
        self.write_data_job(cooldown_seconds=600)
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, lambda: app.live is not None and app.live.cooldown is not None, "the cooldown")
            await pilot.pause()
            cooldown, header = self.text(app, "live-cooldown"), self.text(app, "live-header")
            started = time.monotonic()
            await pilot.press("s", "y")
            await self.finish(pilot)
            elapsed = time.monotonic() - started
            result = self.text(app, "live-result")
        self.assertIn("cooling down", header)
        self.assertRegex(cooldown, r"Cooldown before run 2: (10 min|9 min 59 s) left, until \d\d:\d\d:\d\d")
        self.assertLess(elapsed, 5)
        self.assertIn("Job interrupted: 1/2 runs completed, exit code 130", result)
        self.assertEqual(len(runs.runners), 1)
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"]), ("interrupted", 130))

    async def test_a_busy_lock_starts_nothing_and_the_next_x_runs(self) -> None:
        self.write_data_job()
        self.state.mkdir()
        other = RunLock("run-job")
        other.acquire()
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.finish(pilot)
            result, header = self.text(app, "live-result"), self.text(app, "live-header")
            other.release()
            await pilot.press("escape")
            await self.start(pilot)
            await self.finish(pilot)
            second = self.text(app, "live-result")
        self.assertIn(f"Did not start: Another run is in progress (run-job, PID {os.getpid()}). Try again when it finishes.", result)
        self.assertIn("did not start", header)
        self.assertIn("Job succeeded", second)
        self.assertEqual(len(runs.runners), 2)
        self.assertEqual(len(self.executions()), 1)
        self.assertTrue(run_lock_is_free())

    async def test_a_job_that_fails_before_it_starts_records_nothing(self) -> None:
        self.write_data_job()
        runs = FakeRuns(missing=frozenset({"draw-things-cli"}))
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.finish(pilot)
            result = self.text(app, "live-result")
        self.assertIn("Did not start: Could not find 'draw-things-cli' on PATH", result)
        self.assertEqual(self.executions(), [])
        self.assertTrue(run_lock_is_free())
        self.assertFalse(self.output_directory.exists())

    async def test_x_during_a_run_and_the_list_status(self) -> None:
        self.write_data_job()
        self.write_data_job("z-other.yaml")
        runs = FakeRuns(block=True)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, JobListScreen)
            table = app.screen.query_one(JobTable)
            statuses = {str(table.get_row_at(index)[0]): str(table.get_row_at(index)[4]) for index in range(table.row_count)}
            with mock.patch.object(app, "notify", wraps=app.notify) as notify:
                await pilot.press("x")
                await pilot.pause()
            self.assertIsInstance(app.screen, JobListScreen)
            app.request_stop()
            await self.finish(pilot)
            after = str(table.get_row_at(0)[4])
        self.assertEqual(statuses, {"walk.yaml": "running", "z-other.yaml": "valid"})
        notify.assert_called_once_with("A job is already running", severity="warning")
        self.assertEqual(after, "valid")

    async def test_escape_and_l_keep_the_state_and_output(self) -> None:
        self.write_data_job()
        gate = threading.Event()
        runs = FakeRuns(gate=gate)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.wait_for(pilot, lambda: app.screen.query_one(JobTable).row_count > 0, "the job list")
            with mock.patch.object(app, "notify", wraps=app.notify) as notify:
                await pilot.press("l")
            notify.assert_called_once_with("No job has run in this session")
            # Started from the detail view, the live view still goes back to the list.
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("x")
            await self.wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the run confirmation")
            await pilot.press("y")
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            await pilot.pause()
            before = (self.text(app, "live-header"), self.pane(app))
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, JobListScreen)
            await pilot.press("l")
            await pilot.pause()
            self.assertIsInstance(app.screen, LiveRunScreen)
            again = (self.text(app, "live-header"), self.pane(app))
            await pilot.press("escape")
            gate.set()
            await self.finish(pilot)
            await pilot.press("l")
            await pilot.pause()
            final, pane = self.text(app, "live-result"), self.pane(app)
        self.assertEqual(before, again)
        self.assertIn("Loading model base.ckpt", before[1])
        self.assertIn("Job succeeded: 2/2 runs completed", final)
        self.assertEqual(pane.count("Loading model base.ckpt"), 2)

    async def test_the_output_pane_keeps_the_last_lines_as_written(self) -> None:
        from draw_things_control.core.process_output import OutputStream

        self.write_data_job(run_count=1, prompt_pairs=[{"name": "walk", "positive": "walk"}])
        lines = tuple((OutputStream.STDOUT, f"line {number} [x]") for number in range(MAX_OUTPUT_LINES + 150)) + tuple((OutputStream.STDOUT, f"{step}/8 {step * 12}%") for step in range(1, 9))
        runs = FakeRuns(lines=lines)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.finish(pilot)
            pane = self.pane(app)
            model = list(app.live.output)  # type: ignore[union-attr]
        self.assertLessEqual(len(pane), MAX_OUTPUT_LINES)
        self.assertEqual(len(model), MAX_OUTPUT_LINES)
        self.assertEqual(pane[-1], f"line {MAX_OUTPUT_LINES + 149} [x]")
        self.assertNotIn("line 0 [x]", pane)
        self.assertFalse(any("/8" in line for line in pane))

    async def test_q_and_ctrl_c_during_a_run_offer_to_stop_it(self) -> None:
        self.write_data_job()
        for key in ("q", "ctrl+c"):
            with self.subTest(key=key):
                gate = threading.Event()
                runs = FakeRuns(block=True, gate=gate)
                app = self.app(runs)
                async with app.run_test(size=(160, 60)) as pilot:
                    await self.start(pilot)
                    await self.wait_for(pilot, runs.started.is_set, "run 1")
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertEqual(sum(isinstance(screen, ConfirmScreen) for screen in app.screen_stack), 1)
                    await pilot.press("n")
                    await pilot.pause()
                    self.assertTrue(app.job_running)
                    await pilot.press(key, "y")
                    with mock.patch.object(app, "notify", wraps=app.notify) as notify:
                        await pilot.press(key)
                    notify.assert_called_once_with("Waiting for the job to stop")
                    self.assertTrue(app.is_running)
                    gate.set()
                    await self.wait_for(pilot, lambda: not pilot.app.is_running, "the app to exit")
                self.assertEqual(app.return_code, 0)
                self.assertTrue(run_lock_is_free())
                self.assertEqual(self.executions()[0]["exit_code"], 130)

    async def test_a_signal_stops_the_job_then_quits_with_128_plus_n(self) -> None:
        self.write_data_job()
        runs = FakeRuns(block=True)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            app.handle_signal(signal.SIGTERM)
            await self.wait_for(pilot, lambda: not pilot.app.is_running, "the app to exit")
        self.assertEqual(app.return_code, 143)
        self.assertEqual(runs.runners[0].shutdown_signal, signal.SIGTERM)
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"], execution["signal"]), ("interrupted", 143, "SIGTERM"))
        self.assertTrue(run_lock_is_free())

    async def test_a_signal_without_a_job_quits_at_once(self) -> None:
        for received in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=received.name):
                app = self.app(FakeRuns())
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.handle_signal(received)
                    await self.wait_for(pilot, lambda: not pilot.app.is_running, "the app to exit")
                self.assertEqual(app.return_code, 128 + received.value)

    async def test_the_signal_handlers_are_registered_while_the_app_runs(self) -> None:
        before = {received: signal.getsignal(received) for received in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
        app = self.app(FakeRuns())
        with mock.patch.object(asyncio.get_running_loop(), "add_signal_handler", wraps=asyncio.get_running_loop().add_signal_handler) as add:
            async with app.run_test() as pilot:
                await pilot.pause()
        self.assertEqual({call.args[0] for call in add.call_args_list}, {signal.SIGHUP, signal.SIGTERM, signal.SIGINT})
        self.assertEqual({received: signal.getsignal(received) for received in before}, before)

    async def test_an_app_that_ends_during_a_run_stops_the_job(self) -> None:
        self.write_data_job()
        runs = FakeRuns(block=True)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            # No dialog: as if the app ended on an error.
            app.exit(return_code=1)
            await pilot.pause()
        self.assertTrue(runs.runners[0].stopped.wait(5))
        self.assertEqual(runs.runners[0].shutdown_signal, signal.SIGINT)
        deadline = time.monotonic() + 5
        while not run_lock_is_free() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertTrue(run_lock_is_free())

    async def test_an_app_that_ends_before_the_job_has_begun_still_stops_it(self) -> None:
        self.write_data_job()
        runs = FakeRuns()
        opened, release = threading.Event(), threading.Event()
        sweep = Store.sweep_interrupted

        def slow_sweep(store: Store) -> int:
            opened.set()
            assert release.wait(10)
            return sweep(store)

        app = self.app(runs)
        with mock.patch.object(Store, "sweep_interrupted", slow_sweep):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.start(pilot)
                await self.wait_for(pilot, opened.is_set, "the worker to open the store")
                # JobService.run has not begun, so there is no job to cancel yet.
                app.exit(return_code=1)
                await pilot.pause()
            release.set()
            deadline = time.monotonic() + 5
            while not app._worker_done.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        self.assertTrue(run_lock_is_free())
        self.assertEqual(runs.runners, [])
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"]), ("interrupted", 130))

    async def test_signals_stay_handled_until_the_worker_ends_after_the_app_has_gone(self) -> None:
        self.write_data_job()
        gate = threading.Event()
        runs = FakeRuns(block=True, gate=gate)
        app = self.app(runs)
        before = signal.getsignal(signal.SIGTERM)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            app.exit(return_code=1)
            await pilot.pause()
        # The job is stopping but held by the gate: a second signal must not kill the process.
        self.assertIsNot(signal.getsignal(signal.SIGTERM), before)
        app.ignore_signal(signal.SIGTERM)
        gate.set()
        deadline = time.monotonic() + 5
        while signal.getsignal(signal.SIGTERM) is not before and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertTrue(run_lock_is_free())

    async def test_a_signal_while_stopping_keeps_the_stopping_signals_exit_code(self) -> None:
        self.write_data_job()
        gate = threading.Event()
        runs = FakeRuns(block=True, gate=gate)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            await pilot.press("s", "y")
            app.handle_signal(signal.SIGTERM)
            gate.set()
            await self.wait_for(pilot, lambda: not pilot.app.is_running, "the app to exit")
        self.assertEqual(app.return_code, 130)
        self.assertEqual(self.executions()[0]["signal"], "SIGINT")

    async def test_the_tables_update_in_place(self) -> None:
        self.write_data_job(run_count=20, prompt_pairs=[{"name": "walk", "positive": "walk"}])
        runs = FakeRuns()
        app = self.app(runs)
        with mock.patch("draw_things_control.tui.widgets.RunTable.clear") as clear:
            async with app.run_test(size=(160, 60)) as pilot:
                await self.wait_for(pilot, lambda: app.screen.query_one(JobTable).row_count > 0, "the job list")
                jobs = app.screen.query_one(JobTable)
                with mock.patch.object(runs, "gate", threading.Event()):
                    await self.start(pilot)
                    await self.wait_for(pilot, runs.started.is_set, "run 1")
                    await pilot.press("escape")
                    await pilot.pause()
                    status_width = jobs.columns[app.screen.status_column].content_width
                    await pilot.press("l")
                    runs.gate.set()
                    await self.finish(pilot)
                table = app.screen.query_one("#live-runs")
                statuses = {str(table.get_row_at(index)[2]) for index in range(table.row_count)}
        # Clearing the run table would scroll it back to the top on every event.
        clear.assert_not_called()
        self.assertEqual((table.row_count, statuses), (20, {"succeeded"}))
        self.assertGreaterEqual(status_width, len("running"))

    async def test_the_screen_shows_the_redacted_command(self) -> None:
        self.write_data_job()
        runs = FakeRuns()
        app = self.app(runs)
        command = GenerationService.redact_command(["draw-things-cli", "generate", "--api-key", "sekret-value", "--remote-shared-secret", "hush-value"])
        async with app.run_test(size=(160, 60)) as pilot:
            await self.wait_for(pilot, lambda: app.screen.query_one(JobTable).row_count > 0, "the job list")
            app.live = LiveRun(load_job(self.data / "walk.yaml", self.global_config), self.data / "walk.yaml")
            app.open_live()
            await pilot.pause()
            app.post_message(JobEventMessage(RunStarted(at="", number=1, total=2, pair="walk", positive="walk", negative=None, input=None, resized_input=None, output="out.mov", last_frame=None, command=tuple(command))))
            await pilot.pause()
            active = self.text(app, "live-active")
        self.assertIn("--api-key", active)
        self.assertNotIn("sekret-value", active)
        self.assertNotIn("hush-value", active)

    async def test_nothing_reaches_stdout_or_stderr_and_only_run_job_files_are_written(self) -> None:
        self.write_data_job()
        settings = GlobalConfig(input_directory=self.input_directory, output_directory=self.output_directory, write_job_records=True)
        before = set(self.root.rglob("*"))
        runs = FakeRuns()
        app = self.app(runs, settings)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.start(pilot)
                await self.finish(pilot)
                result = self.text(app, "live-result")
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertIn("manifest: ", result)
        self.assertIn("log: ", result)
        written = sorted(path.relative_to(self.root).as_posix() for path in set(self.root.rglob("*")) - before if path.is_file())
        allowed = ("state/dtc.db", "state/run.lock", "output/")
        self.assertTrue(all(name.startswith(allowed) for name in written), written)
        self.assertEqual(sum(name.endswith(".mov") for name in written), 2)
        self.assertEqual(sum(name.endswith(".json") for name in written), 1)
        self.assertEqual(sum(name.endswith(".log") for name in written), 1)


class SigtermTests(JobTestCase):
    def test_sigterm_to_a_running_tui_stops_the_job_and_exits_with_143(self) -> None:
        data = self.root / "data"
        data.mkdir()
        self.write_job(job_data(**TWO_RUNS), name="data/walk.yaml")
        state = self.root / "state"
        started = self.root / "started"
        process = subprocess.Popen([sys.executable, "-m", "tests.tui.sigterm_app", str(self.root), str(started)], cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 30
            while not started.exists():
                if process.poll() is not None:
                    self.fail(f"the app exited early: {process.communicate()[1].decode()}")
                self.assertLess(time.monotonic(), deadline, "the fake run never started")
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            _stdout, stderr = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertEqual(process.returncode, 143, stderr.decode())
        with mock.patch.object(run_lock, "STATE_DIRECTORY", state):
            store = Store()
            try:
                [row] = store.list_executions()
            finally:
                store.close()
            self.assertTrue(run_lock_is_free())
        self.assertEqual((row["status"], row["exit_code"], row["signal"]), ("interrupted", 143, "SIGTERM"))
        self.assertEqual((state / "run.lock").read_text(encoding="utf-8"), "")
