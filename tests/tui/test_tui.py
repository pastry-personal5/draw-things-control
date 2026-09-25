"""Headless tests of the terminal UI, with fake tools and temporary directories."""

from __future__ import annotations

import itertools
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from textual.widgets import Static
from typer.testing import CliRunner

from draw_things_control.cli import app as cli
from draw_things_control.core.global_config import PROJECT_ROOT
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_report import plan_lines
from draw_things_control.jobs.job_service import JobService
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.screens import HelpScreen, JobDetailScreen, JobListScreen
from draw_things_control.tui.widgets import JobTable
from tests.fixtures import JobTestCase, job_data


def make_service(missing: frozenset[str] = frozenset()) -> JobService:
    """A JobService whose tool checks pass unless the tool is in ``missing``, with fixed output names."""
    numbers = itertools.count(1000)

    def require_ffmpeg() -> str:
        if "ffmpeg" in missing:
            raise ValueError("Could not find 'ffmpeg' on PATH")
        return "ffmpeg"

    return JobService(
        runner_factory=mock.Mock(side_effect=AssertionError("the TUI must not start a run")),
        find_executable=lambda executable: None if executable in missing else executable,
        frame_extractor=mock.Mock(),
        require_ffmpeg=require_ffmpeg,
        clock=lambda: datetime(2026, 9, 24, 15, 30, 12),
        random_number=lambda: next(numbers),
        random_seed=mock.Mock(side_effect=AssertionError("the TUI must not draw a seed")),
    )


