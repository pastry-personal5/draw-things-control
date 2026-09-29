"""Milestone 04: ``/verbose high|medium|low``: the preference file, the ``include_output`` the TUI's one gRPC stream
opens with, what the draw-things-cli pane writes, and how often the periodic widgets render."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.tui.app import LOW_NOTICE, DrawThingsApp
from draw_things_control.tui.commands import completions
from draw_things_control.tui.panes.cli_output import LOW_MARKER, MEDIUM_MARKER
from draw_things_control.tui.panes.status import StatusPane
from draw_things_control.tui.preferences import DEFAULT_VERBOSE_LEVEL, LOW_STATUS_REFRESH_SECONDS, MEDIUM_OUTPUT_WINDOW_SECONDS, load_verbose_level, save_verbose_level
from tests.tui.fake_server import cooldown_started, job_started, run_finished, run_output, run_started
from tests.tui.feed_case import FeedTestCase

EARLIER = "(earlier output not shown)"


class Clock:
    """A settable monotonic clock, for a LiveRun and for the screen's once-a-minute refresh."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class PreferencesTests(unittest.TestCase):
    def test_a_missing_unreadable_or_unknown_preference_is_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config" / "tui-preferences.yaml"
            self.assertEqual(load_verbose_level(path), DEFAULT_VERBOSE_LEVEL)
            path.parent.mkdir()
            for text in ("verbose_level: loud\n", "verbose_level: [\n", "- a\n- b\n", "", "verbose_level: 3\n"):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(load_verbose_level(path), DEFAULT_VERBOSE_LEVEL, text)
            path.write_bytes(b"\xff\xfe")
            self.assertEqual(load_verbose_level(path), DEFAULT_VERBOSE_LEVEL)

    def test_a_saved_level_is_read_back_and_config_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "config" / "tui-preferences.yaml"
            for level in ("low", "medium", "high"):
                save_verbose_level(path, level)  # type: ignore[arg-type]  # a plain string in the loop
                self.assertEqual(load_verbose_level(path), level)
            self.assertIn("version: 1", path.read_text(encoding="utf-8"))

    def test_the_words_complete_after_the_command(self) -> None:
        self.assertEqual(completions("/verbose ", []), ["/verbose high", "/verbose medium", "/verbose low"])
        self.assertEqual(completions("/verbose m", []), ["/verbose medium"])
        self.assertEqual(completions("/verb", []), ["/verbose"])


class VerboseTestCase(FeedTestCase):
    def preset(self, level: str) -> None:
        save_verbose_level(self.paths.tui_preferences, level)  # type: ignore[arg-type]  # a plain string

    async def running_job(self, pilot: Any, clock: Clock, *, run: int = 1) -> DrawThingsApp:
        """Connect, start a job, and start its run at ``clock``'s time, on that clock."""
        app = await self.connect(pilot)
        app.main.running.clock = clock  # type: ignore[union-attr]
        self.server.send_queue_entry("Q0001", "running")
        await self.push(pilot, job_started())
        await self.wait_for(pilot, lambda: app.live is not None, "the job")
        assert app.live is not None
        app.live._clock = clock
        await self.push(pilot, run_started(run))
        return app

    def requests(self) -> list[bool]:
        return [request.include_output for request in self.server.requests]


