"""Headless tests of deleting executions from the TUI (Milestone 06): ``d`` and ``Space`` on the Execution History
widget, ``/delete``, and the dialog, against a fake ``dtc serve`` that deletes from the test's store."""

from __future__ import annotations

from typing import Any

from textual.widgets import Button

from draw_things_control.state.executions import ExecutionSettings, NewExecution
from draw_things_control.state.queue import NewQueueEntry
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.delete_dialog import DeleteDialog
from draw_things_control.tui.panes.execution import ExecutionPane
from draw_things_control.tui.panes.history import HistoryPane
from draw_things_control.tui.screens import MainScreen
from tests.tui.test_history import HistoryCase, at


class DeleteTests(HistoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.database = self.paths.database

    def ids(self, app: DrawThingsApp) -> list[str]:
        return [row[0] for row in self.history(app)]

    async def dialog(self, pilot: Any) -> str:
        """Wait for the dialog; its text."""
        await self.wait_for(pilot, lambda: isinstance(pilot.app.screen, DeleteDialog), "the dialog")
        await pilot.pause()
        return self.text(pilot.app, "delete-text")

    async def answer(self, pilot: Any, key: str) -> None:
        await pilot.press(key)
        await pilot.pause()

    async def finished(self, pilot: Any) -> None:
        await self.wait_for(pilot, lambda: not pilot.app._deleting, "the deletion to finish")
        await self.settle(pilot)

    async def focus_history(self, pilot: Any, row: int = 0) -> HistoryPane:
        pane = pilot.app.screen.query_one(HistoryPane)
        pane.focus()
        pane.move_cursor(row=row)
        await pilot.pause()
        return pane

    async def test_d_asks_and_deletes_the_selected_execution(self) -> None:
        self.add("walk", 0, runs=("succeeded", "failed"), status="failed")
        second = self.add("wave", 30)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.focus_history(pilot)
            detail = app.screen.query_one(ExecutionPane)
            await self.wait_for(pilot, lambda: detail.execution_id == second, "the detail")
            await pilot.press("d")
            text = await self.dialog(pilot)
            self.assertNotIn("Skip", str([button.label for button in app.screen.query(Button)]))
            await self.answer(pilot, "y")
            await self.finished(pilot)
            ids = self.ids(app)
            await self.wait_for(pilot, lambda: detail.execution_id != second, "the detail to move")
        self.assertTrue(text.startswith("Delete E0002 (wave, succeeded, 1/1 runs, "), text)
        self.assertIn("Its log and manifest are deleted; its outputs stay.", text)
        self.assertEqual(ids, ["E0001"])
        self.assertIn("Deleted 1 execution", self.said)
        self.assertEqual(self.server.deletions, [(["E0002"], True), (["E0002"], False)])

    async def test_marks_are_kept_by_execution_across_a_re_read_and_cleared_by_a_filter(self) -> None:
        for minutes in (0, 10, 20):
            self.add("walk", minutes)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            pane = await self.focus_history(pilot, 0)
            await pilot.press("space")
            # The ID column widens for the mark, so it never crops the ID's last digit.
            width = pane.columns[pane.column_keys[0]].content_width
            await self.focus_history(pilot, 2)
            await pilot.press("space")
            marked = self.marks_of(app)
            # A new execution moves every row down one; the marks follow their executions.
            self.add("walk", 30)
            await self.command(pilot, "/get history")
            after_read = self.marks_of(app)
            await self.command(pilot, "/filter name walk")
            after_filter = self.marks_of(app)
        self.assertEqual(width, len("*E0003"))
        self.assertEqual(marked, ["E0003", "E0001"])
        self.assertEqual(after_read, ["E0003", "E0001"])
        self.assertEqual(after_filter, [])

    async def test_several_step_through_delete_skip_and_cancel(self) -> None:
        for minutes in (0, 10, 20, 30):
            self.add("walk", minutes)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete execution E1 e3 E0004", settle=False)
            first = await self.dialog(pilot)
            await self.answer(pilot, "s")
            second = await self.dialog(pilot)
            await self.answer(pilot, "enter")
            third = await self.dialog(pilot)
            await self.answer(pilot, "n")
            await self.finished(pilot)
            ids = self.ids(app)
        # History order, newest first, whatever order they were typed in.
        self.assertIn("Delete E0004 (walk", first)
        self.assertIn("1 of 3", first)
        self.assertIn("Delete E0003", second)
        self.assertIn("3 of 3", third)
        self.assertEqual(ids, ["E0004", "E0002", "E0001"])
        self.assertIn("Deleted 1 execution", self.said)

    async def test_delete_all_deletes_the_rest_without_asking_and_marks_clear(self) -> None:
        for minutes in (0, 10, 20):
            self.add("walk", minutes)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            pane = await self.focus_history(pilot, 0)
            await pilot.press("space")
            await self.focus_history(pilot, 1)
            await pilot.press("space")
            await pilot.press("d")
            await self.dialog(pilot)
            await self.answer(pilot, "a")
            await self.finished(pilot)
            ids, marks = self.ids(app), pane.marks
        self.assertEqual((ids, marks), (["E0001"], set()))
        self.assertEqual(self.server.deletions[-1], (["E0003", "E0002"], False))
        self.assertIn("Deleted 2 executions", self.said)

    async def test_the_cursor_stays_on_its_execution_when_rows_above_it_go(self) -> None:
        for minutes in (0, 10, 20, 30):
            self.add("walk", minutes)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            pane = await self.focus_history(pilot, 0)
            await pilot.press("space")
            await self.focus_history(pilot, 1)
            await pilot.press("space")
            await self.focus_history(pilot, 2)
            await pilot.press("d")
            await self.dialog(pilot)
            await self.answer(pilot, "a")
            await self.finished(pilot)
            ids, cursor = self.ids(app), pane.cursor_row
        self.assertEqual(ids, ["E0002", "E0001"])
        self.assertEqual(ids[cursor], "E0002")

    async def test_delete_filtered_reads_past_the_first_page_and_is_refused_with_no_filter(self) -> None:
        store = self.store()
        for minutes in range(205):
            name = "walk" if minutes % 2 == 0 else "wave"
            store.executions.start(NewExecution(job_name=name, job_file=f"/jobs/{name}.yaml", mode="i2v", started_at=at(minutes), settings=ExecutionSettings(output_directory=str(self.outputs))))
        for execution_id in range(1, 206):
            store.executions.finish(execution_id, status="succeeded", exit_code=0, signal=None, finished_at=at(300))
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete filtered")
            refused = self.since("/delete filtered")
            await self.command(pilot, "/filter name walk")
            await self.command(pilot, "/delete filtered", settle=False)
            text = await self.dialog(pilot)
            await self.answer(pilot, "a")
            await self.finished(pilot)
            remaining = len(app.screen.query_one(HistoryPane).executions)
        self.assertEqual(refused, ["No filter is set; use /delete all to delete the whole history"])
        self.assertIn("1 of 103", text)
        # 103 walks: one dry run, then one Delete all request, each under 200 IDs.
        self.assertEqual([(len(ids), dry_run) for ids, dry_run in self.server.deletions], [(103, True), (103, False)])
        self.assertEqual(remaining, 0)
        self.assertEqual(len(self.store().executions.page(limit=1000)), 102)

    async def test_delete_all_deletes_the_whole_history_in_batches(self) -> None:
        store = self.store()
        for minutes in range(201):
            store.executions.start(NewExecution(job_name="walk", job_file="/jobs/walk.yaml", mode="i2v", started_at=at(minutes), settings=ExecutionSettings(output_directory=str(self.outputs))))
            store.executions.finish(minutes + 1, status="succeeded", exit_code=0, signal=None, finished_at=at(300))
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete all", settle=False)
            await self.dialog(pilot)
            await self.answer(pilot, "a")
            await self.finished(pilot)
        self.assertEqual([(len(ids), dry_run) for ids, dry_run in self.server.deletions], [(200, True), (1, True), (200, False), (1, False)])
        self.assertEqual(self.executions(), [])

    async def test_a_running_execution_is_left_out_and_the_resume_warning_shows(self) -> None:
        self.add("walk", 0, status=None, runs=("running",))
        self.add("wave", 10)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            # The lock is free, so the running row reads as interrupted; the server still refuses it.
            await self.command(pilot, "/delete all", settle=False)
            text = await self.dialog(pilot)
            await self.answer(pilot, "n")
            await self.finished(pilot)
        self.assertIn("Not deleted: E0001 is running; it cannot be deleted", self.said)
        self.assertNotIn("of 2", text)
        self.assertIn("Delete E0002", text)

    async def test_an_execution_a_resume_takes_while_the_dialog_is_open_is_refused_by_the_server(self) -> None:
        self.add("walk", 0, status="interrupted", runs=("succeeded", "failed"))
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete execution E0001", settle=False)
            await self.dialog(pilot)
            store = self.store()
            new = NewQueueEntry(job_path="/jobs/walk.yaml", job_text="name: walk\n", config_file="base.yaml", config_text="model: m.ckpt\n", input_directory="/in", output_directory=str(self.outputs), cooldown_default=None, settings=ExecutionSettings(output_directory=str(self.outputs)), submitted_at=at(5), total_runs=2, resumes=None, resumes_execution=1)
            store.queue.submit(new)
            await self.answer(pilot, "y")
            await self.finished(pilot)
            ids = self.ids(app)
        self.assertIn("Not deleted: Q0001 is queued to resume from E0001", self.said)
        self.assertIn("Nothing was deleted", self.said)
        self.assertEqual(ids, ["E0001"])

    async def test_the_server_down_opens_no_dialog(self) -> None:
        self.add("walk", 0)
        self.server.unreachable = True
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete execution E0001", settle=False)
            await self.finished(pilot)
            screen = app.screen
        self.assertIsInstance(screen, MainScreen)
        self.assertTrue(any("Cannot reach the server" in line for line in self.since("/delete execution E0001")))
        self.assertEqual(len(self.executions()), 1)

    async def test_a_request_failing_partway_closes_the_dialog_and_says_how_many_went(self) -> None:
        for minutes in (0, 10, 20):
            self.add("walk", minutes)
        self.server.deletes_before_drop = 1
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete all", settle=False)
            await self.dialog(pilot)
            await self.answer(pilot, "y")
            await self.dialog(pilot)
            await self.answer(pilot, "y")
            await self.finished(pilot)
            screen, ids = app.screen, self.ids(app)
        self.assertIsInstance(screen, MainScreen)
        self.assertIn("1 execution deleted before it", self.said)
        self.assertEqual(ids, ["E0002", "E0001"])

    async def test_missing_ids_and_the_usage_are_said(self) -> None:
        self.add("walk", 0)
        app = self.app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/delete execution E0099")
            missing = self.since("/delete execution E0099")
            await self.command(pilot, "/delete")
            usage = self.since("/delete")
        self.assertEqual(missing, ["No execution E0099"])
        self.assertTrue(usage[0].startswith("Usage: /delete execution"), usage)
