"""Headless tests of the TUI's everyday comforts: Tab completion, the saved command history, scrolling the logs from the
keyboard, F1, the idle work status, the Queue list's counts, toasts, and desktop notifications."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from rich.text import Text

from framecontrol.tui import desktop
from framecontrol.tui.panes.job_definitions import JobsList
from framecontrol.tui.panes.queue import QueueList, counts_text
from framecontrol.tui.preferences import COMMAND_HISTORY_LIMIT, load_command_history, save_command_history
from framecontrol.tui.shortcut_help import ShortcutHelpOverlay
from framecontrol.tui.text_overlay import TextOverlay
from framecontrol.tui.widgets import ActivityLog, CommandField
from tests.tui.tui_case import TuiTestCase


def entry(queue_id: str, state: str, job: str = "walk", succeeded: int = 0, total: int = 5) -> dict:
    return {"queue_id": queue_id, "kind": "job", "backend": "draw-things-cli", "job_path": f"/jobs/{job}.yaml", "generation": None, "state": state, "total_runs": total, "succeeded": succeeded, "park_requested": False}


class CompletionTests(TuiTestCase):
    async def test_tab_adds_what_the_choices_share_then_steps_through_them_and_the_status_line_lists_them(self) -> None:
        app = self.make_app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            field = app.screen.query_one(CommandField)
            field.value = "/r"
            field.cursor_position = 2
            await pilot.press("tab")
            shared = field.value
            field.value = "/qu"
            field.cursor_position = 3
            await pilot.press("tab")
            first, status = field.value, self.text(app, "status-line")
            await pilot.press("tab")
            second = field.value
            await pilot.press("tab")
            wrapped = field.value
            await pilot.press("x")
            await pilot.pause()
            after_typing = self.text(app, "status-line")
        self.assertEqual(shared, "/re")
        self.assertEqual((first, second, wrapped), ("/queue", "/quit", "/queue"))
        self.assertIn("Tab 1/2", status)
        self.assertIn("/queue", status)
        self.assertIn("/quit", status)
        self.assertNotIn("Tab 1/2", after_typing)

    async def test_tab_with_one_choice_finishes_it_and_with_none_moves_the_focus(self) -> None:
        app = self.make_app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            field = app.screen.query_one(CommandField)
            field.value = "/rev"
            field.cursor_position = 4
            await pilot.press("tab")
            finished = field.value
            field.value = "nonsense"
            field.cursor_position = 8
            await pilot.press("tab")
            focused = type(app.focused).__name__
        self.assertEqual(finished, "/reveal")
        self.assertEqual(focused, "JobsList")


class CommandHistoryTests(TuiTestCase):
    async def test_commands_are_kept_for_the_next_session(self) -> None:
        app = self.make_app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            for line in ("/get jobs", "/get history", "/get history"):
                await self.command(pilot, line)
            await self.wait_for(pilot, lambda: load_command_history(self.paths.tui_command_history) == ["/get jobs", "/get history"], "the history file")
        again = self.make_app()
        async with again.run_test() as pilot:
            await self.settle(pilot)
            field = again.screen.query_one(CommandField)
            field.focus()
            await pilot.press("up")
            newest = field.value
            await pilot.press("up")
            older = field.value
        self.assertEqual((newest, older), ("/get history", "/get jobs"))

    def test_the_file_keeps_the_newest_commands_and_a_missing_one_is_empty(self) -> None:
        path = self.paths.tui_command_history
        self.assertEqual(load_command_history(path), [])
        save_command_history(path, [f"/get jobs {number}" for number in range(COMMAND_HISTORY_LIMIT + 5)])
        loaded = load_command_history(path)
        self.assertEqual(len(loaded), COMMAND_HISTORY_LIMIT)
        self.assertEqual(loaded[-1], f"/get jobs {COMMAND_HISTORY_LIMIT + 4}")


class KeyboardTests(TuiTestCase):
    async def test_page_keys_in_the_command_field_scroll_the_logs(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(pilot)
            log = app.screen.query_one(ActivityLog)
            for number in range(120):
                app.say(f"line {number}")
            await pilot.pause()
            bottom = log.scroll_y
            await pilot.press("pageup")
            await pilot.pause()
            up = log.scroll_y
            await pilot.press("pagedown")
            await pilot.pause()
            down = log.scroll_y
        self.assertGreater(bottom, 0)
        self.assertLess(up, bottom)
        self.assertGreater(down, up)

    async def test_f1_shows_the_help_of_the_focused_component_or_the_command_field_and_closes_it(self) -> None:
        app = self.make_app()
        async with app.run_test() as pilot:
            await self.settle(pilot)
            await pilot.press("f1")
            await pilot.pause()
            from_field = app.screen
            title = str(from_field.query_one("#shortcut-help-title").render()) if isinstance(from_field, ShortcutHelpOverlay) else ""
            await pilot.press("f1")
            await pilot.pause()
            closed = not isinstance(app.screen, ShortcutHelpOverlay)
            app.screen.query_one(JobsList).focus()
            await pilot.press("f1")
            await pilot.pause()
            from_list = str(app.screen.query_one("#shortcut-help-title").render()) if isinstance(app.screen, ShortcutHelpOverlay) else ""
        self.assertEqual(title, "Command field shortcuts")
        self.assertTrue(closed)
        self.assertEqual(from_list, "Jobs list shortcuts")


class QueueAndStatusTests(TuiTestCase):
    async def test_the_idle_work_status_says_what_is_queued_and_what_finished_last(self) -> None:
        self.server.entries = [entry("Q0004", "queued", "wave"), entry("Q0003", "queued"), entry("Q0002", "succeeded", succeeded=5), entry("Q0001", "failed", succeeded=2)]
        app = self.make_app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            app.refresh_queue()
            await self.wait_for(pilot, lambda: app.screen.query_one(QueueList).row_count == 4, "the queue")
            await pilot.pause()
            lines = self.text(app, "work-status").split("\n")
            subtitle = str(app.screen.query_one(QueueList).border_subtitle)
        self.assertEqual(lines[0], "idle")
        self.assertEqual(lines[1], "2 queued, next: wave (Q0004)")
        self.assertEqual(lines[3], "last: Q0002 walk succeeded, 5/5 runs")
        self.assertEqual(subtitle, "2 queued, 2 finished")

    async def test_enter_on_a_queue_row_opens_the_viewer_and_l_writes_the_entry_to_the_log(self) -> None:
        self.server.entries = [entry("Q0003", "queued")]
        self.server.details["Q0003"] = {**entry("Q0003", "queued"), "resumable": False, "resume_refused_reason": "Q0003 is queued"}
        app = self.make_app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            app.refresh_queue()
            pane = app.screen.query_one(QueueList)
            await self.wait_for(pilot, lambda: pane.row_count == 1, "the queue")
            pane.focus()
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: isinstance(app.screen, TextOverlay), "the viewer")
            title = str(app.screen.query_one("#text-overlay-title").render())
            body = str(app.screen.query_one("#text-overlay-body").render())
            logged_by_enter = any("Queue entry Q0003" in line for line in self.said)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("l")
            await self.wait_for(pilot, lambda: any("Queue entry Q0003" in line for line in self.said), "the log")
        self.assertEqual(title, "Queue entry Q0003")
        self.assertIn("Queue entry Q0003: queued", body)
        self.assertFalse(logged_by_enter)

    def test_the_counts_leave_out_a_state_with_no_entry(self) -> None:
        self.assertEqual(counts_text([entry("Q1", "running"), entry("Q2", "queued"), entry("Q3", "cancelled")]), "1 running, 1 queued, 1 finished")
        self.assertEqual(counts_text([]), "")

    async def test_an_error_from_a_key_in_a_list_is_a_toast_and_one_from_the_command_field_is_not(self) -> None:
        (self.data / "broken.yaml").write_text("version: 2\nnot: [valid", encoding="utf-8")
        app = self.make_app()
        async with app.run_test(size=(160, 50)) as pilot:
            await self.settle(pilot)
            await self.command(pilot, "/queue cancel nonsense")
            from_field = len(app._notifications)
            table = app.screen.query_one(JobsList)
            table.focus()
            await pilot.press("a")
            await pilot.pause()
            from_list = [notification.severity for notification in app._notifications]
        self.assertEqual(from_field, 0)
        self.assertEqual(from_list, ["error"])


class DesktopNotificationTests(unittest.TestCase):
    def test_the_title_and_message_are_arguments_of_the_script_never_part_of_it(self) -> None:
        with mock.patch.object(desktop.sys, "platform", "darwin"), mock.patch.object(desktop, "run_process", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            error = desktop.post_notification('say "hi"', "walk: 2/2 runs completed")
        command = run.call_args.args[0]
        self.assertIsNone(error)
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[-2:], ["walk: 2/2 runs completed", 'say "hi"'])
        self.assertNotIn("walk", " ".join(command[1:-2]))

    def test_a_failure_or_another_system_is_reported_not_raised(self) -> None:
        with mock.patch.object(desktop.sys, "platform", "linux"):
            self.assertIn("macOS", desktop.post_notification("t", "m") or "")
        with mock.patch.object(desktop.sys, "platform", "darwin"), mock.patch.object(desktop, "run_process", side_effect=OSError("no osascript")):
            self.assertIn("no osascript", desktop.post_notification("t", "m") or "")
        with mock.patch.object(desktop.sys, "platform", "darwin"), mock.patch.object(desktop, "run_process", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIn("exited with 1", desktop.post_notification("t", "m") or "")


class StatusLineTests(unittest.TestCase):
    def test_the_line_names_the_server_connection_and_the_completions_replace_it(self) -> None:
        from pathlib import Path

        from framecontrol.tui.text.status import status_line_text

        up = status_line_text(Path("/data"), None, False, server=True)
        down = status_line_text(Path("/data"), None, False, server=False)
        stepping = status_line_text(Path("/data"), None, False, server=True, completions=(["/queue", "/quit"], 1))
        self.assertIn("server connected", str(up))
        self.assertIn("server offline (read-only)", str(down))
        self.assertEqual(str(stepping).strip(), "Tab 2/2: /queue  /quit")
        self.assertIsInstance(up, Text)
