"""The frozen copy of the TUI's old estimator: it gives the estimates the code it was taken from gave, and keeps the faults the forecast replaces."""

from __future__ import annotations

import unittest

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobStarted, RunFinished, RunOutput, RunStarted, RunStatus
from tests.timing.frozen_estimator import FrozenEstimator
from tests.timing.frozen_estimator import PastRun as FrozenPastRun

AT = "2026-09-26T14:00:00+00:00"


def job_started(runs: int, cooldown: CooldownPolicy, first_run: int = 1) -> JobStarted:
    return JobStarted(at=AT, job_name="walk", job_file="/jobs/walk.yaml", source_text="", mode="i2v", total_runs=runs, output_directory="/out", input=None, model="base.ckpt", seed=1, seed_source="job", cooldown=cooldown, cooldown_source="job", manifest=None, log=None, first_run=first_run)


def run_started(number: int, total: int) -> RunStarted:
    return RunStarted(at=AT, number=number, total=total, pair="only", positive="walk", negative=None, input=None, resized_input=None, output=f"walk-{number}.mov", last_frame=None, command=())


def reading(number: int, stage: str, phase: str, done: int | None = None, total: int | None = None) -> RunOutput:
    """What the executor emits for a stage reading: the stage and counter, and no text of its own."""
    return RunOutput(at=AT, number=number, stream="stdout", text="", progress=None, percent=None, phase=phase, offset=0.0, stage=stage, done=done, total=total)


def run_finished(number: int, seconds: float) -> RunFinished:
    return RunFinished(at=AT, number=number, status=RunStatus.SUCCEEDED, exit_code=0, seconds=seconds, output=f"walk-{number}.mov", last_frame=None)


class Rig:
    """A frozen estimator, a clock, and the events of a Draw Things job, in the terms the old estimator's own tests used."""

    def __init__(self, runs: int = 3, cooldown: CooldownPolicy | None = None, past: tuple[float, int | None] | None = None) -> None:
        self.runs = runs
        self.now = 0.0
        self.estimator = FrozenEstimator(past_run=FrozenPastRun(*past) if past is not None else None)
        self.active = 0
        self.estimator.observe(job_started(runs, cooldown or CooldownPolicy("manual", seconds=100.0)), 0.0)

    def at(self, seconds: float) -> None:
        self.now = seconds

    def start(self, number: int) -> None:
        self.active = number
        self.estimator.observe(run_started(number, self.runs), self.now)

    def step(self, at: float, step: int, total: int = 40) -> None:
        """The CLI printing `Sampling... step / total`: step - 1 steps are done."""
        self.now = at
        self.estimator.observe(reading(self.active, "sampling", "Sampling", step - 1, total), at)

    def finish(self, at: float, number: int, seconds: float, status: RunStatus = RunStatus.SUCCEEDED) -> None:
        self.now = at
        self.estimator.observe(RunFinished(at=AT, number=number, status=status, exit_code=0, seconds=seconds, output=f"walk-{number}.mov", last_frame=None), at)

    def one_run(self, number: int, begin: float, *, load: float = 30.0, per_step: float = 10.0, tail: float = 20.0) -> float:
        """A whole run: loading, 40 steps, and a tail; returns when it finished."""
        self.at(begin)
        self.start(number)
        for step in range(1, 41):
            self.step(begin + load + step * per_step, step)
        end = begin + load + 40 * per_step + tail
        self.finish(end, number, seconds=load + 40 * per_step)
        return end

    def run(self, at: float | None = None):
        return self.estimator.run_estimate(self.now if at is None else at)

    def job(self, at: float | None = None):
        return self.estimator.job_estimate(self.now if at is None else at)


