"""Headless tests of the history pane, the execution detail, and reveal, over a temporary state store."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

from rich.text import Text

from draw_things_control.core.global_config import CooldownPolicy
from draw_things_control.core.run_lock import RunLock, ensure_state_directory
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_events import CooldownStarted
from draw_things_control.state.store import Store
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.history import HistoryFilter, HistoryPage, HistoryReader
from draw_things_control.tui.panes import HistoryPane
from draw_things_control.tui.screens import MainScreen
from draw_things_control.tui.text import confirm_run_text, event_text, stored_cooldown_text
from tests.fixtures import job_data
from tests.tui.fake_runs import FakeRuns
from tests.tui.tui_case import TuiTestCase

START = datetime(2026, 9, 20, 9, 0).astimezone()


def at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class HistoryTests(TuiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()

    def app(self) -> DrawThingsApp:
        return self.make_app(FakeRuns().service)

    def store(self) -> Store:
        ensure_state_directory()
        store = Store()
        self.addCleanup(store.close)
        return store

    def add(self, name: str, minutes: int, *, status: str | None = "succeeded", runs: tuple[str, ...] = ("succeeded",), job_file: str | None = None, outputs: tuple[str | None, ...] | None = None) -> int:
        """An execution with its runs; ``status`` None leaves it running. Output files are named after the job and run."""
        store = self.store()
        execution_id = store.start_execution(job_name=name, job_file=job_file or f"/jobs/{name}.yaml", mode="i2v", model="base.ckpt", seed=42, seed_source="config_file", cooldown_seconds=5.0, cooldown_source="job", total_runs=len(runs), started_at=at(minutes), job_yaml=f"name: {name}\n", settings={"output_directory": str(self.outputs)})
        for number, run_status in enumerate(runs, start=1):
            output = outputs[number - 1] if outputs is not None else f"{name}-{number}.mov"
            store.start_run(execution_id, number, pair="walk", positive="a walk [slow]", negative="blurry", input="in.png", output=output, last_frame=None, command=["draw-things-cli", "generate", "--api-key", "[redacted]"], started_at=at(minutes))
            if run_status != "running":
                store.finish_run(execution_id, number, status=run_status, exit_code=0 if run_status == "succeeded" else 1, seconds=12.5, output=output, last_frame=None)
        if status is not None:
            store.finish_execution(execution_id, status=status, exit_code=0 if status == "succeeded" else 1, signal=None, finished_at=at(minutes + 1))
        return execution_id

    def columns(self, app: DrawThingsApp, *indexes: int) -> list[list[str]]:
        return [[row[index] for index in indexes] for row in self.history(app)]

    async def detail(self, pilot: Any, execution_id: int) -> str:
        await self.command(pilot, f"/execution {execution_id}")
        return "\n".join(self.since(f"/execution {execution_id}"))

    async def test_an_empty_history_says_so_and_creates_nothing(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            table = app.screen.query_one(HistoryPane)
            self.assertEqual((table.row_count, table.border_title, table.border_subtitle), (0, "History", "No execution history yet"))
            detail = await self.detail(pilot, 1)
        self.assertEqual(detail, "No execution history yet")
        self.assertFalse(self.state.exists())

    async def test_executions_are_listed_newest_first_with_their_succeeded_runs(self) -> None:
        older = self.add("walk", 0, runs=("succeeded", "failed", "succeeded"), status="failed")
        newer = self.add("wave", 30, runs=("succeeded",))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            rows = self.history(app)
        self.assertEqual(rows, [[str(newer), "wave", "succeeded", START.strftime("%m-%d ") + "09:30", "1/1"], [str(older), "walk", "failed", START.strftime("%m-%d %H:%M"), "2/3"]])

    async def test_the_filters_narrow_combine_and_come_off(self) -> None:
        self.add("sunset-walk", 0, job_file="/jobs/evening.yaml")
        self.add("sunset-walk", 10, status="failed", runs=("failed",), job_file="/jobs/evening.yaml")
        self.add("wave_100%", 20, job_file="/jobs/Wave.yml")
        self.add("dawn", 30, job_file="/walk/dawn.yaml")
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            table = app.screen.query_one(HistoryPane)
            results: dict[str, tuple[list[list[str]], str, str]] = {}
            for line in ("/filter name WALK", "/filter status failed", "/filter name even", "/filter status succeeded", "/filter name wave_1", "/filter name 100%", "/filter name t_", "/filter off", "/filter name walk", "/filter status running"):
                await self.command(pilot, line)
                results[line] = (self.columns(app, 1, 2), str(table.border_title), str(table.border_subtitle))
        # A name matches the job name or the job file's name, in any case; never the directory.
        self.assertEqual(results["/filter name WALK"][0], [["sunset-walk", "failed"], ["sunset-walk", "succeeded"]])
        self.assertEqual(results["/filter status failed"][0], [["sunset-walk", "failed"]])
        self.assertEqual(results["/filter status failed"][1], "History: status failed, name WALK")
        self.assertEqual(results["/filter name even"][0], [["sunset-walk", "failed"]])
        self.assertEqual(results["/filter status succeeded"][0], [["sunset-walk", "succeeded"]])
        # % and _ are matched as written, not as wildcards.
        self.assertEqual(results["/filter name wave_1"][0], [["wave_100%", "succeeded"]])
        self.assertEqual(results["/filter name 100%"][0], [["wave_100%", "succeeded"]])
        self.assertEqual(results["/filter name t_"][0], [])
        self.assertEqual(results["/filter name t_"][2], "No executions match the filter")
        self.assertEqual((len(results["/filter off"][0]), results["/filter off"][1], results["/filter off"][2]), (4, "History", ""))
        self.assertEqual(results["/filter status running"][0], [])
        self.assertIn("History: status running, name walk", self.said)

    async def test_pages_load_at_the_last_row_and_a_refresh_keeps_them_and_the_selection(self) -> None:
        ids = [self.add(f"job{number}", number) for number in range(8)]
        app = self.app()
        with mock.patch("draw_things_control.tui.panes.PAGE_SIZE", 3), mock.patch("draw_things_control.tui.history.PAGE_SIZE", 3):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                table = app.screen.query_one(HistoryPane)
                first = table.row_count
                table.focus()
                await pilot.press("down", "down")
                await self.settle(pilot)
                second = table.row_count
                await pilot.press("down", "down")
                await self.settle(pilot)
                self.add("newest", 100)
                await self.command(pilot, "/history")
                rows = [row[0] for row in self.history(app)]
                selected = rows[table.cursor_row]
        self.assertEqual((first, second), (3, 6))
        self.assertEqual(rows[0], str(max(ids) + 1))
        self.assertGreaterEqual(len(rows), 7)
        self.assertEqual(selected, str(ids[-5]))

    async def test_running_rows_read_as_interrupted_when_the_lock_is_free_and_are_polled_when_held(self) -> None:
        running = self.add("walk", 0, status=None, runs=("running",))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            free = self.columns(app, 2, 4)
        self.assertEqual(free, [["interrupted", "0/1"]])
        lock = RunLock("run-job")
        lock.acquire()
        self.addCleanup(lock.release)
        app = self.app()
        with mock.patch("draw_things_control.tui.panes.HISTORY_POLL_SECONDS", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                held = self.columns(app, 2, 4)
                store = self.store()
                store.finish_run(running, 1, status="succeeded", exit_code=0, seconds=1.0, output="walk-1.mov", last_frame=None)
                store.finish_execution(running, status="succeeded", exit_code=0, signal=None, finished_at=at(1))
                await self.wait_for(pilot, lambda: self.columns(app, 2, 4) == [["succeeded", "1/1"]], "the poll to see the finished row")
        self.assertEqual(held, [["running", "0/1"]])

    async def test_the_detail_shows_the_stored_snapshot_and_marks_missing_files(self) -> None:
        job_file = self.data / "walk.yaml"
        self.write_data_job("walk.yaml")
        execution_id = self.add("sunset-walk", 0, runs=("succeeded", "failed"), job_file=str(job_file), outputs=("kept.mov", "gone.mov"))
        (self.outputs / "kept.mov").write_bytes(b"video")
        job_file.unlink()
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            detail = await self.detail(pilot, execution_id)
            self.said.clear()
            table = app.screen.query_one(HistoryPane)
            table.focus()
            await pilot.press("enter")
            await self.settle(pilot)
            from_row = "\n".join(self.said)
            unknown = await self.detail(pilot, 99)
        self.assertEqual(from_row, detail)
        self.assertTrue(detail.startswith(f"Execution {execution_id}: sunset-walk\n"), detail)
        self.assertNotIn("imported", detail)
        for line in ("  status: succeeded, exit code 0", f"  job file: {job_file}", "  mode: i2v", "  model: base.ckpt", "  seed: 42 (config_file)", "  cooldown: 5 s (job)", f"  started: {at(0)}", f"  finished: {at(1)}", "  manifest: -", "Run 1 succeeded (pair walk, 12.5 s, exit code 0)", "Run 2 failed (pair walk, 12.5 s, exit code 1)", "  positive: a walk [slow]", "  negative: blurry", "  input: in.png", f"  output: {self.outputs / 'kept.mov'}\n", f"  output: {self.outputs / 'gone.mov'} (missing)", "  command: draw-things-cli generate --api-key [redacted]"):
            self.assertIn(line, detail)
        self.assertEqual(unknown, "No execution 99")

    def test_the_cooldown_detail_for_each_mode_and_for_old_rows(self) -> None:
        def row(cooldown_seconds: float | None, source: str, mapping: dict | None) -> dict[str, Any]:
            return {"cooldown_seconds": cooldown_seconds, "cooldown_source": source, "settings": {"cooldown": mapping} if mapping is not None else {}}

        self.assertEqual(stored_cooldown_text(row(None, "global_config", {"mode": "auto", "ratio": 0.5, "minimum_seconds": 300.0, "maximum_seconds": 1800.0})), "auto, half, 5 min to 30 min (global_config)")
        self.assertEqual(stored_cooldown_text(row(None, "job", {"mode": "auto", "ratio": 0.4, "minimum_seconds": 0.0, "maximum_seconds": 3600.0})), "auto, 40%, 0 s to 1 h (job)")
        self.assertEqual(stored_cooldown_text(row(900.0, "global_config", {"mode": "manual", "seconds": 900.0})), "900 s (global_config)")
        self.assertEqual(stored_cooldown_text(row(0.0, "job", {"mode": "off"})), "off (job)")
        # Rows and manifests from before the mapping keep only the seconds, which mean manual.
        self.assertEqual(stored_cooldown_text(row(900.0, "global_config", None)), "900 s (global_config)")
        self.assertEqual(stored_cooldown_text(row(None, "default", None)), "-")

    def test_the_cooldown_messages_and_the_confirmation(self) -> None:
        def started(**changes: Any) -> str:
            return str(event_text(CooldownStarted(**{"at": at(0), "after_run": 1, "seconds": 900.0, "until": "14:05:00", "run_seconds": 1440.0, **changes})))

        self.assertEqual(started(), "Cooldown 15 min before run 2, until 14:05:00")
        self.assertEqual(started(mode="auto", ratio=0.5, seconds=720.0), "Cooldown 12 min (half of run 1's 24 min) before run 2, until 14:05:00")
        self.assertEqual(started(mode="auto", ratio=0.4, seconds=576.0), "Cooldown 9 min 36 s (40% of run 1's 24 min) before run 2, until 14:05:00")
        self.assertEqual(started(mode="auto", ratio=0.5, seconds=300.0, run_seconds=180.0, bound="minimum"), "Cooldown 5 min (the minimum; half of run 1's 3 min is less) before run 2, until 14:05:00")
        self.assertEqual(started(mode="auto", ratio=0.5, seconds=1800.0, run_seconds=4800.0, bound="maximum"), "Cooldown 30 min (the maximum; half of run 1's 1 h 20 min is more) before run 2, until 14:05:00")
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="auto", minimum_seconds=300.0, maximum_seconds=1800.0))
        job = load_job(self.write_job(job_data(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.dt_config)
        self.assertIn("  Cooldown: auto: half of each run's time, 5 min to 30 min, from global_config (up to 2 waits, 1 h total at most)\n", str(confirm_run_text(job, "draw-things-cli")))
        job = load_job(self.write_job(job_data(cooldown={"mode": "off"})), self.global_config, self.dt_config)
        self.assertIn("  Cooldown: off (job)\n", str(confirm_run_text(job, "draw-things-cli")))

    async def test_an_imported_execution_is_marked_and_its_outputs_are_beside_its_manifest(self) -> None:
        (self.outputs / "old-1.mov").write_bytes(b"video")
        execution_id = self.store().import_execution(
            {"job_name": "old", "job_file": "/jobs/old.yaml", "mode": "t2v", "status": "succeeded", "seed": 7, "total_runs": 1, "started_at": at(0), "finished_at": at(1), "manifest_path": str(self.outputs / "old.json")},
            [{"pair": "walk", "positive": "walk", "output": "old-1.mov", "started_at": at(0), "seconds": 3.0, "exit_code": 0, "status": "succeeded"}],
        )
        app = self.app()
        reveal = mock.Mock(return_value=None)
        with mock.patch("draw_things_control.tui.screens.reveal_in_finder", reveal):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                detail = await self.detail(pilot, execution_id)
                await self.command(pilot, f"/reveal {execution_id}")
        self.assertTrue(detail.startswith(f"Execution {execution_id}: old  imported\n"), detail)
        self.assertIn("  seed: 7 (-)", detail)
        self.assertTrue(detail.endswith(f"  output: {self.outputs / 'old-1.mov'}"), detail)
        reveal.assert_called_once_with(self.outputs / "old-1.mov")

    async def test_reveal_shows_the_output_in_finder_or_says_why_not(self) -> None:
        execution_id = self.add("walk", 0, runs=("succeeded", "succeeded", "failed"), outputs=("one.mov", "two.mov", None))
        (self.outputs / "one.mov").write_bytes(b"video")
        (self.outputs / "two.mov").write_bytes(b"video")
        app = self.app()
        reveal = mock.Mock(return_value=None)
        with mock.patch("draw_things_control.tui.screens.reveal_in_finder", reveal):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                results = {}
                for line in (f"/reveal {execution_id}", f"/reveal {execution_id} 1", f"/reveal {execution_id} 3", f"/reveal {execution_id} 9", "/reveal 99"):
                    await self.command(pilot, line)
                    results[line] = self.since(line)
                (self.outputs / "one.mov").unlink()
                await self.command(pilot, f"/reveal {execution_id} 1")
                missing = self.since(f"/reveal {execution_id} 1")
        self.assertEqual(results[f"/reveal {execution_id}"], [f"Revealed {self.outputs / 'two.mov'}"])
        self.assertEqual(results[f"/reveal {execution_id} 1"], [f"Revealed {self.outputs / 'one.mov'}"])
        self.assertEqual(results[f"/reveal {execution_id} 3"], [f"Run 3 of execution {execution_id} has no output"])
        self.assertEqual(results[f"/reveal {execution_id} 9"], [f"Execution {execution_id} has no run 9"])
        self.assertEqual(results["/reveal 99"], ["No execution 99"])
        self.assertEqual(missing, [f"{self.outputs / 'one.mov'} is missing"])
        self.assertEqual([call.args[0] for call in reveal.call_args_list], [self.outputs / "two.mov", self.outputs / "one.mov"])

    async def test_a_failing_open_is_reported_and_the_app_stays(self) -> None:
        execution_id = self.add("walk", 0, outputs=("one.mov",))
        (self.outputs / "one.mov").write_bytes(b"video")
        path = self.outputs / "one.mov"
        app = self.app()
        failures = (FileNotFoundError(2, "No such file or directory", "open"), subprocess.CompletedProcess(["open"], 1, "", "LSOpenURLsWithRole() failed\n"))
        for failure in failures:
            with self.subTest(failure=failure):
                run = mock.Mock(side_effect=failure) if isinstance(failure, Exception) else mock.Mock(return_value=failure)
                with mock.patch("draw_things_control.tui.history.subprocess.run", run):
                    async with app.run_test(size=(140, 40)) as pilot:
                        await self.settle(pilot)
                        await self.command(pilot, f"/reveal {execution_id}")
                        [message] = self.since(f"/reveal {execution_id}")
                        self.assertTrue(app.is_running)
                app = self.app()
                self.assertEqual(run.call_args.args[0], ["open", "-R", str(path)])
                self.assertTrue(message.startswith(f"Cannot reveal {path}: "), message)
        self.assertIn("open exited with 1: LSOpenURLsWithRole() failed", message)

    async def test_brackets_in_a_filter_are_shown_as_written(self) -> None:
        self.add("walk", 0)
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            table = app.screen.query_one(HistoryPane)
            titles = []
            for text in ("[/]", "[bold]x"):
                await self.command(pilot, f"/filter name '{text}'")
                # The getter gives the title back as markup; read as markup, it must be the text as typed.
                titles.append(Text.from_markup(table.border_title).plain)
            self.assertTrue(app.is_running)
        self.assertEqual(titles, ["History: name [/]", "History: name [bold]x"])

    async def test_an_unreadable_row_is_reported_and_the_pane_keeps_working(self) -> None:
        execution_id = self.add("walk", 0)
        connection = sqlite3.connect(self.state / "dtc.db")
        connection.execute("UPDATE executions SET settings = '{' WHERE id = ?", (execution_id,))
        connection.commit()
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            table = app.screen.query_one(HistoryPane)
            subtitle = Text.from_markup(table.border_subtitle).plain
            detail = await self.detail(pilot, execution_id)
            self.assertTrue(app.is_running)
            self.assertFalse(app.screen.history.reading)
            connection.execute("UPDATE executions SET settings = '{}' WHERE id = ?", (execution_id,))
            connection.commit()
            await self.command(pilot, "/history")
            rows = self.columns(app, 1)
        connection.close()
        self.assertTrue(subtitle.startswith(f"Cannot read the state database {self.state / 'dtc.db'}: "), subtitle)
        self.assertTrue(detail.startswith("Cannot read the state database"), detail)
        self.assertEqual(rows, [["walk"]])

    async def test_a_stale_page_is_dropped_and_does_not_end_the_loading(self) -> None:
        self.add("walk", 0)
        self.add("wave", 10)
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            screen = app.screen
            assert isinstance(screen, MainScreen)
            stale = screen.history.read_count
            with mock.patch.object(HistoryPane, "read_page"):
                await self.command(pilot, "/filter name walk")
                self.assertEqual(self.history(app), [])
                self.assertTrue(screen.history.reading)
                # The unfiltered read that was in flight when the filter changed arrives late.
                screen.history.show_page(stale, HistoryFilter(), HistoryPage(0, [{"id": 99, "job_name": "old", "status": "succeeded", "started_at": at(0), "total_runs": 1, "succeeded": 1}], True))
                self.assertEqual(self.history(app), [])
                self.assertTrue(screen.history.reading)
            await self.command(pilot, "/history")
            rows = self.columns(app, 1)
        self.assertEqual(rows, [["walk"]])

    async def test_a_job_another_process_starts_later_is_polled_even_when_filtered(self) -> None:
        self.add("old", 0)
        app = self.app()
        with mock.patch("draw_things_control.tui.panes.HISTORY_POLL_SECONDS", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                await self.command(pilot, "/filter status succeeded")
                lock = RunLock("run-job")
                lock.acquire()
                self.addCleanup(lock.release)
                running = self.add("walk", 5, status=None, runs=("running",))
                await self.command(pilot, "/filter off")
                await self.wait_for(pilot, lambda: self.columns(app, 1, 2)[:1] == [["walk", "running"]], "the new execution")
                await self.command(pilot, "/filter status succeeded")
                store = self.store()
                store.finish_run(running, 1, status="succeeded", exit_code=0, seconds=1.0, output="walk-1.mov", last_frame=None)
                store.finish_execution(running, status="succeeded", exit_code=0, signal=None, finished_at=at(6))
                lock.release()
                await self.wait_for(pilot, lambda: self.columns(app, 1, 2) == [["walk", "succeeded"], ["old", "succeeded"]], "the finished execution under the filter")

    async def test_one_store_is_opened_and_closed_with_the_screen(self) -> None:
        self.add("walk", 0)
        app = self.app()
        with mock.patch("draw_things_control.tui.history.Store", wraps=Store) as opened, mock.patch.object(HistoryReader, "close", autospec=True, side_effect=HistoryReader.close) as close:
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                for line in ("/history", "/execution 1", "/filter name w", "/filter off"):
                    await self.command(pilot, line)
        self.assertEqual(opened.call_count, 1)
        close.assert_called_once()
