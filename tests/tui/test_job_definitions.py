"""The Job Definition widget, job IDs, and the blank lines in Messages (Milestone 10)."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from rich.text import Text

from draw_things_control.core.run_lock import RunLockError
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.tui import job_files
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.job_files import JobCatalog, JobRow
from draw_things_control.tui.job_watch import JobWatcher
from draw_things_control.tui.panes import JobDefinitionPane
from draw_things_control.tui.screens import ConfirmScreen
from draw_things_control.tui.text import changed_text, job_display_names, sort_job_rows
from draw_things_control.tui.widgets import MessageLog
from tests.fixtures import job_data
from tests.tui.test_tui import make_service
from tests.tui.tui_case import TuiTestCase

# One prompt pair for every run, so any run_count is valid.
ONE_PAIR = [{"name": "only", "positive": "walk"}]


class JobDefinitionTests(TuiTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The test's data directory stands for the project's data/jobs/, the one directory whose files get IDs.
        patcher = mock.patch("draw_things_control.core.generation_config.JOBS_DIRECTORY", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, *, data: Path | None = None) -> DrawThingsApp:
        return self.make_app(make_service(frozenset()), data=data)

    def touch(self, path: Path, when: float) -> None:
        os.utime(path, (when, when))

    @staticmethod
    def rows(app: DrawThingsApp) -> list[list[str]]:
        table = app.screen.query_one(JobDefinitionPane)
        return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]

    async def test_each_file_name_keeps_its_id_and_a_new_name_takes_the_next(self) -> None:
        walk, wave = self.write_data_job("walk.yaml"), self.write_data_job("wave.yml")
        self.touch(walk, 1_000_000)
        self.touch(wave, 2_000_000)
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            first = self.rows(pilot.app)
            table = pilot.app.screen.query_one(JobDefinitionPane)
            title = (table.border_title, str(table.border_subtitle))
        # Newest first by default; the IDs follow the file names' order when first seen.
        self.assertEqual([row[:2] for row in first], [["J0002", "wave"], ["J0001", "walk"]])
        self.assertEqual(first[0][3:], ["i2v", "5"])
        self.assertEqual(title, ("Job Definition", "by changed, newest first"))
        # Deleted and a new name added: the new name takes the next number, and the deleted one's comes back with it.
        wave.unlink()
        self.write_data_job("run.yaml")
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            second = sorted(row[:2] for row in self.rows(pilot.app))
        self.assertEqual(second, [["J0001", "walk"], ["J0003", "run"]])
        self.write_data_job("wave.yml")
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            third = sorted(row[:2] for row in self.rows(pilot.app))
            await self.command(pilot, "/get jobs")
            [listing] = self.since("/get jobs")
        self.assertEqual(third, [["J0001", "walk"], ["J0002", "wave"], ["J0003", "run"]])
        self.assertIn("  J0002  wave.yml  ", listing)

    async def test_an_id_finds_its_job_in_any_case_and_an_unknown_one_says_so(self) -> None:
        self.write_data_job("walk.yaml")
        self.write_data_job("bad.yaml", mode="t2i")
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            rows = {row[1]: row for row in self.rows(pilot.app)}
            for line in ("/describe job j2", "/describe job J0002", "/describe j2", "/describe J0002", "/describe job walk"):
                await self.command(pilot, line)
            await self.command(pilot, "/apply J9")
            await self.command(pilot, "/apply j0002")
            await self.wait_for(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen), "the run confirmation")
            await pilot.press("n")
        # Listed by file name when first seen: bad.yaml is J0001. An invalid file keeps its ID; its mode and runs read invalid.
        self.assertEqual(rows["bad"][0::3], ["J0001", "invalid"])
        self.assertEqual(rows["bad"][4], "invalid")
        self.assertEqual(rows["walk"][0], "J0002")
        for line in ("/describe job j2", "/describe job J0002", "/describe j2", "/describe J0002", "/describe job walk"):
            shown = "\n".join(self.since(line))
            self.assertTrue(shown.startswith(f"Job ID: J0002\nJob file: {self.data / 'walk.yaml'}"), shown)
        self.assertEqual(self.since("/apply J9")[:1], [f"No job file has the ID J0009 in {self.data}"])

    async def test_the_sort_follows_the_keys_and_the_command_and_is_kept(self) -> None:
        for name, when in (("b.yaml", 3_000_000), ("a.yaml", 1_000_000), ("c.yaml", 2_000_000)):
            self.touch(self.write_data_job(name, run_count=int(when // 1_000_000), prompt_pairs=ONE_PAIR), when)
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            table = pilot.app.screen.query_one(JobDefinitionPane)
            orders = [[row[1] for row in self.rows(pilot.app)]]
            table.focus()
            await pilot.press("down")
            selected = table.selected
            await pilot.press("s")
            await self.settle(pilot)
            orders.append([row[1] for row in self.rows(pilot.app)])
            after_sort = table.selected
            await pilot.press("r")
            await self.settle(pilot)
            orders.append([row[1] for row in self.rows(pilot.app)])
            await self.command(pilot, "/sort jobs runs")
            orders.append([row[1] for row in self.rows(pilot.app)])
            await self.command(pilot, "/sort jobs name desc")
            orders.append([row[1] for row in self.rows(pilot.app)])
            subtitle = str(table.border_subtitle)
        # Changed (newest first); then the next key, mode, where every job ties and the ID decides, both ways; runs
        # ascending; name descending.
        self.assertEqual(orders, [["b", "c", "a"], ["a", "b", "c"], ["a", "b", "c"], ["a", "c", "b"], ["c", "b", "a"]])
        # The selection stays on the same job after a sort.
        assert selected is not None and after_sort is not None
        self.assertEqual(after_sort.path, selected.path)
        self.assertEqual(subtitle, "by name, descending")
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            kept = [row[1] for row in self.rows(pilot.app)]
        self.assertEqual(kept, ["c", "b", "a"])

    async def test_enter_describes_the_job_and_a_asks_to_run_it(self) -> None:
        self.write_data_job("walk.yaml")
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            table = pilot.app.screen.query_one(JobDefinitionPane)
            table.focus()
            await pilot.press("enter")
            await self.settle(pilot)
            described = "\n".join(self.said)
            await pilot.press("a")
            await self.wait_for(pilot, lambda: isinstance(pilot.app.screen, ConfirmScreen), "the run confirmation")
            # Only y runs; n cancels, and the job did not start.
            await pilot.press("n")
            await self.settle(pilot)
            running = pilot.app.job_running
            await pilot.press("escape")
            focused = pilot.app.focused
        self.assertIn(f"Job ID: J0001\nJob file: {self.data / 'walk.yaml'}", described)
        self.assertFalse(running)
        self.assertIsNot(focused, table)

    async def test_a_new_or_changed_file_shows_at_the_next_check_and_only_it_is_read_again(self) -> None:
        walk = self.write_data_job("walk.yaml", prompt_pairs=ONE_PAIR)
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            table = pilot.app.screen.query_one(JobDefinitionPane)
            self.write_data_job("wave.yaml")
            with mock.patch.object(job_files, "read_job", wraps=job_files.read_job) as read:
                table.load()
                await self.settle(pilot)
            names = sorted(row[1] for row in self.rows(pilot.app))
            read_again = [call.args[0].name for call in read.call_args_list]
            walk.write_text(walk.read_text(encoding="utf-8").replace("run_count: 5", "run_count: 6"), encoding="utf-8")
            self.touch(walk, walk.stat().st_mtime + 10)
            table.load()
            await self.settle(pilot)
            runs = {row[1]: row[4] for row in self.rows(pilot.app)}
        self.assertEqual(names, ["walk", "wave"])
        self.assertEqual(read_again, ["wave.yaml"])
        self.assertEqual(runs["walk"], "6")

    async def test_another_data_directory_lists_its_files_without_ids_and_writes_nothing(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        self.write_job(job_data(), name="elsewhere/walk.yaml")
        async with self.app(data=other).run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            rows = self.rows(pilot.app)
            await self.command(pilot, "/apply J1")
        self.assertEqual([row[:2] for row in rows], [["-", "walk"]])
        self.assertEqual(self.since("/apply J1"), [f"No job file has the ID J0001 in {other}"])
        self.assertFalse(self.state.exists())


class MessagesSpacingTests(TuiTestCase):
    async def test_blocks_are_set_apart_by_one_blank_line_and_never_at_the_top(self) -> None:
        self.write_data_job("walk.yaml")
        async with self.make_app(make_service(frozenset())).run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            log = pilot.app.screen.query_one(MessageLog)
            await self.command(pilot, "/clear")
            log.say("first line")
            log.say("second line")
            log.say(Text("a table\nof two lines\n\n"))
            log.say("after the table")
            log.say(Text("> /help"), block=True)
            log.say("the answer")
            lines = [strip.text.rstrip() for strip in log.lines]
        # The time (HH:MM:SS and a space) leads each message's first line only.
        bodies = [line[9:] if line[2:3] == ":" and line[5:6] == ":" else line for line in lines]
        # No blank line at the top after /clear; one before and after a message of several lines, and before a block.
        self.assertEqual(bodies, ["first line", "second line", "", "a table", "of two lines", "", "after the table", "", "> /help", "the answer"])


class JobDefinitionTextTests(TuiTestCase):
    def test_names_times_and_sorting(self) -> None:
        self.assertEqual(job_display_names([Path("walk.yaml"), Path("walk.yml"), Path("wave.yaml")]), {Path("walk.yaml"): "walk.yaml", Path("walk.yml"): "walk.yml", Path("wave.yaml"): "wave"})
        now = datetime(2026, 9, 26, 12, 0).astimezone()
        self.assertEqual(changed_text(datetime(2026, 9, 26, 14, 5).timestamp(), now), "09-26 14:05")
        self.assertEqual(changed_text(datetime(2025, 9, 26, 14, 5).timestamp(), now), "2025-09-26")
        self.assertEqual(changed_text(None, now), "-")
        job = self.load(run_count=2)
        rows: list[Any] = [JobRow(Path("b.yaml"), None, "bad", number=1, changed=3.0), JobRow(Path("a.yaml"), job, number=3, changed=1.0), JobRow(Path("c.yaml"), self.load(run_count=4), number=2, changed=1.0)]
        # Ties fall back to the ID; invalid files come after valid ones by runs and mode, in both directions.
        self.assertEqual([row.path.name for row in sort_job_rows(rows, "changed", True)], ["b.yaml", "c.yaml", "a.yaml"])
        self.assertEqual([row.path.name for row in sort_job_rows(rows, "runs", False)], ["a.yaml", "c.yaml", "b.yaml"])
        self.assertEqual([row.path.name for row in sort_job_rows(rows, "runs", True)], ["c.yaml", "a.yaml", "b.yaml"])
        self.assertEqual([row.path.name for row in sort_job_rows(rows, "id", True)], ["a.yaml", "c.yaml", "b.yaml"])

    def load(self, **changes: object) -> Any:
        return load_job(self.write_job(job_data(prompt_pairs=ONE_PAIR, **changes)), self.global_config, self.params)


class JobCatalogTests(TuiTestCase):
    """The catalog behind the widget: validity that follows a job's inputs, finding a job, and errors that never escape."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("draw_things_control.core.generation_config.JOBS_DIRECTORY", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.catalog = JobCatalog(self.data, self.global_config)
        self.addCleanup(self.catalog.close)

    def valid(self) -> dict[str, bool]:
        return {row.path.name: row.job is not None for row in self.catalog.read().rows}

    def test_a_job_is_read_again_when_its_input_or_its_configuration_changes(self) -> None:
        self.write_data_job("walk.yaml")
        self.assertEqual(self.valid(), {"walk.yaml": True})
        image = self.input_directory / "first-frame.png"
        moved = image.with_name("away.png")
        image.rename(moved)
        # The job file did not change, but its input is gone: it is invalid at the next check, and valid again when it returns.
        self.assertEqual(self.valid(), {"walk.yaml": False})
        moved.rename(image)
        self.assertEqual(self.valid(), {"walk.yaml": True})
        configuration = self.params / "base.yaml"
        text = configuration.read_text(encoding="utf-8")
        configuration.write_text("{}\n", encoding="utf-8")
        self.assertEqual(self.valid(), {"walk.yaml": False})
        configuration.write_text(text, encoding="utf-8")
        self.assertEqual(self.valid(), {"walk.yaml": True})

    def test_a_valid_unchanged_job_is_not_read_again_and_a_deleted_one_is_forgotten(self) -> None:
        walk = self.write_data_job("walk.yaml")
        self.catalog.read()
        with mock.patch.object(job_files, "read_job", wraps=job_files.read_job) as read:
            self.catalog.read()
            self.assertEqual(read.call_count, 0)
            self.catalog.read(fresh=True)
            self.assertEqual(read.call_count, 1)
        walk.unlink()
        self.catalog.read()
        self.assertEqual(self.catalog._cache, {})

    def test_a_name_that_is_the_only_spelling_of_an_id_is_still_an_error(self) -> None:
        # From J10000 up, every spelling of the ID is the same text.
        with mock.patch.object(self.catalog, "_find_file", return_value=self.data / "a.yaml"), mock.patch.object(self.catalog, "_find_id", return_value=self.data / "b.yaml"):
            self.assertEqual(self.catalog.find("J10000", []), "'J10000' is both the job file a.yaml and the job ID J10000 (b.yaml); type a.yaml or J10000")

    def test_a_job_id_is_found_before_the_list_is_read_and_a_name_that_is_both_is_an_error(self) -> None:
        self.write_data_job("a.yaml")
        self.write_data_job("j1.yaml")
        self.write_data_job("J0002.yaml")
        rows = self.catalog.read().rows
        numbers = {row.path.name: row.number for row in rows}
        # Listed by file name: J0002.yaml is J0001, a.yaml J0002, j1.yaml J0003.
        self.assertEqual(numbers, {"J0002.yaml": 1, "a.yaml": 2, "j1.yaml": 3})
        # Without the rows (as right after the TUI opens), the store answers.
        self.assertEqual(self.catalog.find("j2", []), self.data / "a.yaml")
        self.assertEqual(self.catalog.find("J0003"), self.data / "j1.yaml")
        # j1 is the file j1.yaml, and also the ID J0001 of another file: neither runs, and both are named.
        self.assertEqual(self.catalog.find("j1", rows), "'j1' is both the job file j1.yaml and the job ID J0001 (J0002.yaml); type j1.yaml or J0001")
        self.assertEqual(self.catalog.find("J0002", rows), "'J0002' is both the job file J0002.yaml and the job ID J0002 (a.yaml); type J0002.yaml or J2")
        # A full file name is never an ID; j3 is j1.yaml's own ID, so there is nothing to choose between.
        self.assertEqual(self.catalog.find("j1.yaml", rows), self.data / "j1.yaml")
        self.assertEqual(self.catalog.find("j3", rows), self.data / "j1.yaml")
        (self.data / "a.yaml").unlink()
        self.assertEqual(self.catalog.find("j2", []), f"J0002 is a.yaml, which is no longer in {self.data}")
        self.assertEqual(self.catalog.find("J9", rows), f"No job file has the ID J0009 in {self.data}")

    def test_an_unusable_state_directory_is_reported_and_never_raises(self) -> None:
        self.write_data_job("walk.yaml")
        with mock.patch.object(job_files, "Store", side_effect=RunLockError("Cannot create the state directory")):
            listing = self.catalog.read()
            self.assertEqual(listing.id_error, "Cannot give job IDs: Cannot create the state directory")
            self.assertEqual([row.number for row in listing.rows], [None])
            self.assertEqual(self.catalog.keep_sort("name", False, 1), "Cannot keep the sort: Cannot create the state directory")
            self.assertEqual(self.catalog.find("J1"), f"No job file has the ID J0001 in {self.data}")
        self.assertEqual(self.catalog.sort(), ("changed", True))

    def test_a_later_sort_choice_is_never_overwritten_by_an_earlier_one(self) -> None:
        self.assertIsNone(self.catalog.keep_sort("name", False, 2))
        # The first choice's save arrives last: it is dropped.
        self.assertIsNone(self.catalog.keep_sort("runs", True, 1))
        self.assertEqual(self.catalog.sort(), ("name", False))