class FrozenEstimatesAsTheOldCodeDid(unittest.TestCase):
    """The old estimator's own expectations, kept as they were when its code was removed (they were taken from its tests)."""

    def test_the_run_rate_leaves_out_loading_and_needs_two_readings(self) -> None:
        rig = Rig()
        rig.start(1)
        self.assertEqual((rig.run(25).fraction, rig.run(25).remaining), (None, None))
        rig.step(30, 1)
        self.assertEqual((rig.run(30).fraction, rig.run(30).remaining), (1 / 40, None))
        rig.step(40, 2)
        # 10 s a step, from the first reading on: the 30 s of loading are not counted.
        self.assertEqual(rig.run(40).remaining, 380.0)
        self.assertEqual(rig.run(45).remaining, 375.0)

    def test_a_pause_at_the_refiner_switch_is_spread_over_the_steps_and_settles(self) -> None:
        rig = Rig()
        rig.start(1)
        for step in range(1, 5):
            rig.step(step * 10.0, step)
        steady = rig.run(40).remaining
        rig.step(110, 5)
        after_switch = rig.run(110).remaining
        for step in range(6, 21):
            rig.step(110 + (step - 5) * 10.0, step)
        later = rig.run().remaining
        assert steady is not None and after_switch is not None and later is not None
        self.assertEqual(steady, 360.0)
        self.assertGreater(after_switch / 35, steady / 36)
        self.assertLess(later / 20, after_switch / 35)

    def test_a_counter_that_goes_back_or_changes_its_total_starts_the_rate_over(self) -> None:
        rig = Rig()
        rig.start(1)
        for at, step in ((10, 10), (20, 11), (30, 12)):
            rig.step(at, step)
        rig.step(40, 1)
        self.assertIsNone(rig.run().remaining)
        rig.step(45, 2)
        self.assertEqual(rig.run().remaining, 38 * 5.0)
        rig.step(50, 3, total=20)
        self.assertEqual((rig.run().fraction, rig.run().remaining), (3 / 20, None))

    def test_after_the_last_step_the_run_is_finishing_and_the_job_counts_the_last_tail(self) -> None:
        rig = Rig()
        end = rig.one_run(1, 0, tail=20)
        rig.at(end + 100)
        rig.estimator.observe(CooldownStarted(at=AT, after_run=1, seconds=0.0, until="14:00:00"), rig.now)
        rig.estimator.observe(CooldownEnded(at=AT, waited_seconds=0.0, cut_short=False), rig.now)
        rig.start(2)
        for step in range(1, 41):
            rig.step(end + 130 + step * 10.0, step)
        run = rig.run(rig.now + 5)
        self.assertTrue(run.finishing)
        self.assertEqual((run.fraction, run.remaining), (1.0, 15.0))
        # Run 1 had no earlier tail, so its finishing counts nothing.
        first = Rig()
        first.start(1)
        first.step(10, 39)
        first.step(20, 40)
        self.assertEqual(first.run(30).remaining, 0.0)

    def test_the_job_times_runs_by_their_full_time_and_waits_by_their_draw_things_time(self) -> None:
        for policy, wait in ((CooldownPolicy("manual", seconds=100.0), 100.0), (CooldownPolicy("auto", ratio=0.5), 215.0), (CooldownPolicy("off"), 0.0)):
            with self.subTest(policy.mode):
                rig = Rig(runs=3, cooldown=policy)
                end = rig.one_run(1, 0)
                # Run 1: 30 s loading and 400 s of steps (430 s draw-things-cli time), and a 20 s tail: 450 s in all.
                self.assertEqual(end, 450)
                rig.at(460)
                rig.start(2)
                rig.step(490, 1)
                rig.step(500, 2)
                estimate = rig.job()
                # Run 2's 380 s left, then run 3 at 450 s, with a wait before it (auto: half of 430 s).
                self.assertEqual(estimate.remaining, 380 + wait + 450)
                self.assertAlmostEqual(estimate.fraction or 0, 500 / (500 + 380 + wait + 450))

    def test_before_a_runs_rate_the_job_keeps_its_estimate_from_run_2_on(self) -> None:
        rig = Rig(runs=2, cooldown=CooldownPolicy("off"))
        rig.start(1)
        rig.at(10)
        self.assertIsNone(rig.job().remaining)
        end = rig.one_run(1, 0)
        rig.at(end + 10)
        rig.start(2)
        rig.at(end + 40)
        # Loading run 2: the job counts it as run 1's full 450 s less the 30 s so far; the run itself, from run 1's
        # draw-things-cli time (430 s), has 400 s left.
        self.assertEqual(rig.job().remaining, 420)
        self.assertEqual(rig.run().remaining, 400)

    def test_before_any_report_a_run_is_estimated_from_the_latest_successful_run_of_any_job(self) -> None:
        rig = Rig(runs=2, cooldown=CooldownPolicy("manual", seconds=60.0), past=(430.0, 40))
        rig.start(1)
        rig.at(30)
        run = rig.run()
        self.assertEqual((run.remaining, run.fraction), (400.0, 30 / 430))
        # The job has an estimate from the start too: this run as the past run says, the wait, and run 2 as long.
        self.assertEqual(rig.job().remaining, 400 + 60 + 430)
        # A run that takes longer than the past one no longer has an estimate from it.
        self.assertIsNone(rig.run(431).remaining)

    def test_the_first_report_rescales_the_past_run_and_the_second_uses_the_live_rate(self) -> None:
        rig = Rig(past=(430.0, 40))
        rig.start(1)
        rig.step(30, 1)
        # 430 s over 40 steps is 10.75 s a step, for the 39 steps left.
        self.assertEqual(rig.run().remaining, 39 * 10.75)
        rig.step(38, 2)
        # The live rate takes over: 8 s a step.
        self.assertEqual(rig.run().remaining, 38 * 8.0)
        unknown = Rig(past=(430.0, None))
        unknown.start(1)
        unknown.step(30, 1)
        # Without the past run's step count, its time less the time elapsed stands until the rate.
        self.assertEqual(unknown.run().remaining, 400.0)
        other = Rig(past=(430.0, 40))
        other.start(1)
        other.step(30, 1, total=20)
        # The counter counts 20 steps, not the past run's 40: its time per step would halve the estimate, so its time less
        # the time elapsed stands.
        self.assertEqual(other.run().remaining, 400.0)

    def test_this_jobs_own_last_run_is_preferred_to_the_stores(self) -> None:
        rig = Rig(cooldown=CooldownPolicy("off"), past=(100.0, 10))
        self.assertEqual(rig.estimator.reference_run(), FrozenPastRun(100.0, 10))
        end = rig.one_run(1, 0)
        self.assertEqual(rig.estimator.reference_run(), FrozenPastRun(430.0, 40))
        rig.at(end + 10)
        rig.start(2)
        rig.at(end + 40)
        self.assertEqual(rig.run().remaining, 400.0)

    def test_during_a_cooldown_the_countdown_stands_for_the_run_and_its_wait(self) -> None:
        rig = Rig(runs=3, cooldown=CooldownPolicy("manual", seconds=100.0))
        end = rig.one_run(1, 0)
        rig.estimator.observe(CooldownStarted(at=AT, after_run=1, seconds=100.0, until="14:09:10"), end)
        self.assertEqual(rig.job(end + 40).remaining, 60 + 450 + 100 + 450)

    def test_a_stop_freezes_the_estimates_where_it_found_them(self) -> None:
        rig = Rig()
        rig.start(1)
        rig.step(30, 1)
        rig.step(40, 2)
        rig.estimator.request_stop(50)
        self.assertEqual(rig.estimator.moment(500), 50)


