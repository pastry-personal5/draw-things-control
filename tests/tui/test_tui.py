"""Headless tests of the terminal UI, with fake tools and temporary directories."""

from __future__ import annotations

import itertools
import signal
from datetime import datetime
from pathlib import Path
from unittest import mock

from textual.color import Color
from typer.testing import CliRunner

from draw_things_control.cli import app as cli
from draw_things_control.core.global_config import PROJECT_ROOT
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_report import plan_lines
from draw_things_control.jobs.job_service import JobService
from draw_things_control.tui.app import DrawThingsApp
from draw_things_control.tui.panes import HistoryPane
from draw_things_control.tui.screens import MainScreen
from draw_things_control.tui.widgets import MAX_MESSAGE_LINES, CommandInput, MessageLog
from tests.fixtures import JobTestCase
from tests.tui.tui_case import TuiTestCase


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


class TuiTests(TuiTestCase):
    def app(self, *, data: Path | None = None, executable: str = "draw-things-cli", missing: frozenset[str] = frozenset()) -> DrawThingsApp:
        return self.make_app(make_service(missing), data=data, executable=executable)

    async def show(self, pilot, name: str) -> str:
        await self.command(pilot, f"/job {name}")
        return "\n".join(self.since(f"/job {name}"))

    async def test_the_widgets_are_laid_out_around_the_history(self) -> None:
        for size, cli_height, messages in (((120, 40), 15, (15, 36)), ((80, 24), 14, (14, 20))):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await self.settle(pilot)
                    width, height = size
                    regions = {name: app.screen.query_one(f"#{name}").region for name in ("cli", "messages", "history", "command-line", "status-line")}
                    rules = [rule.region for rule in app.screen.query(".command-rule")]
                    focused = app.focused
                    status = self.text(app, "status-line")
                self.assertEqual((regions["cli"].x, regions["cli"].y, regions["cli"].height), (0, 0, cli_height))
                self.assertEqual((regions["messages"].x, regions["messages"].y, regions["messages"].bottom), (0, *messages))
                self.assertGreaterEqual(regions["messages"].height, 6)
                self.assertEqual((regions["history"].y, regions["history"].right, regions["history"].height), (0, width, height - 4))
                self.assertEqual(regions["history"].x, regions["cli"].right)
                # A third of the width, at least 36 columns.
                self.assertEqual(regions["history"].width, max(36, width // 3))
                self.assertEqual([(rule.x, rule.y, rule.width, rule.height) for rule in rules], [(0, height - 4, width, 1), (0, height - 2, width, 1)])
                self.assertEqual((regions["command-line"].y, regions["command-line"].width, regions["command-line"].height), (height - 3, width, 1))
                self.assertEqual((regions["status-line"].y, regions["status-line"].height), (height - 1, 1))
                self.assertIsInstance(focused, CommandInput)
                self.assertIn(str(self.data), status)
                self.assertIn("idle", status)
                self.assertIn("/help: commands, Ctrl-C twice: quit", status)

    async def test_the_app_is_dark_the_cursor_steady_and_the_palette_off(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            field = app.screen.query_one(CommandInput)
            self.assertEqual(app.theme, "textual-dark")
            self.assertFalse(field.cursor_blink)
            self.assertEqual(field.placeholder, "")
            # Every widget but the command line and the history is on black.
            for name in ("cli", "run-line", "cli-output", "messages", "status-line"):
                self.assertEqual(app.screen.query_one(f"#{name}").styles.background, Color(0, 0, 0), name)
            for name in ("command", "history"):
                self.assertNotEqual(app.screen.query_one(f"#{name}").styles.background, Color(0, 0, 0), name)
            await pilot.press("ctrl+p")
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            self.assertEqual(field.value, "")

    async def test_the_old_single_keys_only_type(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("q", "x", "l", "s", "r", "question_mark", "j", "k")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIsInstance(app.screen, MainScreen)
            self.assertEqual(app.screen.query_one(CommandInput).value, "qxlsr?jk")
            self.assertIsNone(app.live)

    async def test_jobs_lists_every_job_file_and_its_status(self) -> None:
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
            await self.command(pilot, "/jobs")
            [listing] = self.since("/jobs")
        lines = listing.splitlines()
        self.assertEqual(lines[0], "Jobs")
        self.assertEqual([line.split()[0] for line in lines[1:]], ["a-still.yml", "b-walk.yaml", "c-bad.yaml", "d-bad-pairs.yaml"])
        self.assertEqual(lines[1].split()[1:], ["sunset-walk", "i2i", "1", "run", "valid"])
        self.assertEqual(lines[2].split()[1:], ["sunset-walk", "i2v", "5", "runs", "valid"])
        self.assertIn("invalid: 'mode'", lines[3])
        self.assertIn("invalid: 'prompt_pairs[0].runs'", lines[4])
        self.assertNotIn(str(self.data), lines[4])

    async def test_brackets_in_errors_and_paths_are_shown_as_written(self) -> None:
        (self.data / "broken.yaml").write_text("name: x\npositive: a cat [smiling]: [/sad]\n", encoding="utf-8")
        self.write_data_job("[b] walk.yaml")
        self.write_data_job("c-key.yaml", **{"[/sad]": 1})
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/jobs")
            [listing] = self.since("/jobs")
            shown = await self.show(pilot, "broken.yaml")
            walk = await self.show(pilot, "'[b] walk.yaml'")
        self.assertIn("[b] walk.yaml", listing)
        self.assertIn("a cat [smiling]: [/sad]", listing)
        self.assertIn("invalid: '[/sad]' is not a known key", listing)
        self.assertIn("a cat [smiling]: [/sad]", shown)
        self.assertIn("Invalid job:", shown)
        self.assertIn(f"Job file: {self.data / '[b] walk.yaml'}", walk)
        missing = self.root / "[/data]"
        app = self.app(data=missing)
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/jobs")
            self.assertEqual(self.since("/jobs"), [f"Data directory not found: {missing}"])

    async def test_file_suffixes_match_in_any_case(self) -> None:
        self.write_data_job("Portrait.YAML")
        self.write_data_job("Still.Yml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/jobs")
            [listing] = self.since("/jobs")
        self.assertEqual([line.split()[0] for line in listing.splitlines()[1:]], ["Portrait.YAML", "Still.Yml"])

    async def test_an_empty_and_a_missing_data_directory_show_a_message(self) -> None:
        for directory, message in ((self.data, f"No job files (*.yaml, *.yml) in {self.data}"), (self.root / "absent", f"Data directory not found: {self.root / 'absent'}")):
            with self.subTest(directory=directory):
                self.said.clear()
                app = self.app(data=directory)
                async with app.run_test() as pilot:
                    await self.settle(pilot)
                    # The message is written when the app starts, and again for /jobs.
                    self.assertIn(message, self.said)
                    await self.command(pilot, "/jobs")
                    self.assertEqual(self.since("/jobs"), [message])
                    await self.command(pilot, "/job walk")
                    self.assertEqual(self.since("/job walk"), [f"No job file 'walk' in {directory}"])

    async def test_job_prints_the_summary_pairs_and_the_dry_run_plan(self) -> None:
        path = self.write_data_job("walk.yaml", run_count=3, prompt_pairs=[{"name": "walk", "positive": "walk", "negative": "blurry", "runs": [1, 3]}, {"name": "wave", "positive": "wave", "runs": [2]}])
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            shown = await self.show(pilot, "walk.yaml")
            by_stem = await self.show(pilot, "walk")
        self.assertIn(f"Job file: {path}", shown)
        self.assertIn(f"Job file: {path}", by_stem)
        self.assertIn("  runs: 3 (walk, wave, walk)", shown)
        self.assertIn("  seed: 42 (config_file)", shown)
        self.assertIn("walk: runs 1, 3", shown)
        self.assertIn("  negative: blurry", shown)
        self.assertIn("wave: run 2", shown)
        # The same plan run-job --dry-run prints for this file, with the same output names.
        expected = plan_lines(load_job(path, self.global_config), make_service().preview(load_job(path, self.global_config), executable="draw-things-cli"))
        self.assertEqual(shown.split("Dry-run plan\n\n")[1].splitlines(), expected)
        self.assertNotIn("placeholder", shown)

    async def test_a_job_name_without_its_suffix_must_be_unique(self) -> None:
        self.write_data_job("walk.yaml")
        self.write_data_job("walk.yml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            ambiguous = await self.show(pilot, "walk")
            exact = await self.show(pilot, "walk.yml")
        self.assertEqual(ambiguous, "'walk' matches walk.yaml, walk.yml; give the file name")
        self.assertIn(f"Job file: {self.data / 'walk.yml'}", exact)

    async def test_a_job_without_a_seed_shows_the_placeholder_seed(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        self.write_data_job("random.yaml", config_file="noseed.yaml", run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            shown = await self.show(pilot, "random")
        self.assertIn("  seed: random (drawn when the job starts)", shown)
        self.assertIn("placeholder seed 0", shown)
        self.assertIn("seed 0 (random)", shown)
        self.assertIn("--seed 0 ", shown)

    async def test_an_invalid_job_shows_only_its_error(self) -> None:
        self.write_data_job("bad.yaml", mode="t2i")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            shown = await self.show(pilot, "bad")
        self.assertTrue(shown.startswith(f"Invalid job: {self.data / 'bad.yaml'}"), shown)
        self.assertIn("'mode'", shown)
        self.assertNotIn("Dry-run plan", shown)

    async def test_a_missing_tool_shows_the_message_and_the_rest_still_renders(self) -> None:
        self.write_data_job("walk.yaml")
        for missing in ("draw-things-cli", "ffmpeg"):
            with self.subTest(missing=missing):
                app = self.app(missing=frozenset({missing}))
                async with app.run_test() as pilot:
                    await self.settle(pilot)
                    shown = await self.show(pilot, "walk")
                self.assertIn("  runs: 5", shown)
                self.assertIn("walk: runs 1, 3, 5", shown)
                self.assertIn(f"Could not find '{missing}'", shown)
                self.assertNotIn("# Run 1/5", shown)

    async def test_an_os_error_while_planning_shows_in_the_plan(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app()
        with mock.patch.object(app.job_service, "preview", side_effect=PermissionError(13, "Permission denied", "/out")):
            async with app.run_test() as pilot:
                await self.settle(pilot)
                shown = await self.show(pilot, "walk")
                self.assertTrue(app.is_running)
        self.assertIn("  runs: 5", shown)
        self.assertIn("Permission denied", shown)

    async def test_the_executable_option_is_used_for_the_plan(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app(executable="/opt/local/draw-things-cli", missing=frozenset({"draw-things-cli"}))
        async with app.run_test() as pilot:
            await self.settle(pilot)
            shown = await self.show(pilot, "walk")
        self.assertNotIn("Could not find", shown)
        self.assertIn("/opt/local/draw-things-cli generate", shown)

    async def test_each_job_command_reads_the_file_again(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            before = await self.show(pilot, "walk")
            self.write_data_job("walk.yaml", run_count=2, prompt_pairs=[{"name": "walk", "positive": "walk"}])
            after = await self.show(pilot, "walk")
        self.assertIn("  runs: 5", before)
        self.assertIn("  runs: 2", after)

    async def test_wrong_commands_say_why(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            for line, message in (
                ("run walk", "Commands begin with /; type /help"),
                ("/launch walk", "Unknown command '/launch'; type /help"),
                ("/exit", "Unknown command '/exit'; type /help"),
                ("/run 'walk", "Cannot read the command: No closing quotation"),
                ("/job", "Usage: /job JOB"),
                ("/run a b", "Usage: /run JOB"),
                ("/stop now", "Usage: /stop"),
                ("/history 3", "Usage: /history"),
                ("/execution x", "Usage: /execution ID"),
                ("/filter status done", "Unknown status 'done'; use one of succeeded, failed, interrupted, running"),
                ("/filter clear", "Usage: /filter status STATUS | /filter name TEXT | /filter off"),
                ("/filter name ''", "Usage: /filter status STATUS | /filter name TEXT | /filter off"),
                ("/reveal x", "Usage: /reveal ID [RUN]"),
                # IDs SQLite cannot hold, and digits int() or SQLite would refuse, are usage errors, not crashes.
                ("/execution 99999999999999999999", "Usage: /execution ID"),
                ("/execution ²", "Usage: /execution ID"),
                ("/execution 0", "Usage: /execution ID"),
                ("/reveal 99999999999999999999", "Usage: /reveal ID [RUN]"),
                ("/reveal 1 ²", "Usage: /reveal ID [RUN]"),
                ("/stop", "No job is running"),
            ):
                with self.subTest(line=line):
                    await self.command(pilot, line)
                    self.assertEqual(self.since(line), [message])
            self.assertTrue(app.is_running)

    async def test_help_and_clear(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/help")
            [shown] = self.since("/help")
            log = app.screen.query_one(MessageLog)
            self.assertGreater(len(log.lines), 10)
            await self.command(pilot, "/clear")
            self.assertEqual(len(log.lines), 0)
        self.assertIn("/reveal ID [RUN]", shown)
        self.assertIn("Tab", shown)

    async def test_messages_keep_at_most_5000_lines(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            log = app.screen.query_one(MessageLog)
            for number in range(MAX_MESSAGE_LINES + 20):
                log.say(f"line {number}")
            await pilot.pause()
            self.assertLessEqual(len(log.lines), MAX_MESSAGE_LINES)
            self.assertIn(f"line {MAX_MESSAGE_LINES + 19}", log.lines[-1].text)

    async def test_tab_completes_and_otherwise_moves_to_the_history(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            field = app.screen.query_one(CommandInput)
            await pilot.press("slash", "r", "e")
            await self.wait_for(pilot, lambda: field._suggestion == "/reveal", "the suggestion")
            await pilot.press("tab")
            self.assertEqual(field.value, "/reveal")
            field.value = ""
            await pilot.press("slash", "j", "o", "b", "space", "w")
            await self.wait_for(pilot, lambda: field._suggestion == "/job walk.yaml", "the job name")
            await pilot.press("tab", "enter")
            await self.settle(pilot)
            self.assertIn("  runs: 5", "\n".join(self.since("/job walk.yaml")))
            await pilot.press("tab")
            self.assertIsInstance(app.focused, HistoryPane)
            await pilot.press("escape")
            self.assertIs(app.focused, field)

    async def test_up_and_down_recall_this_sessions_commands_and_escape_clears(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            field = app.screen.query_one(CommandInput)
            for line in ("/help", "/jobs", "/jobs", "nonsense"):
                await self.command(pilot, line)
            field.focus()
            await pilot.press("up")
            self.assertEqual(field.value, "nonsense")
            await pilot.press("up", "up")
            self.assertEqual(field.value, "/help")
            await pilot.press("up")
            self.assertEqual(field.value, "/help")
            await pilot.press("down")
            self.assertEqual(field.value, "/jobs")
            await pilot.press("down", "down")
            self.assertEqual(field.value, "")
            await pilot.press("up", "escape")
            self.assertEqual(field.value, "")
            await pilot.press("up")
            self.assertEqual(field.value, "nonsense")

    async def test_ctrl_c_clears_the_line_then_quits_on_a_second_press(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            field = app.screen.query_one(CommandInput)
            await pilot.press("slash", "j")
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertEqual(field.value, "")
            self.assertNotIn("Press Ctrl-C again", self.text(app, "status-line"))
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertEqual(self.text(app, "status-line").strip(), "Press Ctrl-C again to quit")
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertFalse(app.is_running)
        self.assertEqual(app.return_code, 0)

    async def test_a_first_ctrl_c_is_forgotten_after_two_seconds(self) -> None:
        app = self.app()
        with mock.patch("draw_things_control.tui.app.QUIT_PRESS_SECONDS", 0.2):
            async with app.run_test() as pilot:
                await self.settle(pilot)
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertTrue(app.quit_armed)
                await self.wait_for(pilot, lambda: "Press Ctrl-C again" not in self.text(app, "status-line"), "the hint to go")
                self.assertFalse(app.quit_armed)
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertTrue(app.is_running)

    async def test_quit_quits(self) -> None:
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/quit", settle=False)
            await pilot.pause()
            self.assertFalse(app.is_running)
        self.assertEqual(app.return_code, 0)

    async def test_browsing_changes_no_file(self) -> None:
        self.write_data_job("walk.yaml")
        self.write_data_job("resize.yaml", input="photo.jpg", desired_input_width=850)
        self.write_image("photo.jpg", (1920, 1080))
        before = self.snapshot()
        app = self.app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            for line in ("/jobs", "/job walk", "/job resize", "/history", "/execution 1", "/filter status failed", "/filter name walk", "/filter off", "/reveal 1"):
                await self.command(pilot, line)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.output_directory.exists())
        self.assertFalse(self.state.exists())


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
            # Jobs run on a worker thread, where a service that installs signal handlers would raise.
            self.assertFalse(options["job_service"]._handle_signals)
            self.assertEqual(options["shutdown_grace"], 10.0)
            self.runner.invoke(cli.app, ["tui", "--global-config", str(self.global_path), "--data-dir", str(self.root), "--shutdown-grace", "2.5"])
            self.assertEqual(init.call_args.kwargs["data_directory"], self.root)
            self.assertEqual(init.call_args.kwargs["shutdown_grace"], 2.5)

    def test_a_negative_shutdown_grace_exits_with_2(self) -> None:
        with mock.patch("draw_things_control.tui.app.DrawThingsApp.run") as run:
            result = self.runner.invoke(cli.app, ["tui", "--global-config", str(self.global_path), "--shutdown-grace", "-1"])
        self.assertEqual(result.exit_code, 2)
        run.assert_not_called()

    def test_any_running_job_is_cancelled_after_the_app_returns(self) -> None:
        with mock.patch("draw_things_control.tui.app.DrawThingsApp.run", side_effect=RuntimeError("terminal gone")), mock.patch.object(JobService, "cancel") as cancel:
            result = self.runner.invoke(cli.app, ["tui", "--global-config", str(self.global_path)])
        self.assertIsInstance(result.exception, RuntimeError)
        cancel.assert_called_once_with(signal.SIGINT)
        cli.configure_logging.assert_called_once_with()

    def test_run_job_keeps_a_service_that_handles_signals(self) -> None:
        self.assertTrue(cli.job_service._handle_signals)

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