class CommandTests(VerboseTestCase):
    async def test_verbose_reports_sets_persists_and_refuses(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/verbose")
            self.assertEqual(self.since("/verbose"), ["Verbose level: high."])
            await self.command(pilot, "/verbose MEDIUM")
            self.assertEqual(self.since("/verbose MEDIUM"), ["Verbose level: medium."])
            self.assertEqual(app.verbose_level, "medium")
            self.assertEqual(load_verbose_level(self.paths.tui_preferences), "medium")
            await self.command(pilot, "/verbose medium")
            self.assertEqual(self.since("/verbose medium"), ["Verbose level: medium."])
            await self.command(pilot, "/verbose loud")
            self.assertEqual(self.since("/verbose loud"), ["Usage: /verbose [high|medium|low]"])
            await self.command(pilot, "/verbose high low")
            self.assertEqual(self.since("/verbose high low"), ["Usage: /verbose [high|medium|low]"])
            self.assertEqual(app.verbose_level, "medium")

    async def test_a_fresh_start_is_high_and_an_earlier_session_is_remembered(self) -> None:
        async with self.make_app().run_test() as pilot:
            await self.connect(pilot)
            self.assertEqual(pilot.app.verbose_level, "high")  # type: ignore[attr-defined]
        self.assertFalse(self.paths.tui_preferences.exists())
        self.preset("medium")
        async with self.make_app().run_test() as pilot:
            await self.connect(pilot)
            self.assertEqual(pilot.app.verbose_level, "medium")  # type: ignore[attr-defined]


class StreamTests(VerboseTestCase):
    async def test_the_stream_opens_with_include_output_for_each_level(self) -> None:
        for level, wanted in (("high", True), ("medium", True), ("low", False)):
            with self.subTest(level=level):
                self.server.requests.clear()
                self.preset(level)
                async with self.make_app().run_test() as pilot:
                    await self.connect(pilot)
                self.assertEqual(self.requests()[0], wanted)

    async def test_only_crossing_the_low_boundary_reconnects_and_it_resumes_from_the_last_event(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.push(pilot, job_started())
            await self.command(pilot, "/verbose medium")
            await self.command(pilot, "/verbose high")
            await pilot.pause()
            self.assertEqual(len(self.server.requests), 1)
            last_id = app._feed.last_event_id
            await self.command(pilot, "/verbose low")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 2, "the reconnect")
            self.assertEqual(self.since("/verbose low"), [LOW_NOTICE, "Reconnecting to dtc serve for verbose low..."])
            await self.command(pilot, "/verbose low")
            await self.command(pilot, "/verbose high")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 3, "the second reconnect")
            await pilot.pause()
            self.assertEqual(self.since("/verbose high"), ["Verbose level: high.", "Reconnecting to dtc serve for verbose high..."])
        self.assertEqual([(request.include_output, request.last_event_id) for request in self.server.requests], [(True, 1), (False, last_id), (True, last_id)])
        # Every call was cancelled when its stream was replaced or the app ended.
        self.assertEqual(len(self.server.open_calls), 0)

    async def test_a_switch_the_backlog_can_explain_resumes_with_no_reset_and_replays_what_low_filtered(self) -> None:
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.push(pilot, run_output("first"))
            await self.command(pilot, "/verbose low")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 2, "low")
            await pilot.pause()
            queries = self.http_queries()
            await self.push(pilot, run_output("second"), run_output("third"))
            self.assertEqual(self.pane(app), ["first", LOW_MARKER])
            await self.command(pilot, "/verbose high")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 3, "high")
            await self.wait_for(pilot, lambda: self.pane(app)[-1] == "third", "the replayed lines")
            pane = self.pane(app)
            self.assertEqual(self.http_queries(), queries)
        self.assertEqual(pane, ["first", LOW_MARKER, "second", "third"])

    async def test_a_switch_the_backlog_cannot_explain_is_a_reset_and_reseeds_the_pane(self) -> None:
        clock = Clock()
        self.server.running = [{"queue_id": "Q0001", "job_path": "/jobs/walk.yaml", "total_runs": 2, "submitted_at": "t"}]
        self.server.details["Q0001"] = {"queue_id": "Q0001", "execution_id": None, "current_run": 1, "current_run_elapsed_seconds": 5.0}
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.push(pilot, run_output("kept until the reset"))
            await self.command(pilot, "/verbose low")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 2, "low")
            self.server.explain_gap = False
            await self.command(pilot, "/verbose high")
            await self.wait_for(pilot, lambda: self.pane(app) == [EARLIER], "the reseeded pane")
        self.assertEqual(self.requests(), [True, False, True])

    def http_queries(self) -> int:
        return len([path for path in self.server.http_paths if path.startswith("GET /v1/queue?state=running")])


class MediumTests(VerboseTestCase):
    async def test_medium_writes_a_runs_first_minute_and_marks_each_closed_window_once(self) -> None:
        self.preset("medium")
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            clock.now = 1000 + MEDIUM_OUTPUT_WINDOW_SECONDS
            await self.push(pilot, run_output("at the edge"))
            clock.now = 1000 + MEDIUM_OUTPUT_WINDOW_SECONDS + 1
            await self.push(pilot, run_output("late"), run_output("later", progress=(2, 8), percent=25))
            await self.push(pilot, run_output("later still"))
            first = self.pane(app)
            clock.now = 2000
            await self.push(pilot, run_finished(1), run_started(2))
            await self.push(pilot, run_output("second run"))
            clock.now = 2100
            await self.push(pilot, run_output("second run, late"))
            second = self.pane(app)
            written = app.live.output_count  # type: ignore[union-attr]
        self.assertEqual(first, ["at the edge", MEDIUM_MARKER])
        self.assertEqual(second, ["at the edge", MEDIUM_MARKER, "second run", MEDIUM_MARKER])
        # The run line's own progress is fed by the same events and is not gated.
        self.assertEqual(written, 6)

    async def test_medium_typed_mid_run_opens_a_fresh_window_and_high_shows_only_what_comes_after(self) -> None:
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            clock.now = 1500
            await self.command(pilot, "/verbose medium")
            clock.now = 1530
            await self.push(pilot, run_output("thirty seconds in"))
            clock.now = 1590
            await self.push(pilot, run_output("ninety seconds in"))
            await self.command(pilot, "/verbose high")
            clock.now = 1600
            await self.push(pilot, run_output("after the switch"))
            clock.now = 1700
            await self.push(pilot, run_finished(1), run_started(2))
            await self.command(pilot, "/verbose medium")
            clock.now = 1705
            await self.push(pilot, run_output("next run"))
            pane = self.pane(app)
        self.assertEqual(pane, ["thirty seconds in", MEDIUM_MARKER, "after the switch", "next run"])

    async def test_between_runs_nothing_says_the_window_closed(self) -> None:
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.push(pilot, run_finished(1))
            await self.command(pilot, "/verbose medium")
            assert app.live is not None
            app.live.output_window_start = None
            clock.now = 5000
            await self.push(pilot, run_output("stray line"))
            pane = self.pane(app)
        self.assertEqual(pane, ["stray line"])


