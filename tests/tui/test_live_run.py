"""Headless tests of following a job from the TUI: events arrive over the gRPC feed from a fake ``dtc serve``; the TUI
never runs a job itself (Milestone 03), so nothing here starts draw-things-cli."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.core.arguments import redact_command
from draw_things_control.core.run_lock import RunLock
from draw_things_control.services.history import HistoryReader
from draw_things_control.state.executions import ExecutionRow, NewExecution, NewRun
from draw_things_control.state.store import Store
from draw_things_control.tui.live_run import MAX_OUTPUT_LINES, PastRun
from draw_things_control.tui.panes.execution import ExecutionPane
from draw_things_control.tui.panes.history import HistoryPane
from tests.tui.fake_server import job_finished, job_started, run_finished, run_output, run_started
from tests.tui.feed_case import FeedTestCase

OLD = "2026-09-20T09:00:00+00:00"


class LiveRunTests(FeedTestCase):
    async def start_job(self, pilot: Any, *, queue_id: str = "Q0001", total_runs: int = 2) -> None:
        self.server.send_queue_entry(queue_id, "running")
        await self.push(pilot, job_started(total_runs=total_runs))
        await self.wait_for(pilot, lambda: pilot.app.live is not None, "the job to start")

    async def test_a_job_runs_with_progress_and_ends_with_its_result(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            self.assertIn("No job has run in this session", self.text(app, "run-line"))
            await self.start_job(pilot)
            await self.push(pilot, run_started(1), run_output("Loading model base.ckpt"), run_output("Sampling... 4 / 8 [ ] 50  %", progress=(4, 8), percent=50))
            running = self.text(app, "status").split("\n")
            run_line = self.text(app, "run-line")
            pane = self.pane(app)
            await self.push(pilot, run_finished(1), run_started(2), run_finished(2), job_finished())
            ended = self.text(app, "status").split("\n")
            final_run_line = self.text(app, "run-line")
        self.assertEqual(pane, ["Loading model base.ckpt", "Sampling... 4 / 8 [ ] 50  %"])
        self.assertIn("Run 1/2", run_line)
        self.assertIn("progress 4/8, 50%", run_line)
        self.assertEqual(running[0], "running  E0001: walk  run 1/2")
        self.assertTrue(running[3].startswith("step 4/8  "), running)
        self.assertEqual(ended[:2], ["finished (succeeded)  E0001: walk", "2/2 runs succeeded"])
        self.assertIn("walk: finished", final_run_line)
        self.assertIn("Job started: walk (i2v, 2 runs, seed 1 (job), model base.ckpt)", self.said)
        self.assertIn("Job succeeded: 2/2 runs completed", self.log())

    async def test_a_new_job_replaces_the_last_ones_output(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            for _ in range(2):
                await self.start_job(pilot, total_runs=1)
                await self.push(pilot, run_started(1, 1), run_output("Loading model base.ckpt"), run_finished(1), job_finished(1))
                self.assertEqual(self.pane(app).count("Loading model base.ckpt"), 1, self.pane(app))

    async def test_the_output_pane_keeps_the_last_lines_as_written(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.start_job(pilot, total_runs=1)
            await self.push(pilot, run_started(1, 1), *(run_output(f"line {number} [x]") for number in range(MAX_OUTPUT_LINES + 150)))
            pane = self.pane(app)
            model = list(app.live.output)  # type: ignore[union-attr]
        self.assertLessEqual(len(pane), MAX_OUTPUT_LINES)
        self.assertEqual(len(model), MAX_OUTPUT_LINES)
        self.assertEqual(pane[-1], f"line {MAX_OUTPUT_LINES + 149} [x]")
        self.assertNotIn("line 0 [x]", pane)

    async def test_stop_cancels_the_followed_entry_once(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/stop")
            self.assertEqual(self.since("/stop"), ["No job is running"])
            await self.start_job(pilot, queue_id="Q0007")
            await self.push(pilot, run_started(1))
            await self.command(pilot, "/stop")
            await self.wait_for(pilot, lambda: app.live is not None and app.live.stop_requested, "the stop to be confirmed")
            await self.command(pilot, "/stop")
            self.assertEqual(self.since("/stop"), ["Already stopping"])
        self.assertEqual(self.server.cancelled_ids, ["Q0007"])
        self.assertIn("Q0007 cancelled", self.said)

    async def test_attaching_to_a_running_entry_shows_where_it_is_and_marks_the_pane(self) -> None:
        self.server.running = [{"queue_id": "Q0003", "job_path": "/jobs/walk.yaml", "total_runs": 2, "submitted_at": "t"}]
        self.server.details["Q0003"] = {"queue_id": "Q0003", "execution_id": None, "current_run": 1, "current_run_elapsed_seconds": 12.0, "current_step": 3, "current_step_total": 8}
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.wait_for(pilot, lambda: app.live is not None, "the seeded job")
            await self.wait_for(pilot, lambda: self.pane(app), "the marker in the pane")
            run_line = self.text(app, "run-line")
            pane = self.pane(app)
        self.assertIn("Run 1/2", run_line)
        self.assertIn("progress 3/8", run_line)
        self.assertEqual(pane, ["(earlier output not shown)"])
        self.assertEqual(self.server.requests[0].last_event_id, 1)
        self.assertIsInstance(app.live.path, Path)  # type: ignore[union-attr]
        # An attach is not a start this session saw.
        self.assertNotIn("Job started", self.log())

    async def test_a_signal_quits_at_once_with_128_plus_n(self) -> None:
        for received in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=received.name):
                app = self.make_app()
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.handle_signal(received)
                    await self.wait_for(pilot, lambda: not pilot.app.is_running, "the app to exit")
                self.assertEqual(app.return_code, 128 + received.value)

    async def test_the_signal_handlers_are_registered_while_the_app_runs(self) -> None:
        app = self.make_app()
        with mock.patch.object(asyncio.get_running_loop(), "add_signal_handler") as add:
            async with app.run_test() as pilot:
                await pilot.pause()
        self.assertEqual({call.args[0] for call in add.call_args_list}, {signal.SIGHUP, signal.SIGTERM, signal.SIGINT})

    async def test_the_server_holding_the_lock_gets_its_own_status_line_while_the_feed_is_down(self) -> None:
        # A connected feed already says the server is up, so the lock is read only while the feed cannot reach it.
        self.server.health_ok = False
        lock = RunLock("serve", directory=self.state)
        lock.acquire()
        self.addCleanup(lock.release)
        app = self.make_app()
        wanted = f"The dtc server (PID {os.getpid()}) holds the run lock while it is up; stop it to generate by hand."
        async with app.run_test(size=(160, 60)) as pilot:
            await self.settle(pilot)
            await self.wait_for(pilot, lambda: self.text(app, "status").split("\n")[0] == wanted, "the server's lock line")


class StoreBackedTests(FeedTestCase):
    """What the running job does to the panes that read the state store, driven through the feed."""

    def store(self) -> Store:
        from draw_things_control.state.store import ensure_state_directory

        ensure_state_directory(self.state)
        store = Store.open(self.paths.database)
        self.addCleanup(store.close)
        return store

    def add_execution(self, name: str, *, finished: bool = True, runs: int = 1, seconds: float = 600.0, steps: int = 30) -> int:
        store = self.store()
        execution_id = store.executions.start(NewExecution(job_name=name, job_file=f"/jobs/{name}.yaml", mode="i2v", total_runs=runs, started_at=OLD))
        for number in range(1, runs + 1):
            store.executions.start_run(execution_id, number, NewRun(pair="p", positive="text", started_at=OLD, command=["draw-things-cli", "generate", "--steps", str(steps)]))
            if finished:
                store.executions.finish_run(execution_id, number, status="succeeded", exit_code=0, seconds=seconds, output="a.mov", last_frame=None)
        if finished:
            store.executions.finish(execution_id, status="succeeded", exit_code=0, signal=None, finished_at="2026-09-20T09:01:00+00:00")
        return execution_id

    async def test_a_new_job_moves_the_history_cursor_unless_the_history_is_being_browsed(self) -> None:
        self.add_execution("old")
        for browsing in (False, True):
            with self.subTest(browsing=browsing):
                app = self.make_app()
                async with app.run_test(size=(160, 60)) as pilot:
                    await self.connect(pilot)
                    table = app.screen.query_one(HistoryPane)
                    await self.wait_for(pilot, lambda table=table: table.selected is not None, "the history")
                    before = table.selected
                    if browsing:
                        app.screen.set_focus(table)
                    # The server records the execution just before it announces the job.
                    new = self.add_execution("walk", finished=False)
                    await self.push(pilot, job_started(execution_id=f"E{new:04d}"))
                    await self.settle(pilot)
                    await self.wait_for(pilot, lambda table=table, new=new: new in table.executions, "the new row")
                    if not browsing:
                        await self.wait_for(pilot, lambda app=app, new=new: app.screen.query_one(ExecutionPane).execution_id == new, "the new execution in the detail")
                    selected = table.selected
                self.assertEqual(selected, before if browsing else new)

    async def test_a_finished_run_updates_its_row_without_reading_the_whole_history(self) -> None:
        pages, updates = [], []
        page, update = HistoryReader.page, HistoryPane.update_rows

        def count_page(reader: HistoryReader, *arguments: Any, **options: Any) -> Any:
            pages.append(arguments)
            return page(reader, *arguments, **options)

        def count_update(pane: HistoryPane, request: int, rows: list[ExecutionRow]) -> None:
            updates.append([row.id for row in rows])
            update(pane, request, rows)

        app = self.make_app()
        with mock.patch.object(HistoryReader, "page", count_page), mock.patch.object(HistoryPane, "update_rows", count_update):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.connect(pilot)
                before = len(pages)
                execution_id = self.add_execution("walk", finished=False, runs=3)
                await self.push(pilot, job_started(total_runs=3, execution_id=f"E{execution_id:04d}"))
                await self.settle(pilot)
                store = self.store()
                for number in (1, 2, 3):
                    await self.push(pilot, run_started(number, 3))
                    store.executions.finish_run(execution_id, number, status="succeeded", exit_code=0, seconds=5.0, output="a.mov", last_frame=None)
                    await self.push(pilot, run_finished(number))
                    await self.wait_for(pilot, lambda number=number: len(updates) == number, f"run {number}'s row")
                store.executions.finish(execution_id, status="succeeded", exit_code=0, signal=None, finished_at="2026-09-20T09:05:00+00:00")
                await self.push(pilot, job_finished(3))
                await self.settle(pilot)
                history = self.history(app)
        # One read for the new execution, one when the job ends; each run updates its row in place.
        self.assertEqual(len(pages) - before, 2)
        self.assertEqual(updates, [[execution_id]] * 3)
        self.assertEqual([row[2:] for row in history], [["succeeded", history[0][3], "3/3"]])

    async def test_the_log_shows_the_redacted_command_and_nothing_else_of_the_secrets(self) -> None:
        command = tuple(redact_command(("draw-things-cli", "generate", "--api-key", "sekret-value", "--remote-shared-secret", "hush-value")))
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.push(pilot, job_started())
            await self.push(pilot, replace(run_started(1), command=command))
            run_line = self.text(app, "run-line")
        self.assertIn("--api-key", self.log())
        for widget in (self.log(), run_line):
            self.assertNotIn("sekret-value", widget)
            self.assertNotIn("hush-value", widget)

    async def test_a_job_is_estimated_from_the_latest_successful_run_before_it_reports_a_step(self) -> None:
        self.add_execution("other")
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.push(pilot, job_started())
            await self.wait_for(pilot, lambda: app.live is not None and app.live.past_run is not None, "the past run")
            await self.push(pilot, run_started(1))
            past = app.live.past_run  # type: ignore[union-attr]
            status = self.text(app, "status").split("\n")
        self.assertEqual(past, PastRun(600.0, 30))
        self.assertIn("ends ~", status[1])
        self.assertIn("ends ~", status[2])
        # 600 s less the moment the run has taken so far.
        self.assertRegex(status[2], r"\(in (10 min|9 min 5\d s)\)$")

    async def test_an_attached_job_is_estimated_from_the_past_run_too(self) -> None:
        self.add_execution("other")
        self.server.running = [{"queue_id": "Q0003", "job_path": "/jobs/walk.yaml", "total_runs": 2, "submitted_at": OLD}]
        self.server.details["Q0003"] = {"queue_id": "Q0003", "execution_id": None, "current_run": 1, "current_run_elapsed_seconds": 10.0}
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.wait_for(pilot, lambda: app.live is not None and app.live.past_run is not None, "the past run")
            past = app.live.past_run  # type: ignore[union-attr]
        self.assertEqual(past, PastRun(600.0, 30))