class TuiTests(JobTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.root / "data"
        self.data.mkdir()
        patcher = mock.patch("draw_things_control.core.generation_config.DT_CONFIG_DIRECTORY", self.dt_config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_data_job(self, name: str, **changes: object) -> Path:
        path = self.data / name
        self.write_job(job_data(**changes), name=f"data/{name}")
        return path

    def app(self, *, data: Path | None = None, executable: str = "draw-things-cli", missing: frozenset[str] = frozenset()) -> DrawThingsApp:
        return DrawThingsApp(settings=self.global_config, data_directory=data or self.data, executable=executable, job_service=make_service(missing))

    @staticmethod
    async def settle(pilot) -> None:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    @staticmethod
    def rows(app: DrawThingsApp) -> list[list[str]]:
        table = app.screen.query_one(JobTable)
        return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]

    @staticmethod
    def text(app: DrawThingsApp, widget_id: str) -> str:
        return str(app.screen.query_one(f"#{widget_id}", Static).content)

    def snapshot(self) -> dict[Path, tuple[int, int]]:
        return {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in self.root.rglob("*")}

    async def test_the_list_shows_every_job_file_and_its_status(self) -> None:
        self.write_data_job("b-walk.yaml")
        self.write_data_job("a-still.yml", mode="i2i", run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])
        self.write_data_job("c-bad.yaml", mode="t2i")
        self.write_data_job("d-bad-pairs.yaml", run_count=2)
        (self.data / ".hidden.yaml").write_text("not: a job\n", encoding="utf-8")
        (self.data / "notes.txt").write_text("not a job\n", encoding="utf-8")
        for directory in (".trash", "nested"):
            (self.data / directory).mkdir()
            self.write_data_job(f"{directory}/e-inside.yaml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            rows = self.rows(app)
        self.assertEqual([row[0] for row in rows], ["a-still.yml", "b-walk.yaml", "c-bad.yaml", "d-bad-pairs.yaml"])
        self.assertEqual(rows[0][1:], ["sunset-walk", "i2i", "1", "valid"])
        self.assertEqual(rows[1][1:], ["sunset-walk", "i2v", "5", "valid"])
        self.assertTrue(rows[2][4].startswith("invalid: 'mode'"), rows[2])
        self.assertTrue(rows[3][4].startswith("invalid: 'prompt_pairs[0].runs'"), rows[3])
        self.assertNotIn(str(self.data), rows[3][4])

    async def test_brackets_in_errors_and_paths_are_shown_as_written(self) -> None:
        (self.data / "broken.yaml").write_text("name: x\npositive: a cat [smiling]: [/sad]\n", encoding="utf-8")
        self.write_data_job("[b] walk.yaml")
        # A table cell shows only its first line, so this markup must be there: the unknown key is quoted in it.
        self.write_data_job("c-key.yaml", **{"[/sad]": 1})
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            rows = self.rows(app)
            await pilot.press("j", "enter")
            await self.settle(pilot)
            summary = self.text(app, "summary")
        self.assertEqual([row[0] for row in rows], ["[b] walk.yaml", "broken.yaml", "c-key.yaml"])
        self.assertIn("a cat [smiling]: [/sad]", rows[1][4])
        self.assertEqual(rows[2][4], "invalid: '[/sad]' is not a known key")
        self.assertIn("a cat [smiling]: [/sad]", summary)
        missing = self.root / "[/data]"
        app = self.app(data=missing)
        async with app.run_test() as pilot:
            await self.settle(pilot)
            self.assertEqual(self.text(app, "list-message"), f"Data directory not found: {missing}")

    async def test_file_suffixes_match_in_any_case(self) -> None:
        self.write_data_job("Portrait.YAML")
        self.write_data_job("Still.Yml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            self.assertEqual([row[0] for row in self.rows(app)], ["Portrait.YAML", "Still.Yml"])

    async def test_an_empty_and_a_missing_data_directory_show_a_message(self) -> None:
        for directory, message in ((self.data, f"No job files (*.yaml, *.yml) in {self.data}"), (self.root / "absent", f"Data directory not found: {self.root / 'absent'}")):
            with self.subTest(directory=directory):
                app = self.app(data=directory)
                async with app.run_test() as pilot:
                    await self.settle(pilot)
                    self.assertEqual(self.text(app, "list-message"), message)
                    self.assertEqual(self.rows(app), [])
                    await pilot.press("enter")
                    self.assertIsInstance(app.screen, JobListScreen)

    async def test_the_detail_view_shows_the_summary_pairs_and_the_dry_run_plan(self) -> None:
        path = self.write_data_job("walk.yaml", run_count=3, prompt_pairs=[{"name": "walk", "positive": "walk", "negative": "blurry", "runs": [1, 3]}, {"name": "wave", "positive": "wave", "runs": [2]}])
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("enter")
            await self.settle(pilot)
            self.assertIsInstance(app.screen, JobDetailScreen)
            summary, pairs, plan = (self.text(app, name) for name in ("summary", "pairs", "plan"))
            await pilot.press("escape")
            self.assertIsInstance(app.screen, JobListScreen)
        self.assertIn(f"Job file: {path}", summary)
        self.assertIn("  runs: 3 (walk, wave, walk)", summary)
        self.assertIn("  seed: 42 (config_file)", summary)
        self.assertIn("walk: runs 1, 3", pairs)
        self.assertIn("  negative: blurry", pairs)
        self.assertIn("wave: run 2", pairs)
        # The same plan run-job --dry-run prints for this file, with the same output names.
        expected = plan_lines(load_job(path, self.global_config), make_service().preview(load_job(path, self.global_config), executable="draw-things-cli"))
        self.assertEqual(plan.splitlines()[2:], expected)
        self.assertNotIn("placeholder", plan)

    async def test_a_job_without_a_seed_shows_the_placeholder_seed(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.json")
        self.write_data_job("random.yaml", config_file="noseed.json", run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("enter")
            await self.settle(pilot)
            summary, plan = self.text(app, "summary"), self.text(app, "plan")
        self.assertIn("  seed: random (drawn when the job starts)", summary)
        self.assertIn("placeholder seed 0", plan)
        self.assertIn("seed 0 (random)", plan)
        self.assertIn("--seed 0 ", plan)

    async def test_an_invalid_job_shows_only_its_error(self) -> None:
        self.write_data_job("bad.yaml", mode="t2i")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("enter")
            await self.settle(pilot)
            summary = self.text(app, "summary")
            self.assertFalse(app.screen.query_one("#pairs").display)
            self.assertFalse(app.screen.query_one("#plan").display)
        self.assertIn("Invalid job:", summary)
        self.assertIn("'mode'", summary)

    async def test_a_missing_tool_shows_the_message_and_the_rest_still_renders(self) -> None:
        self.write_data_job("walk.yaml")
        for missing in ("draw-things-cli", "ffmpeg"):
            with self.subTest(missing=missing):
                app = self.app(missing=frozenset({missing}))
                async with app.run_test() as pilot:
                    await self.settle(pilot)
                    await pilot.press("enter")
                    await self.settle(pilot)
                    summary, pairs, plan = (self.text(app, name) for name in ("summary", "pairs", "plan"))
                self.assertIn("  runs: 5", summary)
                self.assertIn("walk: runs 1, 3, 5", pairs)
                self.assertIn(f"Could not find '{missing}'", plan)
                self.assertNotIn("# Run 1/5", plan)

    async def test_an_os_error_while_planning_shows_in_the_plan_pane(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app()
        with mock.patch.object(app.job_service, "preview", side_effect=PermissionError(13, "Permission denied", "/out")):
            async with app.run_test() as pilot:
                await self.settle(pilot)
                await pilot.press("enter")
                await self.settle(pilot)
                self.assertTrue(app.is_running)
                summary, plan = self.text(app, "summary"), self.text(app, "plan")
        self.assertIn("  runs: 5", summary)
        self.assertIn("Permission denied", plan)

    async def test_details_that_arrive_after_the_view_closed_are_kept_but_not_shown(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("enter")
            await self.settle(pilot)
            detail = app.screen
            details = app.details[self.data / "walk.yaml"]
            await pilot.press("escape")
            await pilot.pause()
            detail.show(details)
            detail.keep(details)
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIsInstance(app.screen, JobListScreen)

    async def test_the_executable_option_is_used_for_the_plan(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app(executable="/opt/local/draw-things-cli", missing=frozenset({"draw-things-cli"}))
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("enter")
            await self.settle(pilot)
            plan = self.text(app, "plan")
        self.assertNotIn("Could not find", plan)
        self.assertIn("/opt/local/draw-things-cli generate", plan)

    async def test_refresh_keeps_the_selection_and_clears_the_plan_cache(self) -> None:
        self.write_data_job("b.yaml")
        self.write_data_job("c.yaml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("j")
            await pilot.press("enter")
            await self.settle(pilot)
            await pilot.press("escape")
            self.assertEqual(list(app.details), [self.data / "c.yaml"])
            self.write_data_job("a.yaml")
            await pilot.press("r")
            await self.settle(pilot)
            table = app.screen.query_one(JobTable)
            self.assertEqual([row[0] for row in self.rows(app)], ["a.yaml", "b.yaml", "c.yaml"])
            self.assertEqual(table.cursor_row, 2)
            self.assertEqual(app.details, {})
            await pilot.press("k")
            self.assertEqual(table.cursor_row, 1)

    async def test_help_opens_and_closes(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("question_mark")
            self.assertIsInstance(app.screen, HelpScreen)
            self.assertIn("Refresh the list", self.text(app, "help"))
            await pilot.press("escape")
            self.assertIsInstance(app.screen, JobListScreen)
            await pilot.press("question_mark", "question_mark")
            self.assertIsInstance(app.screen, JobListScreen)

    async def test_q_and_ctrl_c_quit(self) -> None:
        self.write_data_job("walk.yaml")
        for key in ("q", "ctrl+c"):
            with self.subTest(key=key):
                app = self.app()
                async with app.run_test() as pilot:
                    await self.settle(pilot)
                    await pilot.press("enter")
                    await self.settle(pilot)
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertFalse(app.is_running)

    async def test_browsing_changes_no_file(self) -> None:
        self.write_data_job("walk.yaml")
        self.write_data_job("resize.yaml", input="photo.jpg", desired_input_width=850)
        self.write_image("photo.jpg", (1920, 1080))
        before = self.snapshot()
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            for _ in range(2):
                await pilot.press("enter")
                await self.settle(pilot)
                await pilot.press("escape", "j")
            await pilot.press("r")
            await self.settle(pilot)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.output_directory.exists())


class TuiCommandTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.global_path = self.root / "global-config.yaml"
        self.global_path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n", encoding="utf-8")
        # The command reinstalls the terminal sinks after the app; here they would point at CliRunner's closed streams.
        patcher = mock.patch.object(cli, "configure_logging")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tui_builds_the_app_from_its_options(self) -> None:
        with mock.patch("draw_things_control.tui.app.DrawThingsApp.run") as run, mock.patch("draw_things_control.tui.app.DrawThingsApp.__init__", return_value=None) as init, mock.patch("draw_things_control.tui.app.DrawThingsApp.return_code", new_callable=mock.PropertyMock, return_value=0):
            result = self.runner.invoke(cli.app, ["tui", "--global-config", str(self.global_path), "--executable", "/opt/dtc/cli"])
            self.assertEqual(result.exit_code, 0, result.output)
            run.assert_called_once_with()
            options = init.call_args.kwargs
            self.assertEqual(options["data_directory"], PROJECT_ROOT / "data")
            self.assertEqual(options["executable"], "/opt/dtc/cli")
            self.assertEqual(options["settings"].output_directory, self.output_directory)
            self.assertIsInstance(options["job_service"], JobService)
            self.runner.invoke(cli.app, ["tui", "--global-config", str(self.global_path), "--data-dir", str(self.root)])
            self.assertEqual(init.call_args.kwargs["data_directory"], self.root)

    def test_a_failed_app_exits_with_its_return_code_and_logging_is_restored(self) -> None:
        with mock.patch("draw_things_control.tui.app.DrawThingsApp.run"), mock.patch("draw_things_control.tui.app.DrawThingsApp.return_code", new_callable=mock.PropertyMock, return_value=1):
            result = self.runner.invoke(cli.app, ["tui", "--global-config", str(self.global_path)])
        self.assertEqual(result.exit_code, 1)
        cli.configure_logging.assert_called_once_with()

    def test_an_invalid_global_configuration_exits_with_2_before_the_app_starts(self) -> None:
        with mock.patch("draw_things_control.tui.app.DrawThingsApp.run") as run:
            result = self.runner.invoke(cli.app, ["tui", "--global-config", str(self.root / "absent.yaml")])
        self.assertEqual(result.exit_code, 2)
        run.assert_not_called()
