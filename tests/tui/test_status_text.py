"""The work status and the run summary, read from the forecast the server published and the front end's own clock."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from typing import Any

from rich.text import Text

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobFinished, JobStarted, JobStatus, RunFinished, RunStarted, RunStatus
from framecontrol.tui.live_run import LiveRun
from framecontrol.tui.text.status import bar_line, end_text, run_summary_text, stage_text, whole_duration, work_status_lines

WALL = datetime(2026, 9, 26, 14, 0, 0)
AT = "2026-09-26T14:00:00+00:00"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def span(started: float, end: float, *, spread: float = 0.0, floor: float = 0.0) -> dict[str, float]:
    """A published span in seconds after 14:00:00, whose range is ``spread`` of the time from its start to its end each way."""
    base = WALL.timestamp()
    reach = spread * (end - started)
    return {"started_at": base + started, "end_at": base + end, "low": base + end - reach, "high": base + end + reach, "floor": floor}


def forecast(*, stage: str | None = "denoise", done: int | None = 12, total: int | None = 30, run: dict[str, float] | None = None, job: dict[str, float] | None = None) -> dict[str, Any]:
    return {"as_of": WALL.timestamp(), "stage": stage, "stage_done": done, "stage_total": total, "run": run, "job": job, "basis": {"tier": "signature", "fidelity": "live", "runs": 8, "executions": 4, "build_changed": False, "unseen": [], "penalty": None, "skipped": 0}}


class StatusTextCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def live(self, runs: int = 2, cooldown: CooldownPolicy | None = None) -> LiveRun:
        live = LiveRun(clock=self.clock, wall_clock=lambda: WALL + timedelta(seconds=self.clock.now))
        live.apply(JobStarted(at=AT, job_name="sunset-walk", job_file="/jobs/walk.yaml", source_text="", mode="i2v", total_runs=runs, output_directory="/out", input=None, model="base.ckpt", seed=1, seed_source="job", cooldown=cooldown or CooldownPolicy("manual", seconds=100.0), cooldown_source="job", manifest=None, log=None))
        return live

    def start(self, live: LiveRun, number: int) -> None:
        live.apply(RunStarted(at=AT, number=number, total=len(live.runs), pair="only", positive="walk", negative=None, input=None, resized_input=None, output=f"walk-{number}.mov", last_frame=None, command=()))

    def at(self, seconds: float) -> None:
        self.clock.now = seconds

    def lines(self, live: LiveRun | None, width: int = 70) -> list[str]:
        return [str(line) for line in work_status_lines(live, False, width)]


class EndTextTests(StatusTextCase):
    def test_the_end_is_a_clock_time_and_what_is_left_or_the_range_the_forecast_gives(self) -> None:
        now = datetime(2026, 9, 26, 16, 19, 0)
        self.assertEqual(end_text(23 * 60, now), "ends ~16:42 (in 23 min)")
        self.assertEqual(end_text(23 * 60, now, (12 * 60, 39 * 60)), "ends ~16:42 (16:31 to 16:58)")
        self.assertEqual(end_text(3600 * 11 + 1800.4, datetime(2026, 9, 26, 14, 40, 0)), "ends ~09-27 02:10 (in 11 h 30 min)")
        self.assertEqual(end_text(3600 * 11, datetime(2026, 9, 26, 14, 40, 0), (3600 * 10, 3600 * 12)), "ends ~09-27 01:40 (09-27 00:40 to 09-27 02:40)")

    def test_durations_in_whole_seconds_and_bars_that_are_always_whole(self) -> None:
        self.assertEqual(whole_duration(432.1), "7 min 12 s")
        self.assertEqual(whole_duration(-3), "0 s")
        tail = "ends ~16:42 (in 23 min)"
        for width in (60, 42, 36, 30, 10):
            with self.subTest(width=width):
                line = bar_line("Job", 0.38, tail, width)
                self.assertTrue(str(line).startswith("Job") and str(line).endswith(f"38%  {tail}"), str(line))
                self.assertEqual(len(str(line)), max(width, len(f"Job 38%  {tail}")))
        self.assertEqual(str(bar_line("Run", 0.5, "x", 17)), "Run ███░░░ 50%  x")
        self.assertEqual(str(bar_line("Run", None, "estimating", 24)), "Run ░░░░░░░░░ estimating")
        self.assertEqual(str(bar_line("Job", 1.0, "x", 5)), "Job 100%  x")

    def test_a_stage_is_its_name_with_spaces_and_its_units(self) -> None:
        self.assertEqual(stage_text(forecast(stage="denoise", done=12, total=30)), "denoise 12/30")
        self.assertEqual(stage_text(forecast(stage="text_encode", done=3, total=50)), "text encode 3/50")
        self.assertEqual(stage_text(forecast(stage="finishing", done=None, total=None)), "finishing")
        self.assertIsNone(stage_text(forecast(stage=None, done=None, total=None)))
        self.assertIsNone(stage_text(None))


class WorkStatusTests(StatusTextCase):
    def test_the_bars_read_the_published_ends_and_show_the_range_only_when_it_is_wide(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(40)
        # Run 1 ends at 14:07:00 with a range of 1% either way (4 s); the job ends at 14:15:40 with 8% (about 75 s).
        live.apply_forecast(forecast(run=span(0, 420, spread=0.01), job=span(0, 940, spread=0.08)))
        lines = self.lines(live)
        self.assertTrue(lines[1].startswith("Job ") and "4%  ends ~14:15 (14:14 to 14:16)" in lines[1], lines[1])
        self.assertIn("9%  ends ~14:07 (in 6 min 20 s)", lines[2])
        self.assertEqual(lines[3], "denoise 12/30  40 s elapsed")
        self.assertEqual(lines[0], f"running  {live.path.stem}  run 1/2")  # type: ignore[union-attr]

    def test_with_no_forecast_or_no_number_the_bars_say_estimating(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(10)
        lines = self.lines(live)
        self.assertTrue(lines[1].endswith("estimating") and lines[2].endswith("estimating"), lines)
        self.assertEqual(lines[3], "10 s elapsed")
        live.apply_forecast(forecast(run=None, job=None))
        lines = self.lines(live)
        self.assertTrue(lines[1].endswith("estimating") and lines[2].endswith("estimating"), lines)
        self.assertEqual(lines[3], "denoise 12/30  10 s elapsed")

    def test_the_bar_never_falls_below_the_floor_the_server_held(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(100)
        live.apply_forecast(forecast(run=span(0, 1000, floor=0.6), job=span(0, 2000, floor=0.0)))
        lines = self.lines(live)
        self.assertIn(" 60%  ", lines[2])
        self.assertIn(" 5%  ", lines[1])

    def test_a_park_reserved_says_parking_in_place_of_the_jobs_end(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(40)
        live.apply_forecast(forecast(run=span(0, 420), job=span(0, 420)))
        live.park_requested = True
        lines = self.lines(live)
        self.assertTrue(lines[1].endswith("parking"), lines[1])
        self.assertIn("ends ~14:07", lines[2])
        self.assertTrue(lines[0].startswith("parking after run 1/2"), lines[0])

    def test_a_park_reserved_before_the_server_has_a_number_still_says_parking_and_a_stop_wins_over_it(self) -> None:
        live = self.live()
        self.start(live, 1)
        live.park_requested = True
        self.assertTrue(self.lines(live)[1].endswith("parking"))
        live.request_stop()
        self.assertTrue(self.lines(live)[1].endswith("stopping"))

    def test_a_stop_freezes_the_bars_where_it_found_them(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(50)
        live.apply_forecast(forecast(run=span(0, 500), job=span(0, 1000)))
        live.request_stop()
        self.at(400)
        lines = self.lines(live)
        self.assertIn(" 10%  stopping", lines[2])
        self.assertIn(" 5%  stopping", lines[1])
        self.assertTrue(lines[0].startswith("stopping"), lines[0])

    def test_during_a_cooldown_the_wait_is_exact_and_the_job_counts_what_is_left(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(420)
        live.apply(RunFinished(at=AT, number=1, status=RunStatus.SUCCEEDED, exit_code=0, seconds=400.0, output="walk-1.mov", last_frame=None))
        live.apply(CooldownStarted(at=AT, after_run=1, seconds=100.0, until="14:08:40"))
        live.apply_forecast(forecast(stage=None, done=None, total=None, run=None, job=span(0, 1000)))
        self.at(445)
        lines = self.lines(live)
        self.assertTrue(lines[2].startswith("Wait ") and "25%  ends ~14:08 (in 1 min 15 s)" in lines[2], lines[2])
        self.assertEqual((lines[3], lines[4]), ("next: run 2/2", "last run took 6 min 40 s"))
        self.assertIn("ends ~14:16 (in 9 min 15 s)", lines[1])

    def test_the_lines_when_a_job_is_over_or_never_started(self) -> None:
        self.assertEqual([str(line) for line in work_status_lines(None, False, 42)], ["idle", "", "", "", ""])
        self.assertEqual(str(work_status_lines(None, True, 42)[0]), "A job is running in another process")
        live = self.live()
        self.start(live, 1)
        live.apply_forecast(forecast(run=span(0, 420), job=span(0, 940)))
        self.at(450)
        live.apply(RunFinished(at=AT, number=1, status=RunStatus.SUCCEEDED, exit_code=0, seconds=430.0, output="walk-1.mov", last_frame=None))
        live.apply(CooldownEnded(at=AT, waited_seconds=100.0, cut_short=False))
        self.at(1000)
        live.apply(JobFinished(at=AT, status=JobStatus.SUCCEEDED, exit_code=0, completed_runs=2, total_runs=2, signal=None))
        self.assertIsNone(live.forecast)
        lines = self.lines(live)
        self.assertEqual(lines[0], f"finished (succeeded)  {live.path.stem}")  # type: ignore[union-attr]
        self.assertEqual(lines[1], "2/2 runs succeeded")
        never = LiveRun(clock=self.clock)
        never.end(error="The run lock is held")
        self.assertEqual([str(line) for line in work_status_lines(never, False, 60)], ["did not start  ", "", "", "", ""])
        self.assertIsInstance(work_status_lines(never, False, 60)[0], Text)


class RunSummaryTests(StatusTextCase):
    def test_the_run_summary_shows_the_stage_and_its_units_at_every_verbose_level(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(90)
        for level in ("high", "medium", "low"):
            self.assertNotIn("stage", str(run_summary_text(live, level)))  # type: ignore[arg-type]
        live.apply_forecast(forecast(stage="text_encode", done=3, total=50))
        for level in ("high", "medium", "low"):
            with self.subTest(level):
                summary = str(run_summary_text(live, level))  # type: ignore[arg-type]
                self.assertTrue(summary.startswith("Run 1/2  1 min 30 s elapsed  stage text encode 3/50"), summary)
        self.assertNotIn("progress", str(run_summary_text(live, "high")))

    def test_a_cleared_forecast_leaves_the_summary_without_a_stage(self) -> None:
        live = self.live()
        self.start(live, 1)
        live.apply_forecast(forecast())
        live.apply_forecast(None)
        self.assertNotIn("stage", str(run_summary_text(live)))

    def test_the_summary_says_when_the_estimate_rests_on_learned_or_pinned_knobs(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(90)
        live.apply_forecast(forecast(stage="denoise", done=3, total=30))
        self.assertNotIn("estimate", str(run_summary_text(live)))
        for knobs, words in (("provisional", "estimate from provisional calibration"), ("pinned", "estimate with pinned timing knobs"), ("factory", None)):
            published = forecast(stage="denoise", done=3, total=30)
            published["basis"] = {**published["basis"], "knobs": knobs}
            live.apply_forecast(published)
            summary = str(run_summary_text(live))
            with self.subTest(knobs):
                if words is None:
                    self.assertNotIn("estimate", summary)
                else:
                    self.assertIn(words, summary)