class JobDefinitionPaneTests(JobDefinitionTests):
    async def test_a_choice_made_before_the_saved_sort_arrives_wins_and_an_unchanged_check_does_not_redraw(self) -> None:
        self.write_data_job("walk.yaml")
        async with self.app().run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            table = pilot.app.screen.query_one(JobDefinitionPane)
            table.set_sort("name", False)
            # The saved sort (the default here) lands late.
            table.apply_sort("changed", True)
            await self.settle(pilot)
            sort = (table.sort_key, table.descending)
            with mock.patch.object(table, "clear", wraps=table.clear) as clear:
                table.load()
                await self.settle(pilot)
                redraws = clear.call_count
        self.assertEqual(sort, ("name", False))
        self.assertEqual(redraws, 0)


class JobWatcherTests(JobDefinitionTests):
    def wait_until(self, condition: Any, what: str, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.02)

    def test_a_job_file_change_is_reported_and_other_files_and_reads_are_not(self) -> None:
        seen = threading.Event()
        watcher = JobWatcher(self.data, seen.set)
        self.addCleanup(watcher.stop)
        image = self.data / "images" / "in.png"
        image.parent.mkdir()
        image.write_bytes(b"x")
        self.assertTrue(watcher.follow([image]))
        time.sleep(0.3)
        seen.clear()
        (self.data / ".swap.yaml").write_text("x")
        (self.data / "notes.txt").write_text("x")
        self.data.joinpath("notes.txt").read_text()
        self.assertFalse(seen.wait(0.5))
        (self.data / "new.yaml").write_text("x")
        self.wait_until(seen.is_set, "a new job file is reported")
        seen.clear()
        image.write_bytes(b"changed")
        self.wait_until(seen.is_set, "a followed input is reported")

    def test_a_missing_directory_is_not_complete(self) -> None:
        watcher = JobWatcher(self.data, lambda: None)
        self.addCleanup(watcher.stop)
        self.assertFalse(watcher.follow([self.data / "gone" / "in.png"]))
        self.assertTrue(watcher.follow([]))


class JobDefinitionWatchTests(JobDefinitionTests):
    async def test_a_new_file_shows_without_a_timer_and_the_timer_pauses_while_all_jobs_are_valid(self) -> None:
        self.write_data_job("walk.yaml")
        with mock.patch("draw_things_control.tui.panes.JOBS_POLL_SECONDS", 3600):
            async with self.app().run_test(size=(160, 50)) as pilot:
                await self.settle(pilot)
                table = pilot.app.screen.query_one(JobDefinitionPane)
                assert table.check_timer is not None
                paused = table.check_timer._active.is_set() is False
                self.write_data_job("wave.yaml")
                await self.wait_for(pilot, lambda: table.row_count == 2, "the new file is listed")
        self.assertTrue(paused)