class LowTests(VerboseTestCase):
    async def test_low_never_writes_output_asks_nothing_and_says_so_once(self) -> None:
        self.preset("low")
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.push(pilot, run_output("hidden"), run_output("Sampling... 4 / 8 [ ] 50  %", progress=(4, 8), percent=50))
            clock.now = 5000
            await self.push(pilot, run_output("still hidden"))
            assert app.live is not None
            # A stale reading, as an earlier level might have left; low shows none of it.
            app.live.progress, app.live.percent = (3, 8), 37
            app.main.running.tick(force=True)  # type: ignore[union-attr]
            run_line = self.text(app, "run-line")
            pane = self.pane(app)
        self.assertEqual(pane, [LOW_MARKER])
        self.assertIn("Run 1/2", run_line)
        self.assertNotIn("progress", run_line)
        self.assertEqual(self.said.count(LOW_NOTICE), 1)
        self.assertEqual([path for path in self.server.http_paths if path.startswith("GET /v1/queue/Q")], [])
        self.assertEqual(self.requests(), [False])

    async def test_the_status_renders_once_a_minute_between_events_and_at_once_on_them(self) -> None:
        self.preset("low")
        clock = Clock()
        app = self.make_app()
        renders: list[float] = []
        original = StatusPane.show

        def spy(pane: StatusPane, *arguments: Any, **options: Any) -> None:
            renders.append(clock.now)
            original(pane, *arguments, **options)

        with mock.patch.object(StatusPane, "show", spy):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.running_job(pilot, clock)
                running = app.main.running  # type: ignore[union-attr]
                running.clock = clock
                clock.now = 2000
                running.tick()
                first = len(renders)
                clock.now = 2000 + LOW_STATUS_REFRESH_SECONDS - 1
                running.tick()
                running.tick()
                early = len(renders)
                clock.now = 2000 + LOW_STATUS_REFRESH_SECONDS
                running.tick()
                due = len(renders)
                running.tick()
                again = len(renders)
                await self.push(pilot, run_finished(1))
                after_run_finished = len(renders)
                await self.push(pilot, run_started(2))
                after_run_started = len(renders)
                await self.push(pilot, run_finished(2), cooldown_started(2))
                after_cooldown_started = len(renders)
        self.assertEqual((early, due, again), (first, first + 1, first + 1))
        self.assertGreater(after_run_finished, again)
        self.assertGreater(after_run_started, after_run_finished)
        self.assertGreater(after_cooldown_started, after_run_started)

    async def test_a_switch_into_low_mid_run_drops_the_step_reading_at_once(self) -> None:
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.push(pilot, run_output("Sampling... 4 / 8 [ ] 50  %", progress=(4, 8), percent=50))
            before = (self.text(app, "run-line"), self.text(app, "status"))
            # The screen's clock stands still: only a render the switch itself forces can change what is shown.
            await self.command(pilot, "/verbose low")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 2, "the reconnect")
            await pilot.pause()
            after = (self.text(app, "run-line"), self.text(app, "status"))
        self.assertIn("progress 4/8, 50%", before[0])
        self.assertIn("step 4/8", before[1])
        self.assertNotIn("progress", after[0])
        self.assertNotIn("step 4/8", after[1])
        self.assertNotIn("50%", after[1])

    async def test_an_attach_at_low_shows_no_step_reading(self) -> None:
        self.preset("low")
        self.server.running = [{"queue_id": "Q0003", "job_path": "/jobs/walk.yaml", "total_runs": 2, "submitted_at": "t"}]
        self.server.details["Q0003"] = {"queue_id": "Q0003", "execution_id": None, "current_run": 1, "current_run_elapsed_seconds": 12.0, "current_step": 3, "current_step_total": 8}
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.wait_for(pilot, lambda: self.pane(app), "the markers")
            run_line, status = self.text(app, "run-line"), self.text(app, "status")
        self.assertIn("Run 1/2", run_line)
        self.assertNotIn("progress", run_line)
        self.assertNotIn("step 3/8", status)

    async def test_a_stop_at_low_shows_at_once(self) -> None:
        self.preset("low")
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.command(pilot, "/stop")
            await self.wait_for(pilot, lambda: app.live is not None and app.live.stop_requested, "the stop to be confirmed")
            await pilot.pause()
            run_line = self.text(app, "run-line")
        self.assertIn("stopping", run_line)

    async def test_the_marker_and_notice_come_from_a_job_starting_seeded_or_a_switch_into_low(self) -> None:
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            await self.push(pilot, run_output("visible"))
            await self.command(pilot, "/verbose low")
            await self.wait_for(pilot, lambda: len(self.server.requests) == 2, "the reconnect")
            await pilot.pause()
            pane = self.pane(app)
            notice = self.since("/verbose low")
        self.assertEqual(pane, ["visible", LOW_MARKER])
        self.assertEqual(notice[0], LOW_NOTICE)

    async def test_a_seeded_job_at_low_has_both_markers(self) -> None:
        self.preset("low")
        self.server.running = [{"queue_id": "Q0009", "job_path": "/jobs/walk.yaml", "total_runs": 2, "submitted_at": "t"}]
        self.server.details["Q0009"] = {"queue_id": "Q0009", "execution_id": None, "current_run": None}
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.wait_for(pilot, lambda: self.pane(app), "the markers")
            pane = self.pane(app)
        self.assertEqual(pane, [EARLIER, LOW_MARKER])

    async def test_the_startup_notice_is_not_repeated_by_a_reconnect(self) -> None:
        self.preset("low")
        app = self.make_app()
        with mock.patch("draw_things_control.tui.feed.RECONNECT_SECONDS", 0.05):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.connect(pilot)
                self.server.calls[0].cancel()
                await self.wait_for(pilot, lambda: len(self.server.requests) == 2, "the reconnect")
                await self.wait_for(pilot, lambda: app.feed_connected, "the feed")
        self.assertEqual(self.said.count(LOW_NOTICE), 1)

    async def test_a_quit_prompt_shows_at_once_at_low_though_the_periodic_refresh_is_throttled(self) -> None:
        self.preset("low")
        clock = Clock()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.running_job(pilot, clock)
            running = app.main.running  # type: ignore[union-attr]
            running.clock = clock
            clock.now = 3000
            running.tick(force=True)
            app.quit_press.arm()
            clock.now = 3001
            running.tick()
            unforced = self.text(app, "status-line")
            app.show_status()
            forced = self.text(app, "status-line")
        self.assertNotIn("Ctrl-C again", unforced)
        self.assertIn("Press Ctrl-C again to quit", forced)


