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
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.core import run_lock
from draw_things_control.core.generation_service import GenerationService
from draw_things_control.core.global_config import PROJECT_ROOT, GlobalConfig
from draw_things_control.core.run_lock import RunLock, run_lock_is_free
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_events import RunStarted
from draw_things_control.state.store import Store
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.history import HistoryReader
from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, JobEventMessage, LiveRun
from draw_things_control.tui.panes import HistoryPane
from draw_things_control.tui.screens import ConfirmScreen, MainScreen
from draw_things_control.tui.widgets import CommandInput
from tests.fixtures import JobTestCase, job_data
from tests.tui.fake_runs import FakeRuns
from tests.tui.tui_case import TuiTestCase

TWO_RUNS = {"run_count": 2, "prompt_pairs": [{"name": "walk", "positive": "walk", "runs": [1, 2]}], "cooldown_seconds": 0}


class LiveRunTests(TuiTestCase):
    def write_data_job(self, name: str = "walk.yaml", **changes: object) -> Path:
        return super().write_data_job(name, **{**TWO_RUNS, **changes})

    def app(self, runs: FakeRuns, settings: GlobalConfig | None = None) -> DrawThingsApp:
        return self.make_app(runs.service, settings=settings, shutdown_grace=3)

    async def start(self, pilot: Any, name: str = "walk") -> None:
        """Run the job and confirm."""
        app = pilot.app
        await self.command(pilot, f"/run {name}", settle=False)
        await self.wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the run confirmation")
        await pilot.press("y")
        await self.wait_for(pilot, lambda: app.live is not None, "the job to start")

    async def finish(self, pilot: Any) -> None:
        await self.wait_for(pilot, lambda: not pilot.app.job_running, "the job worker to end")
        await pilot.pause()

    def log(self) -> str:
        return "\n".join(self.said)

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
                status, line = self.text(app, "run-line"), self.text(app, "status-line")
                await self.wait_for(pilot, lambda: self.history(app) and self.history(app)[0][2] == "running", "the running row")
                gate.set()
                await self.finish(pilot)
                await self.settle(pilot)
                pane, history, after = self.pane(app), self.history(app), self.text(app, "status-line")
        self.assertIn("Run 1/2", status)
        self.assertIn("progress 4/8, 50%", status)
        self.assertIn("walk.yaml running run 1/2", line)
        log = self.log()
        self.assertIn("Job started: sunset-walk (i2v, 2 runs, seed 42 (config_file), model base.ckpt)", log)
        self.assertIn("Run 1/2 started (pair walk)", log)
        self.assertIn("command: draw-things-cli generate", log)
        self.assertIn("Run 2 succeeded in", log)
        self.assertIn("Job succeeded: 2/2 runs completed, exit code 0", log)
        self.assertEqual(applied[0], "JobStarted")
        self.assertEqual([name for name in applied if not name.startswith("RunOutput")], ["JobStarted", "RunStarted", "RunFinished succeeded", "RunStarted", "RunFinished succeeded", "JobFinished succeeded"])
        self.assertIn("warning: [cache] missing [/bold]", pane)
        self.assertEqual(pane.count("Loading model base.ckpt"), 2)
        self.assertFalse(any("Sampling" in line for line in pane), pane)
        self.assertEqual([row[1:3] + row[4:] for row in history], [["sunset-walk", "succeeded", "2/2"]])
        self.assertIn("walk.yaml finished (succeeded)", after)
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
            await self.command(pilot, "/run walk", settle=False)
            await self.wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the run confirmation")
            confirm = self.text(app, "confirm")
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertIsNone(app.live)
        self.assertIn("Job: sunset-walk", confirm)
        self.assertIn("Mode: i2v", confirm)
        self.assertIn("Runs: 2", confirm)
        self.assertIn("Cooldown: 30 s between runs, from job (1 wait, 30 s total)", confirm)
        self.assertIn("Seed: 42 (config_file)", confirm)
        self.assertIn(f"Output directory: {self.output_directory}", confirm)
        self.assertIn("Executable: draw-things-cli", confirm)
        self.assertEqual(runs.runners, [])
        self.assertFalse((self.state / "run.lock").exists())

    async def test_enter_does_not_confirm_a_run(self) -> None:
        self.write_data_job()
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.command(pilot, "/run walk", settle=False)
            await self.wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), "the run confirmation")
            confirm = self.text(app, "confirm")
            # The Enter that submitted /run, pressed again or held, must not start the job.
            await pilot.press("enter", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertIsNone(app.live)
            await pilot.press("y")
            await self.wait_for(pilot, lambda: app.live is not None, "the job to start")
            await self.finish(pilot)
        self.assertIn("y: run    n or Escape: cancel", confirm)
        self.assertNotIn("Enter: run", confirm)
        self.assertEqual(len(runs.runners), 2)

    async def test_stop_and_quit_still_take_enter(self) -> None:
        self.write_data_job()
        runs = FakeRuns(block=True)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            await self.command(pilot, "/stop", settle=False)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("enter")
            await self.finish(pilot)
        self.assertEqual(self.executions()[0]["status"], "interrupted")

    async def test_a_finished_run_updates_its_row_without_reading_the_whole_history(self) -> None:
        self.write_data_job(run_count=3, prompt_pairs=[{"name": "walk", "positive": "walk"}])
        runs = FakeRuns()
        app = self.app(runs)
        pages, updates = [], []
        page, update = HistoryReader.page, HistoryPane.update_rows

        def count_page(reader: HistoryReader, *arguments: Any, **options: Any) -> Any:
            pages.append(arguments)
            return page(reader, *arguments, **options)

        def count_update(pane: HistoryPane, request: int, rows: list[dict[str, Any]]) -> None:
            updates.append([row["id"] for row in rows])
            update(pane, request, rows)

        with mock.patch.object(HistoryReader, "page", count_page), mock.patch.object(HistoryPane, "update_rows", count_update):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.settle(pilot)
                before = len(pages)
                await self.start(pilot)
                await self.finish(pilot)
                await self.settle(pilot)
                history = self.history(app)
        # One read for the new execution, one when the job ends; each run updates its row in place.
        self.assertEqual(len(pages) - before, 2)
        # The fake runs finish faster than the reads, so what each update found varies; that it read only this row does not.
        self.assertEqual(updates, [[self.executions()[0]["id"]]] * 3)
        self.assertEqual([row[2:] for row in history], [["succeeded", history[0][3], "3/3"]])

    async def test_the_job_is_read_again_when_it_is_run(self) -> None:
        path = self.write_data_job()
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.settle(pilot)
            path.write_text("name: [broken\n", encoding="utf-8")
            await self.command(pilot, "/run walk")
            self.assertIsInstance(app.screen, MainScreen)
            [message] = self.since("/run walk")
        self.assertTrue(message.startswith("Invalid job walk.yaml: "), message)
        self.assertEqual(runs.runners, [])

    async def test_stop_stops_a_run_once(self) -> None:
        self.write_data_job()
        # The gate holds the run after the stop, so stopping can be seen.
        gate = threading.Event()
        runs = FakeRuns(block=True, gate=gate)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            with mock.patch.object(runs.service, "cancel", wraps=runs.service.cancel) as cancel:
                await self.command(pilot, "/stop", settle=False)
                self.assertIsInstance(app.screen, ConfirmScreen)
                await pilot.press("y")
                await pilot.pause()
                self.assertIn("stopping", self.text(app, "status-line"))
                await self.command(pilot, "/stop", settle=False)
                self.assertEqual(self.since("/stop"), ["Already stopping"])
                self.assertIsInstance(app.screen, MainScreen)
                gate.set()
                await self.finish(pilot)
                await self.command(pilot, "/stop", settle=False)
                self.assertEqual(self.since("/stop"), ["No job is running"])
        cancel.assert_called_once_with(signal.SIGINT)
        self.assertIn("Stopping the job (SIGINT)", self.said)
        self.assertIn("Job interrupted: 0/2 runs completed, exit code 130, stopped by SIGINT", self.log())
        self.assertEqual(len(runs.runners), 1)
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"]), ("interrupted", 130))
        self.assertEqual([run["status"] for run in execution["runs"]], ["interrupted"])
        self.assertTrue(run_lock_is_free())

    async def test_stop_during_a_cooldown_ends_it_at_once(self) -> None:
        self.write_data_job(cooldown_seconds=600)
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, lambda: app.live is not None and app.live.cooldown is not None, "the cooldown")
            await pilot.pause()
            cooldown, line = self.text(app, "run-line"), self.text(app, "status-line")
            started = time.monotonic()
            await self.command(pilot, "/stop", settle=False)
            await pilot.press("y")
            await self.finish(pilot)
            elapsed = time.monotonic() - started
        self.assertIn("cooling down", line)
        self.assertRegex(cooldown, r"Cooldown before run 2: (10 min|9 min 59 s) left, until \d\d:\d\d:\d\d")
        self.assertRegex(self.log(), r"Cooldown 10 min before run 2, until \d\d:\d\d:\d\d")
        self.assertIn("Cooldown cut short after", self.log())
        self.assertLess(elapsed, 5)
        self.assertIn("Job interrupted: 1/2 runs completed, exit code 130", self.log())
        self.assertEqual(len(runs.runners), 1)
        [execution] = self.executions()
        self.assertEqual((execution["status"], execution["exit_code"]), ("interrupted", 130))

    async def test_a_busy_lock_starts_nothing_and_the_next_run_runs(self) -> None:
        self.write_data_job()
        self.state.mkdir()
        other = RunLock("run-job")
        other.acquire()
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.finish(pilot)
            status = self.text(app, "run-line")
            other.release()
            await self.start(pilot)
            await self.finish(pilot)
        self.assertIn(f"Did not start: Another run is in progress (run-job, PID {os.getpid()}). Try again when it finishes.", self.said)
        self.assertIn("did not start", status)
        self.assertIn("Job succeeded", self.log())
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
        self.assertIn("Did not start: Could not find 'draw-things-cli' on PATH", self.log())
        self.assertEqual(self.executions(), [])
        self.assertTrue(run_lock_is_free())
        self.assertFalse(self.output_directory.exists())

    async def test_run_during_a_run_and_the_jobs_status(self) -> None:
        self.write_data_job()
        self.write_data_job("z-other.yaml")
        runs = FakeRuns(block=True)
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.start(pilot)
            await self.wait_for(pilot, runs.started.is_set, "run 1")
            await self.command(pilot, "/jobs")
            [during] = [text for text in self.since("/jobs") if text.startswith("Jobs")]
            await self.command(pilot, "/run z-other")
            refused = self.since("/run z-other")
            self.assertIsInstance(app.screen, MainScreen)
            app.request_stop()
            await self.finish(pilot)
            await self.command(pilot, "/jobs")
            [after] = self.since("/jobs")
        self.assertRegex(during, r"walk\.yaml .* running")
        self.assertRegex(during, r"z-other\.yaml .* valid")
        self.assertEqual(refused, ["A job is already running"])
        self.assertNotIn("running", after)

    async def test_a_new_job_replaces_the_last_ones_output(self) -> None:
        self.write_data_job(run_count=1, prompt_pairs=[{"name": "walk", "positive": "walk"}])
        runs = FakeRuns()
        app = self.app(runs)
        async with app.run_test(size=(160, 60)) as pilot:
            self.assertIn("No job has run in this session", self.text(app, "run-line"))
            for _ in range(2):
                await self.start(pilot)
                await self.finish(pilot)
                pane = self.pane(app)
                self.assertEqual(pane.count("Loading model base.ckpt"), 1, pane)
            self.assertIn("sunset-walk: finished", self.text(app, "run-line"))

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

    async def test_quit_and_ctrl_c_twice_during_a_run_offer_to_stop_it(self) -> None:
        self.write_data_job()
        for how in ("/quit", "ctrl+c"):
            with self.subTest(how=how):

                async def ask(pilot: Any, how: str = how) -> None:
                    if how == "ctrl+c":
                        await pilot.press("ctrl+c", "ctrl+c")
                    else:
                        await self.command(pilot, "/quit", settle=False)
                    await pilot.pause()

                gate = threading.Event()
                runs = FakeRuns(block=True, gate=gate)
                app = self.app(runs)
                async with app.run_test(size=(160, 60)) as pilot:
                    await self.start(pilot)
                    await self.wait_for(pilot, runs.started.is_set, "run 1")
                    await ask(pilot)
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    # Ctrl-C does nothing while a dialog is open.
                    await pilot.press("ctrl+c", "ctrl+c")
                    await pilot.pause()
                    self.assertEqual(sum(isinstance(screen, ConfirmScreen) for screen in app.screen_stack), 1)
                    await pilot.press("n")
                    await pilot.pause()
                    self.assertTrue(app.job_running)
                    self.assertIsInstance(app.focused, CommandInput)
                    await ask(pilot)
                    await pilot.press("y")
                    await pilot.pause()
                    self.said.clear()
                    await pilot.press("ctrl+c")
                    await pilot.pause()
                    self.assertEqual(self.said, ["Waiting for the job to stop"])
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
            await self.command(pilot, "/stop", settle=False)
            await pilot.press("y")
            app.handle_signal(signal.SIGTERM)
            gate.set()
            await self.wait_for(pilot, lambda: not pilot.app.is_running, "the app to exit")
        self.assertEqual(app.return_code, 130)
        self.assertEqual(self.executions()[0]["signal"], "SIGINT")

    async def test_the_log_shows_the_redacted_command(self) -> None:
        self.write_data_job()
        runs = FakeRuns()
        app = self.app(runs)
        command = GenerationService.redact_command(["draw-things-cli", "generate", "--api-key", "sekret-value", "--remote-shared-secret", "hush-value"])
        async with app.run_test(size=(160, 60)) as pilot:
            await self.settle(pilot)
            app.live = LiveRun(load_job(self.data / "walk.yaml", self.global_config), self.data / "walk.yaml")
            app.post_message(JobEventMessage(RunStarted(at="", number=1, total=2, pair="walk", positive="walk", negative=None, input=None, resized_input=None, output="out.mov", last_frame=None, command=tuple(command))))
            await pilot.pause()
            status = self.text(app, "run-line")
        self.assertIn("--api-key", self.log())
        for widget in (self.log(), status):
            self.assertNotIn("sekret-value", widget)
            self.assertNotIn("hush-value", widget)

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
                await self.settle(pilot)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertIn("manifest: ", self.log())
        self.assertIn("log: ", self.log())
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
