"""Tests for the Status widget's estimates and text, from a LiveRun driven by synthetic events on a fake clock."""

from __future__ import annotations

from datetime import datetime, timedelta

from rich.text import Text

from draw_things_control.core.global_config import CooldownPolicy
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_events import CooldownEnded, CooldownStarted, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted
from draw_things_control.tui.estimate import job_estimate, moment, run_estimate
from draw_things_control.tui.live_run import LiveRun, PastRun
from draw_things_control.tui.text import bar_line, end_text, status_lines, whole_duration
from tests.fixtures import JobTestCase, job_data

WALL = datetime(2026, 9, 26, 14, 0, 0)
MANUAL_100 = CooldownPolicy("manual", seconds=100.0)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class EstimateTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()

    def live(self, runs: int = 3, cooldown: CooldownPolicy = MANUAL_100) -> LiveRun:
        path = self.write_job(job_data(run_count=runs, prompt_pairs=[{"name": "only", "positive": "walk"}]))
        live = LiveRun(load_job(path, self.global_config, self.dt_config), path, clock=self.clock, wall_clock=lambda: WALL + timedelta(seconds=self.clock.now))
        live.apply(JobStarted(at=self.stamp(), job_name="sunset-walk", job_file=str(path), source_text="", mode="i2v", total_runs=runs, output_directory="/out", input=None, model="base.ckpt", seed=1, seed_source="job", cooldown=cooldown, cooldown_source="job", manifest=None, log=None))
        return live

    def stamp(self) -> str:
        return (WALL + timedelta(seconds=self.clock.now)).astimezone().isoformat(timespec="seconds")

    def at(self, seconds: float) -> None:
        self.clock.now = seconds

    def start(self, live: LiveRun, number: int) -> None:
        live.apply(RunStarted(at=self.stamp(), number=number, total=len(live.runs), pair="only", positive="walk", negative=None, input=None, resized_input=None, output=f"walk-{number}.mov", last_frame=None, command=()))

    def step(self, live: LiveRun, at: float, step: int, total: int = 40) -> None:
        self.at(at)
        live.apply(RunOutput(at=self.stamp(), number=live.active_run or 0, stream="stdout", text=f"{step}/{total}", progress=(step, total), percent=None))

    def finish(self, live: LiveRun, at: float, number: int, seconds: float, status: str = "succeeded") -> None:
        self.at(at)
        live.apply(RunFinished(at=self.stamp(), number=number, status=status, exit_code=0 if status == "succeeded" else 1, seconds=seconds, output=f"walk-{number}.mov", last_frame=None))

    def one_run(self, live: LiveRun, number: int, begin: float, *, load: float = 30.0, per_step: float = 10.0, tail: float = 20.0) -> float:
        """A whole run: loading, 40 steps, and a tail; returns when it finished."""
        self.at(begin)
        self.start(live, number)
        for step in range(1, 41):
            self.step(live, begin + load + step * per_step, step)
        end = begin + load + 40 * per_step + tail
        self.finish(live, end, number, seconds=load + 40 * per_step)
        return end

    def test_the_run_rate_leaves_out_loading_and_needs_two_readings(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.at(25)
        self.assertEqual(run_estimate(live, 25), run_estimate(live, 25).__class__(None, None))
        self.step(live, 30, 1)
        self.assertEqual((run_estimate(live, 30).fraction, run_estimate(live, 30).remaining), (1 / 40, None))
        self.step(live, 40, 2)
        # 10 s a step, from the first reading on: the 30 s of loading are not counted.
        self.assertEqual(run_estimate(live, 40).remaining, 380.0)
        # Between steps the time left keeps counting down.
        self.assertEqual(run_estimate(live, 45).remaining, 375.0)
        self.assertEqual(run_estimate(live, 40).fraction, 2 / 40)

    def test_a_pause_at_the_refiner_switch_is_spread_over_the_steps_and_settles(self) -> None:
        live = self.live()
        self.start(live, 1)
        for step in range(1, 5):
            self.step(live, step * 10.0, step)
        steady = run_estimate(live, 40).remaining
        # Loading the refiner stalls the counter for 60 s after step 4; it keeps counting on at 5/40.
        self.step(live, 110, 5)
        after_switch = run_estimate(live, 110).remaining
        for step in range(6, 21):
            self.step(live, 110 + (step - 5) * 10.0, step)
        later = run_estimate(live, self.clock.now).remaining
        assert steady is not None and after_switch is not None and later is not None
        self.assertEqual(steady, 360.0)
        self.assertGreater(after_switch / 35, steady / 36)
        self.assertLess(later / 20, after_switch / 35)

    def test_a_counter_that_goes_back_or_changes_its_total_starts_the_rate_over(self) -> None:
        live = self.live()
        self.start(live, 1)
        for at, step in ((10, 10), (20, 11), (30, 12)):
            self.step(live, at, step)
        self.step(live, 40, 1)
        self.assertIsNone(run_estimate(live, 40).remaining)
        self.step(live, 45, 2)
        self.assertEqual(run_estimate(live, 45).remaining, 38 * 5.0)
        self.step(live, 50, 3, total=20)
        self.assertEqual((run_estimate(live, 50).fraction, run_estimate(live, 50).remaining), (3 / 20, None))

    def test_after_the_last_step_the_run_is_finishing_and_the_job_counts_the_last_tail(self) -> None:
        live = self.live()
        end = self.one_run(live, 1, 0, tail=20)
        self.at(end + 100)
        live.apply(CooldownStarted(at=self.stamp(), after_run=1, seconds=0.0, until="14:00:00"))
        live.apply(CooldownEnded(at=self.stamp(), waited_seconds=0.0, cut_short=False))
        self.start(live, 2)
        for step in range(1, 41):
            self.step(live, end + 130 + step * 10.0, step)
        run = run_estimate(live, self.clock.now + 5)
        self.assertTrue(run.finishing)
        self.assertEqual((run.fraction, run.remaining), (1.0, 15.0))
        # Run 1 had no earlier tail, so its finishing counts nothing.
        first = self.live()
        self.start(first, 1)
        self.step(first, 10, 39)
        self.step(first, 20, 40)
        self.assertEqual(run_estimate(first, 30).remaining, 0.0)

    def test_the_job_times_runs_by_their_full_time_and_waits_by_their_draw_things_time(self) -> None:
        for policy, wait in ((CooldownPolicy("manual", seconds=100.0), 100.0), (CooldownPolicy("auto", ratio=0.5), 215.0), (CooldownPolicy("off"), 0.0)):
            with self.subTest(policy.mode):
                self.at(0)
                live = self.live(runs=3, cooldown=policy)
                end = self.one_run(live, 1, 0)
                # Run 1: 30 s loading and 400 s of steps (430 s draw-things-cli time), and a 20 s tail: 450 s in all.
                self.assertEqual(end, 450)
                self.at(460)
                self.start(live, 2)
                self.step(live, 490, 1)
                self.step(live, 500, 2)
                # Run 2's 380 s left, then run 3 at 450 s, with a wait before it (auto: half of 430 s).
                estimate = job_estimate(live, 500)
                self.assertEqual(estimate.remaining, 380 + wait + 450)
                self.assertAlmostEqual(estimate.fraction or 0, 500 / (500 + 380 + wait + 450))

    def test_before_a_runs_rate_the_job_keeps_its_estimate_from_run_2_on(self) -> None:
        live = self.live(runs=2, cooldown=CooldownPolicy("off"))
        self.start(live, 1)
        self.at(10)
        self.assertEqual(job_estimate(live, 10).remaining, None)
        end = self.one_run(live, 1, 0)
        self.at(end + 10)
        self.start(live, 2)
        self.at(end + 40)
        # Loading run 2: the job counts it as run 1's full 450 s less the 30 s so far; the run itself, from run 1's
        # draw-things-cli time (430 s), has 400 s left.
        self.assertEqual(job_estimate(live, end + 40).remaining, 420)
        self.assertEqual(run_estimate(live, end + 40).remaining, 400)

    def test_before_any_report_a_run_is_estimated_from_the_latest_successful_run_of_any_job(self) -> None:
        live = self.live(runs=2, cooldown=CooldownPolicy("manual", seconds=60.0))
        self.assertEqual(run_estimate(live, 0), run_estimate(live, 0).__class__())
        live.past_run = PastRun(430.0, 40)
        self.start(live, 1)
        self.at(30)
        run = run_estimate(live, 30)
        self.assertEqual((run.remaining, run.fraction), (400.0, 30 / 430))
        # The job has an estimate from the start too: this run as the past run says, the wait, and run 2 as long.
        self.assertEqual(job_estimate(live, 30).remaining, 400 + 60 + 430)
        self.assertIn("ends ~14:07 (in 6 min 40 s)", str(status_lines(live, False, 60)[2]))
        # A run that takes longer than the past one no longer has an estimate from it.
        self.assertIsNone(run_estimate(live, 431).remaining)

    def test_the_first_report_rescales_the_past_run_and_the_second_uses_the_live_rate(self) -> None:
        live = self.live()
        live.past_run = PastRun(430.0, 40)
        self.start(live, 1)
        self.step(live, 30, 1)
        # 430 s over 40 steps is 10.75 s a step, for the 39 steps left.
        self.assertEqual(run_estimate(live, 30).remaining, 39 * 10.75)
        self.step(live, 38, 2)
        # The live rate takes over: 8 s a step.
        self.assertEqual(run_estimate(live, 38).remaining, 38 * 8.0)
        self.at(0)
        unknown = self.live()
        unknown.past_run = PastRun(430.0, None)
        self.start(unknown, 1)
        self.step(unknown, 30, 1)
        # Without the past run's step count, its time less the time elapsed stands until the rate.
        self.assertEqual(run_estimate(unknown, 30).remaining, 400.0)
        self.at(0)
        other = self.live()
        other.past_run = PastRun(430.0, 40)
        self.start(other, 1)
        self.step(other, 30, 1, total=20)
        # The counter counts 20 steps, not the past run's 40 (an image-to-image run, say): its time per step would halve
        # the estimate, so its time less the time elapsed stands.
        self.assertEqual(run_estimate(other, 30).remaining, 400.0)

    def test_a_run_slower_than_its_measure_says_estimating_not_0_s(self) -> None:
        live = self.live(runs=2, cooldown=CooldownPolicy("off"))
        live.past_run = PastRun(430.0, 40)
        self.start(live, 1)
        self.step(live, 30, 1)
        # 39 steps at 10.75 s are 419.25 s; past them, the past run no longer says.
        self.assertIsNone(run_estimate(live, 30 + 420).remaining)
        self.assertEqual(run_estimate(live, 30 + 420).fraction, 1 / 40)
        self.assertIsNone(job_estimate(live, 30 + 420).remaining)
        self.assertNotIn("in 0 s", str(status_lines(live, False, 60)))
        self.at(0)
        live = self.live(runs=3, cooldown=CooldownPolicy("off"))
        end = self.one_run(live, 1, 0)
        self.at(end)
        self.start(live, 2)
        # Run 2 loads for longer than run 1's full 450 s: the job no longer counts it as W less its elapsed time.
        self.at(end + 460)
        self.assertIsNone(job_estimate(live, end + 460).remaining)

    def test_this_jobs_own_last_run_is_preferred_to_the_stores(self) -> None:
        live = self.live(cooldown=CooldownPolicy("off"))
        live.past_run = PastRun(100.0, 10)
        self.assertEqual(live.reference_run(), PastRun(100.0, 10))
        end = self.one_run(live, 1, 0)
        self.assertEqual(live.reference_run(), PastRun(430.0, 40))
        self.at(end + 10)
        self.start(live, 2)
        self.at(end + 40)
        self.assertEqual(run_estimate(live, end + 40).remaining, 400.0)

    def test_on_run_1_the_job_estimate_follows_the_run(self) -> None:
        live = self.live(runs=2, cooldown=CooldownPolicy("manual", seconds=60.0))
        self.start(live, 1)
        self.step(live, 30, 1)
        self.step(live, 40, 2)
        # The run so far (40 s) and its 380 s left make W and C; then the wait and run 2.
        self.assertEqual(job_estimate(live, 40).remaining, 380 + 60 + 420)

    def test_during_a_cooldown_the_countdown_stands_for_the_run_and_its_wait(self) -> None:
        live = self.live(runs=3, cooldown=CooldownPolicy("manual", seconds=100.0))
        end = self.one_run(live, 1, 0)
        live.apply(CooldownStarted(at=self.stamp(), after_run=1, seconds=100.0, until="14:09:10"))
        self.assertEqual(job_estimate(live, end + 40).remaining, 60 + 450 + 100 + 450)

    def test_a_stop_freezes_the_estimates_where_it_found_them(self) -> None:
        live = self.live()
        self.start(live, 1)
        self.step(live, 30, 1)
        self.step(live, 40, 2)
        self.at(50)
        live.request_stop()
        self.at(500)
        self.assertEqual(moment(live), 50)
        lines = [str(line) for line in status_lines(live, False, 60)]
        self.assertIn("stopping", lines[1])
        self.assertIn("stopping", lines[2])
        self.assertTrue(lines[0].startswith("stopping"), lines[0])

    def test_end_times_durations_and_bars(self) -> None:
        self.assertEqual(end_text(23 * 60, datetime(2026, 9, 26, 16, 19, 0)), "ends ~16:42 (in 23 min)")
        self.assertEqual(end_text(3600 * 11 + 1800.4, datetime(2026, 9, 26, 14, 40, 0)), "ends ~09-27 02:10 (in 11 h 30 min)")
        self.assertEqual(whole_duration(432.1), "7 min 12 s")
        self.assertEqual(whole_duration(-3), "0 s")
        tail = "ends ~16:42 (in 23 min)"
        for width in (60, 42, 36, 30, 10):
            with self.subTest(width=width):
                line = bar_line("Job", 0.38, tail, width)
                # The text is always whole; only the bar gives way.
                self.assertTrue(str(line).startswith("Job") and str(line).endswith(f"38%  {tail}"), str(line))
                self.assertEqual(len(str(line)), max(width, len(f"Job 38%  {tail}")))
        self.assertEqual(str(bar_line("Run", 0.5, "x", 17)), "Run ███░░░ 50%  x")
        # Without an estimate the bar is empty and there is no percentage.
        self.assertEqual(str(bar_line("Run", None, "estimating", 24)), "Run ░░░░░░░░░ estimating")
        self.assertEqual(str(bar_line("Job", 1.0, "x", 5)), "Job 100%  x")

    def test_the_status_lines_in_each_state(self) -> None:
        self.assertEqual([str(line) for line in status_lines(None, False, 42)], [""] * 5)
        self.assertEqual(str(status_lines(None, True, 42)[0]), "A job is running in another process")
        live = self.live(runs=2, cooldown=CooldownPolicy("manual", seconds=100.0))
        self.start(live, 1)
        self.step(live, 30, 1)
        self.step(live, 40, 2)
        lines = [str(line) for line in status_lines(live, False, 60)]
        self.assertEqual(lines[0], f"running  {live.path.name}  run 1/2")
        # 380 s left in run 1, the 100 s wait, and run 2 at run 1's 420 s so far: 900 s from 14:00:40.
        self.assertTrue(lines[1].startswith("Job ") and lines[1].endswith("4%  ends ~14:15 (in 15 min)"), lines[1])
        self.assertIn("ends ~14:07 (in 6 min 20 s)", lines[2])
        self.assertEqual(lines[3], "step 2/40  40 s elapsed")
        self.assertEqual(lines[4], "")
        end = self.one_run(live, 1, 0)
        live.apply(CooldownStarted(at=self.stamp(), after_run=1, seconds=100.0, until="14:09:10"))
        self.at(end + 25)
        lines = [str(line) for line in status_lines(live, False, 60)]
        self.assertTrue(lines[2].startswith("Wait ") and "25%  ends ~14:09 (in 1 min 15 s)" in lines[2], lines[2])
        self.assertEqual((lines[3], lines[4]), ("next: run 2/2", "last run took 7 min 10 s"))
        live.apply(CooldownEnded(at=self.stamp(), waited_seconds=100.0, cut_short=False))
        self.one_run(live, 2, end + 100)
        live.apply(JobFinished(at=self.stamp(), status="succeeded", exit_code=0, completed_runs=2, total_runs=2, signal=None))
        lines = [str(line) for line in status_lines(live, True, 60)]
        # A job another process starts afterwards is announced in its place.
        self.assertEqual(lines[0], f"finished (succeeded)  {live.path.name}")
        # Run 1 from 0 s to 450 s, the 100 s wait, and run 2 to 1000 s.
        self.assertEqual(lines[1:], ["2/2 runs succeeded", "", "job took 16 min 40 s", "last run took 7 min 10 s"])

    def test_a_failed_run_does_not_replace_the_last_run_and_a_job_that_did_not_start_says_so(self) -> None:
        live = self.live(runs=3)
        self.one_run(live, 1, 0)
        self.start(live, 2)
        self.finish(live, self.clock.now + 5, 2, seconds=5.0, status="failed")
        self.assertEqual(str(status_lines(live, False, 60)[4]), "last run took 7 min 10 s")
        path = self.write_job(job_data())
        never = LiveRun(load_job(path, self.global_config, self.dt_config), path, clock=self.clock)
        never.end("The run lock is held")
        self.assertEqual([str(line) for line in status_lines(never, False, 60)], [f"did not start  {path.name}", "", "", "", ""])
        self.assertIsInstance(status_lines(never, False, 60)[0], Text)