class OfflineTests(VerboseTestCase):
    async def test_a_switch_across_low_with_no_server_persists_and_applies_when_it_is_reachable(self) -> None:
        self.server.health_ok = False
        app = self.make_app()
        with mock.patch("draw_things_control.tui.feed.RECONNECT_SECONDS", 0.05):
            async with app.run_test(size=(160, 60)) as pilot:
                await self.settle(pilot)
                await self.command(pilot, "/verbose low")
                said = self.since("/verbose low")
                self.assertEqual(load_verbose_level(self.paths.tui_preferences), "low")
                self.assertEqual(self.server.requests, [])
                self.server.health_ok = True
                await self.wait_for(pilot, lambda: app.feed_connected, "the feed")
        self.assertEqual(said, ["Verbose level: low. It applies when dtc serve is reachable."])
        self.assertFalse(any("Reconnecting" in line for line in self.said))
        self.assertEqual(self.requests(), [False])

    async def test_a_preference_that_cannot_be_written_still_applies_and_says_so(self) -> None:
        # A directory where the file goes: the write fails with an OSError.
        self.paths.tui_preferences.mkdir()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/verbose medium")
            said = self.since("/verbose medium")
            level = app.verbose_level
        self.assertEqual(level, "medium")
        self.assertEqual(said[0], "Verbose level: medium.")
        self.assertIn("could not be saved", said[1])
        self.assertIn("this session only", said[1])