class FrozenKeepsTheFaultsTests(unittest.TestCase):
    """Milestone 10's evidence of what the step-counter heuristics get wrong, kept so the replay measures the change."""

    def estimator(self) -> FrozenEstimator:
        estimator = FrozenEstimator()
        estimator.observe(job_started(2, CooldownPolicy("off")), 0.0)
        estimator.observe(run_started(1, 2), 0.0)
        return estimator

    def test_an_early_h3_counter_that_is_already_complete_reads_as_finishing(self) -> None:
        estimator = self.estimator()
        estimator.observe(reading(1, "tokenize", "tokenizer", 1, 1), 0.7)
        run = estimator.run_estimate(1.0)
        self.assertTrue(run.finishing)
        self.assertEqual((run.fraction, run.remaining), (1.0, 0.0))

    def test_draw_things_last_step_line_reads_as_the_end_of_the_run(self) -> None:
        estimator = self.estimator()
        estimator.observe(reading(1, "sampling", "Sampling", 39, 40), 100.0)
        # `Sampling... 40 / 40` is printed as the last step begins, and the old reading takes it for the end.
        self.assertTrue(estimator.run_estimate(101.0).finishing)

    def test_the_first_run_is_priced_from_the_latest_run_of_any_job(self) -> None:
        estimator = FrozenEstimator(past_run=FrozenPastRun(430.0, 40))
        estimator.observe(job_started(2, CooldownPolicy("off")), 0.0)
        estimator.observe(run_started(1, 2), 0.0)
        self.assertEqual(estimator.run_estimate(30.0).remaining, 400.0)
