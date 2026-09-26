"""Headless tests of the history pane, the execution detail, and reveal, over a temporary state store."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

from rich.text import Text
from textual.content import Content

from draw_things_control.core.global_config import CooldownPolicy
from draw_things_control.core.run_lock import RunLock, ensure_state_directory
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_events import CooldownStarted
from draw_things_control.state.store import Store
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.history import HistoryFilter, HistoryPage, HistoryReader, copy_to_pasteboard
from draw_things_control.tui.panes import ExecutionBody, ExecutionPane, HistoryPane
from draw_things_control.tui.screens import MainScreen
from draw_things_control.tui.text import confirm_run_text, event_text, parameters_text, reveal_action, stored_cooldown_text
from draw_things_control.tui.widgets import CommandInput
from tests.fixtures import job_data
from tests.tui.fake_runs import FakeRuns
from tests.tui.tui_case import TuiTestCase

START = datetime(2026, 9, 20, 9, 0).astimezone()
# A saved Wan command: its configuration asks for 1000x600 and 17 frames, which the detail never shows.
WAN_COMMAND = [
    "draw-things-cli", "generate", "--model", "wan_v2.2_a14b_hne_i2v_q8p.ckpt", "--prompt", "a walk", "--width", "1000", "--height", "600",
    "--config-json", json.dumps({"refinerModel": "wan_v2.2_a14b_lne_i2v_q8p.ckpt", "refinerStart": 0.1, "guidanceScale": 5, "shift": 3.99, "steps": 40, "numFrames": 17}),
]  # fmt: skip


def at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class HistoryCase(TuiTestCase):
    """A temporary state store and executions added to it, for the history and the detail."""

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

    def add(self, name: str, minutes: int, *, status: str | None = "succeeded", runs: tuple[str, ...] = ("succeeded",), job_file: str | None = None, outputs: tuple[str | None, ...] | None = None, command: list[str] | None = None, measured: tuple[int, int, int] | None = None, mode: str = "i2v", seconds: float = 12.5) -> int:
        """An execution with its runs; ``status`` None leaves it running. Output files are named after the job and run.

        ``measured`` is the size and frame count recorded for each successful run; without it, the run is as old rows are.
        """
        store = self.store()
        execution_id = store.start_execution(job_name=name, job_file=job_file or f"/jobs/{name}.yaml", mode=mode, model="base.ckpt", seed=42, seed_source="config_file", cooldown_seconds=5.0, cooldown_source="job", total_runs=len(runs), started_at=at(minutes), job_yaml=f"name: {name}\n", settings={"output_directory": str(self.outputs)})
        for number, run_status in enumerate(runs, start=1):
            output = outputs[number - 1] if outputs is not None else f"{name}-{number}.mov"
            store.start_run(execution_id, number, pair="walk", positive="a walk [slow]", negative="blurry", input="in.png", output=output, last_frame=None, command=command or ["draw-things-cli", "generate", "--api-key", "[redacted]"], started_at=at(minutes))
            if run_status != "running":
                size = measured if measured is not None and run_status == "succeeded" else (None, None, None)
                store.finish_run(execution_id, number, status=run_status, exit_code=0 if run_status == "succeeded" else 1, seconds=seconds, output=output, last_frame=None, output_width=size[0], output_height=size[1], output_frames=size[2])
        if status is not None:
            store.finish_execution(execution_id, status=status, exit_code=0 if status == "succeeded" else 1, signal=None, finished_at=at(minutes + 1))
        return execution_id

    def columns(self, app: DrawThingsApp, *indexes: int) -> list[list[str]]:
        return [[row[index] for index in indexes] for row in self.history(app)]

    async def detail(self, pilot: Any, execution_id: int) -> str:
        await self.command(pilot, f"/execution {execution_id}")
        return "\n".join(self.since(f"/execution {execution_id}"))


class HistoryTests(HistoryCase):
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
                await self.command(pilot, "/get history")
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
            unknown = await self.detail(pilot, 99)
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
        with mock.patch("draw_things_control.tui.history.reveal_in_finder", reveal):
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
        with mock.patch("draw_things_control.tui.history.reveal_in_finder", reveal):
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
            await self.command(pilot, "/get history")
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
            await self.command(pilot, "/get history")
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
                for line in ("/get history", "/execution 1", "/filter name w", "/filter off"):
                    await self.command(pilot, line)
        self.assertEqual(opened.call_count, 1)
        close.assert_called_once()


class ExecutionDetailTests(HistoryCase):
    """The detail widget under the history: it follows the cursor, lists successful runs, and reveals their outputs."""

    @staticmethod
    def detail_pane(app: DrawThingsApp) -> ExecutionPane:
        return app.screen.query_one(ExecutionPane)

    def detail_text(self, app: DrawThingsApp) -> list[str]:
        return str(app.screen.query_one(ExecutionBody).content).split("\n")

    async def shown(self, pilot: Any, execution_id: int) -> None:
        await self.wait_for(pilot, lambda: self.detail_pane(pilot.app).execution_id == execution_id, f"execution {execution_id} in the detail")
        await pilot.pause()

    async def test_the_detail_follows_the_cursor_and_lists_only_successful_runs_with_measured_values(self) -> None:
        older = self.add("walk", 0, runs=("succeeded", "failed", "succeeded"), status="failed", command=WAN_COMMAND, measured=(832, 448, 81), seconds=432.1, outputs=("one.mov", "two.mov", "three.mov"))
        newer = self.add("wave", 30, runs=("interrupted",), status="interrupted", command=WAN_COMMAND)
        for name in ("one.mov", "three.mov"):
            (self.outputs / name).write_bytes(b"video")
        app = self.app()
        # Wide enough for the whole names; cutting them is tested below.
        async with app.run_test(size=(200, 40)) as pilot:
            await self.settle(pilot)
            await self.shown(pilot, newer)
            first = self.detail_text(app)
            title = Content.from_markup(str(self.detail_pane(app).border_title)).plain
            table = app.screen.query_one(HistoryPane)
            table.focus()
            reads = mock.patch.object(ExecutionPane, "load", autospec=True, side_effect=ExecutionPane.load)
            with reads as load:
                await pilot.press("down")
                await self.shown(pilot, older)
            second = self.detail_text(app)
        self.assertEqual(title, f"Execution {newer}: wave interrupted")
        self.assertEqual(first, ["model wan_v2.2_a14b_hne_i2v_q8p.ckpt", "refiner wan_v2.2_a14b_lne_i2v_q8p.ckpt from 10%", "size -  CFG 5  shift 3.99", "No run finished successfully"])
        # Run 2 failed, so only runs 1 and 3 are listed; the size and frames are measured, never the 1000x600 and 17 asked for.
        self.assertEqual([line.rstrip() for line in second], ["model wan_v2.2_a14b_hne_i2v_q8p.ckpt", "refiner wan_v2.2_a14b_lne_i2v_q8p.ckpt from 10%", "832x448  CFG 5  shift 3.99", "#  Frames Steps Time", "1  81     40    7 min 12 s", "   one.mov", "3  81     40    7 min 12 s", "   three.mov"])
        self.assertNotIn("1000", "\n".join(second))
        self.assertNotIn(" 17 ", "\n".join(second))
        load.assert_called_once()

    async def test_old_runs_images_missing_files_and_long_names(self) -> None:
        old = self.add("old", 0, runs=("succeeded",), outputs=("gone [1].mov",))
        image = self.add("still", 10, mode="i2i", runs=("succeeded", "succeeded"), measured=(1024, 576, 0), outputs=("a" * 60 + ".png", "b.png"))
        (self.outputs / ("a" * 60 + ".png")).write_bytes(b"png")
        (self.outputs / "b.png").write_bytes(b"png")
        store = self.store()
        store.finish_run(image, 2, status="succeeded", exit_code=0, seconds=1.0, output="b.png", last_frame=None, output_width=512, output_height=512, output_frames=None)
        app = self.app()
        async with app.run_test(size=(80, 30)) as pilot:
            await self.settle(pilot)
            await self.shown(pilot, image)
            images = self.detail_text(app)
            width = self.detail_pane(app).scrollable_content_region.width
            await self.command(pilot, f"/execution {old}")
            app.screen.query_one(HistoryPane).focus()
            await pilot.press("down")
            await self.shown(pilot, old)
            olds = self.detail_text(app)
        self.assertEqual(images[2], "sizes vary  CFG -  shift -")
        # An image has no frames; a long file name is cut in the middle, keeping its suffix.
        self.assertTrue(images[4].startswith("1  -      -     12 s"), images[4])
        self.assertTrue(images[5].strip().startswith("aaa") and images[5].rstrip().endswith("aaa.png") and "…" in images[5], images[5])
        self.assertLessEqual(max(len(line) for line in images), width)
        self.assertEqual(olds[2], "size -  CFG -  shift -")
        self.assertEqual(olds[4].rstrip(), "1  -      -     12 s")
        self.assertEqual(olds[5].strip(), "gone [1].mov (missing)")

    async def test_enter_moves_to_the_detail_and_the_keys_choose_and_reveal_a_run(self) -> None:
        execution_id = self.add("walk", 0, runs=("succeeded", "succeeded", "succeeded"), outputs=("one.mov", "two.mov", "three.mov"), measured=(832, 448, 81))
        for name in ("one.mov", "three.mov"):
            (self.outputs / name).write_bytes(b"video")
        app = self.app()
        reveal = mock.Mock(return_value=None)
        with mock.patch("draw_things_control.tui.history.reveal_in_finder", reveal):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                await self.shown(pilot, execution_id)
                detail = self.detail_pane(app)
                app.screen.query_one(HistoryPane).focus()
                await pilot.press("enter")
                focused_after_enter, selected = app.focused, detail.selected
                await pilot.press("down", "down", "down")
                last = detail.selected
                await pilot.press("home", "enter")
                await self.settle(pilot)
                await pilot.press("down", "enter")
                await self.settle(pilot)
                moved = detail.selected
                await pilot.press("escape")
                after_escape = app.focused
                app.screen.query_one(HistoryPane).focus()
                await pilot.press("escape")
                history_escape = app.focused
                # A run that succeeds while the widget shows it keeps the selection where it was.
                detail.select(3)
                detail.follow(execution_id, pause=False)
                await self.settle(pilot)
                kept = detail.selected
                messages = [line for line in self.said if "Revealed" in line or "missing" in line]
        self.assertIs(focused_after_enter, detail)
        self.assertEqual((selected, last, moved, kept), (1, 3, 2, 3))
        self.assertIsInstance(after_escape, CommandInput)
        self.assertIsInstance(history_escape, CommandInput)
        self.assertEqual([call.args[0] for call in reveal.call_args_list], [self.outputs / "one.mov"])
        self.assertEqual(messages, [f"Revealed {self.outputs / 'one.mov'}", f"{self.outputs / 'two.mov'} is missing"])
        # Only /execution writes the full detail to Messages; Enter did not.
        self.assertFalse(any(line.startswith("Execution ") for line in self.said))

    async def test_tab_reaches_the_detail_after_the_history(self) -> None:
        self.add("walk", 0)
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            focused = []
            for _ in range(3):
                await pilot.press("tab")
                focused.append(type(app.focused).__name__)
        self.assertEqual(focused, ["HistoryPane", "ExecutionPane", "CommandInput"])

    async def test_a_click_on_a_file_name_reveals_it_and_a_click_on_a_run_selects_it(self) -> None:
        execution_id = self.add("walk", 0, runs=("succeeded", "succeeded"), outputs=("one [x].mov", "two.mov"), measured=(832, 448, 81))
        for name in ("one [x].mov", "two.mov"):
            (self.outputs / name).write_bytes(b"video")
        app = self.app()
        reveal = mock.Mock(return_value=None)
        with mock.patch("draw_things_control.tui.history.reveal_in_finder", reveal):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(pilot)
                await self.shown(pilot, execution_id)
                detail = self.detail_pane(app)
                # Line 6 is run 2's numbers; line 5 is run 1's file name, three columns in.
                await pilot.click(ExecutionBody, offset=(1, 6))
                clicked_run = detail.selected
                await pilot.click(ExecutionBody, offset=(5, 5))
                await self.settle(pilot)
                after_link = detail.selected
        self.assertEqual((clicked_run, after_link), (2, 1))
        self.assertEqual([call.args[0] for call in reveal.call_args_list], [self.outputs / "one [x].mov"])

    def test_the_reveal_action_is_made_of_numbers_only(self) -> None:
        self.assertEqual(reveal_action(12, 3), "reveal(12, 3)")
        with self.assertRaises((TypeError, ValueError)):
            reveal_action("1); app.quit(", 1)  # type: ignore[arg-type]

    async def test_an_empty_history_says_so_in_the_detail(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            await self.wait_for(pilot, lambda: self.detail_text(app) == ["No execution history yet"], "the message")
            self.assertEqual(str(self.detail_pane(app).border_title), "Execution")

    async def test_five_rows_show_even_when_the_history_scrolls_sideways(self) -> None:
        for number in range(7):
            self.add(f"a-long-job-name-{number}", number)
        for size, scrolls in (((80, 30), True), ((200, 40), False)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await self.settle(pilot)
                    await pilot.pause()
                    table = app.screen.query_one(HistoryPane)
                    shown, height = table.show_horizontal_scrollbar, table.region.height
                    execution_top = app.screen.query_one(ExecutionPane).region.y
                # The border, the header, 5 rows, and a line for the sideways scrollbar when there is one.
                self.assertEqual((shown, height, execution_top), (scrolls, 9 if scrolls else 8, height))

    async def test_the_execution_command_shows_the_settings_steps_and_measured_output(self) -> None:
        execution_id = self.add("walk", 0, runs=("succeeded", "failed"), status="failed", command=WAN_COMMAND, measured=(832, 448, 81))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            detail = await self.detail(pilot, execution_id)
        for line in ("  refiner: wan_v2.2_a14b_lne_i2v_q8p.ckpt from 10%", "  CFG: 5", "  shift: 3.99", "  steps: 40, output 832x448, 81 frames", "  steps: 40, output not measured"):
            self.assertIn(line, detail)
        # /execution lists every run, the failed one too.
        self.assertIn("Run 2 failed", detail)


class GetCommandTests(HistoryCase):
    """/get prompts, positive, negative, and param, from the stored execution."""

    def add_job(self) -> int:
        """An execution of three runs over two pairs, whose job overrode steps and shift; its command asks --width 832 over a 1000 configuration."""
        store = self.store()
        execution_id = store.start_execution(job_name="walk", job_file="/jobs/walk.yaml", mode="i2v", model="wan.ckpt", started_at=at(0), config_file="wan.yaml", settings={"output_directory": str(self.outputs), "config_override": {"steps": 8, "shift": 3.99}})
        config = json.dumps({"model": "wan.ckpt", "steps": 40, "shift": 3.99, "width": 1000, "faceRestoration": "", "loras": [], "refinerStart": 0.1})
        pairs = (("walk", "a walk [slow]", "blurry"), ("wave", "a wave", None), ("walk", "a walk [slow]", "blurry"))
        for number, (pair, positive, negative) in enumerate(pairs, start=1):
            command = ["draw-things-cli", "generate", "--model", "wan.ckpt", "--prompt", positive, *(["--negative-prompt", negative] if negative else []), "--steps", "8", "--width", "832", "--config-json", config, "--output", f"/out/walk-{number}.mov", "--disable-preview"]
            store.start_run(execution_id, number, pair=pair, positive=positive, negative=negative, started_at=at(0), command=command)
            store.finish_run(execution_id, number, status="succeeded", exit_code=0, seconds=5.0, output=f"walk-{number}.mov", last_frame=None)
        return execution_id

    async def run_lines(self, *lines: str) -> dict[str, str]:
        app = self.app()
        results = {}
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            for line in lines:
                await self.command(pilot, line)
                results[line] = "\n".join(self.since(line))
        return results

    async def test_the_prompts_of_every_pair_or_of_one_run(self) -> None:
        execution_id = self.add_job()
        copied: list[str] = []
        with mock.patch("draw_things_control.tui.screens.copy_to_pasteboard", side_effect=lambda text: copied.append(text)):
            results = await self.run_lines(f"/get prompts {execution_id}", f"/get positive {execution_id}", f"/get negative {execution_id} 2", f"/get negative {execution_id} 1", f"/get prompts {execution_id} 9", "/get positive 99")
        # Each label on its own line after a blank one, its prompt on the next, and a blank line after the prompt.
        self.assertEqual(results[f"/get prompts {execution_id}"], f"Execution {execution_id}: walk\nPair walk (runs 1, 3)\n\npositive:\na walk [slow]\n\nnegative:\nblurry\n\nPair wave (run 2)\n\npositive:\na wave\n\nnegative:\n(none)\n\nCopied the prompts to the clipboard")
        self.assertEqual(results[f"/get positive {execution_id}"], f"Execution {execution_id}: walk\nPair walk (runs 1, 3)\n\npositive:\na walk [slow]\n\nPair wave (run 2)\n\npositive:\na wave\n\nCopied the positive prompts to the clipboard")
        # No prompt, nothing copied.
        self.assertEqual(results[f"/get negative {execution_id} 2"], f"Execution {execution_id}: walk\nRun 2 (pair wave)\n\nnegative:\n(none)\n")
        self.assertEqual(results[f"/get negative {execution_id} 1"], f"Execution {execution_id}: walk\nRun 1 (pair walk)\n\nnegative:\nblurry\n\nCopied the negative prompt to the clipboard")
        self.assertEqual(results[f"/get prompts {execution_id} 9"], f"Execution {execution_id} has no run 9")
        self.assertEqual(results["/get positive 99"], "No execution 99")
        # The prompts alone for one side; labelled for both. A missing prompt is left out.
        self.assertEqual(copied, ["positive:\na walk [slow]\n\nnegative:\nblurry\n\npositive:\na wave", "a walk [slow]\n\na wave", "blurry"])

    async def test_without_pbcopy_the_terminal_is_asked_to_copy(self) -> None:
        execution_id = self.add_job()
        with mock.patch("draw_things_control.tui.screens.copy_to_pasteboard", return_value="pbcopy was not found"), mock.patch.object(DrawThingsApp, "copy_to_clipboard") as terminal:
            results = await self.run_lines(f"/get positive {execution_id} 2")
        terminal.assert_called_once_with("a wave")
        self.assertTrue(results[f"/get positive {execution_id} 2"].endswith("Asked the terminal to copy the positive prompt (pbcopy was not found)"))

    async def test_the_parameters_table_leaves_out_the_prompts_and_marks_what_was_overridden(self) -> None:
        execution_id = self.add_job()
        results = await self.run_lines(f"/get param {execution_id}", f"/get parameters {execution_id} 2")
        table = results[f"/get param {execution_id}"]
        # Every row by its first word; the rules under the headers are all dashes.
        rows = {line.split()[0]: line for line in table.splitlines() if line.startswith("  ") and set(line.strip()) - {"-", " "}}
        self.assertTrue(table.startswith(f"Execution {execution_id}: walk, run 1 of 3: draw-things-cli arguments (configuration wan.yaml)\n"), table)
        self.assertNotIn("--prompt", table)
        self.assertNotIn("a walk", table)
        self.assertNotIn("blurry", table)
        self.assertRegex(rows["--steps"], r"^  --steps\s+8\s+job override; replaces --config-json 40$")
        self.assertRegex(rows["--width"], r"^  --width\s+832\s+replaces --config-json 1000$")
        self.assertRegex(rows["--model"], r"^  --model\s+wan\.ckpt$")
        self.assertRegex(rows["--disable-preview"], r"^  --disable-preview\s+yes$")
        self.assertRegex(rows["--output"], r"^  --output\s+/out/walk-1\.mov$")
        self.assertIn("\n--config-json\n", table)
        self.assertRegex(rows["steps"], r"^  steps\s+40\s+replaced by --steps 8$")
        self.assertRegex(rows["width"], r"^  width\s+1000\s+replaced by --width 832$")
        self.assertRegex(rows["shift"], r"^  shift\s+3\.99\s+job override$")
        self.assertRegex(rows["faceRestoration"], r'^  faceRestoration\s+""$')
        self.assertRegex(rows["loras"], r"^  loras\s+\[\]$")
        self.assertIn("run 2 of 3", results[f"/get parameters {execution_id} 2"])
        self.assertIn("/out/walk-2.mov", results[f"/get parameters {execution_id} 2"])

    async def test_an_execution_without_a_saved_command_says_so(self) -> None:
        execution_id = self.add("old", 0, command=[])
        store = self.store()
        store._connection().execute("UPDATE runs SET command = '[]' WHERE execution_id = ?", (execution_id,))
        results = await self.run_lines(f"/get param {execution_id}", f"/get param {execution_id} 1")
        self.assertEqual(results[f"/get param {execution_id}"], f"Execution {execution_id} has no run with a saved command")
        self.assertEqual(results[f"/get param {execution_id} 1"], f"Run 1 of execution {execution_id} has no saved command")


class ParametersTextTests(unittest.TestCase):
    def test_a_flag_equal_to_its_config_json_value_as_a_number_replaces_nothing(self) -> None:
        config = json.dumps({"guidanceScale": 5, "strength": 1, "steps": 30})
        command = ["draw-things-cli", "generate", "--model", "m.ckpt", "--cfg", "5.0", "--strength", "1.0", "--steps", "20", "--config-json", config]
        execution = {"id": 1, "job_name": "walk", "settings": {"config_override": {"guidance_scale": 5.0}}, "runs": [{"number": 1, "command": command}]}
        table = str(parameters_text(execution, 1))
        self.assertNotIn("replaces --config-json 5", table)
        self.assertNotIn("replaced by --cfg", table)
        self.assertNotIn("replaced by --strength", table)
        # A value that differs is still marked, both ways.
        self.assertIn("replaces --config-json 30", table)
        self.assertIn("replaced by --steps 20", table)


class PasteboardTests(unittest.TestCase):
    def test_the_text_goes_to_pbcopy_as_utf_8_and_failures_say_why(self) -> None:
        done = subprocess.CompletedProcess(["pbcopy"], 0)
        with mock.patch("draw_things_control.tui.history.shutil.which", return_value="/usr/bin/pbcopy"), mock.patch("draw_things_control.tui.history.subprocess.run", return_value=done) as run:
            self.assertIsNone(copy_to_pasteboard("a café [slow]"))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pbcopy"])
        self.assertEqual((run.call_args.kwargs["input"], run.call_args.kwargs["env"]["LC_CTYPE"]), ("a café [slow]".encode(), "UTF-8"))
        with mock.patch("draw_things_control.tui.history.shutil.which", return_value=None):
            self.assertEqual(copy_to_pasteboard("x"), "pbcopy was not found")
        with mock.patch("draw_things_control.tui.history.shutil.which", return_value="/usr/bin/pbcopy"), mock.patch("draw_things_control.tui.history.subprocess.run", return_value=subprocess.CompletedProcess(["pbcopy"], 1)):
            self.assertEqual(copy_to_pasteboard("x"), "pbcopy exited with 1")
