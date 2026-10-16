"""The forecaster: a pure function of a job's events and the fitted model, checked on scenarios whose answer is known."""

from __future__ import annotations

import time
import unittest
from dataclasses import replace
from typing import Any

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun, HistoryStage
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.forecast import Forecast, Forecaster, JobPlan, PlannedRun
from framecontrol.jobs.timing.replay_stream import ReplayJob, replay_job
from tests.timing.builders import dt_run, h3_run
from tests.timing.synthetic import synthetic_history

ORIGIN = 1000.0
# A Draw Things run of 10 steps of 50 s: prepare 1, starting 7, processing 6, sampling 500, finishing 11, verify 2.
RUN_SECONDS, CHILD_SECONDS = 527.0, 524.0
SAMPLING_START = 14.0


def plan_of(execution: HistoryExecution, first_run: int = 1) -> JobPlan:
    return JobPlan(tuple(PlannedRun(run.run, run.backend, run.pipeline, dict(run.workload), dict(run.environment)) for run in execution.runs if run.run >= first_run), execution.cooldown)


class Playback:
    """Plays a replayed job into a forecaster up to a time and asks it what it forecasts there."""

    def __init__(self, history: list[HistoryRun], execution: HistoryExecution, *, first_run: int = 1, model: FittedModel | None = None) -> None:
        self.forecaster = Forecaster(model if model is not None else FittedModel(history), plan_of(execution, first_run))
        self.job: ReplayJob = replay_job(execution, first_run=first_run, origin=ORIGIN)
        self.next = 0

    def feed(self, until: float) -> None:
        events = self.job.events
        while self.next < len(events) and events[self.next][0] <= until + 1e-9:
            when, event = events[self.next]
            self.forecaster.observe(event, when)
            self.next += 1

    def at(self, seconds: float) -> Forecast | None:
        """The forecast ``seconds`` after the job started (every event up to then has been seen)."""
        self.feed(ORIGIN + seconds)
        return self.forecaster.forecast(ORIGIN + seconds)


def identical(execution: int, count: int = 1, **changes: Any) -> list[HistoryRun]:
    return [dt_run(execution, number + 1, **changes) for number in range(count)]


HISTORY = [dt_run(execution, 1) for execution in range(1, 5)]


def single_run(**changes: Any) -> HistoryExecution:
    return HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1, **changes),))


class RunForecastTests(unittest.TestCase):
    def test_at_the_runs_start_a_run_with_history_is_forecast_to_end_when_its_siblings_did(self) -> None:
        forecast = Playback(HISTORY, single_run()).at(0.0)
        assert forecast is not None and forecast.run is not None
        self.assertAlmostEqual(forecast.run.end_at - ORIGIN, RUN_SECONDS, delta=0.015 * RUN_SECONDS)
        self.assertEqual(forecast.run.started_at, ORIGIN)
        self.assertLess(forecast.run.low, forecast.run.end_at)
        self.assertGreater(forecast.run.high, forecast.run.end_at)
        self.assertGreaterEqual(forecast.run.low, ORIGIN)
        self.assertEqual((forecast.stage, forecast.stage_done, forecast.stage_total), ("prepare", None, None))
        self.assertEqual(forecast.as_of, ORIGIN)

    def test_its_basis_says_which_history_it_rests_on(self) -> None:
        forecast = Playback(HISTORY, single_run()).at(0.0)
        assert forecast is not None
        basis = forecast.basis
        self.assertEqual((basis.tier, basis.fidelity, basis.runs, basis.executions, basis.build_changed, basis.unseen), ("signature", "live", 4, 4, False, ()))

    def test_the_stage_and_its_counter_are_the_active_stages(self) -> None:
        forecast = Playback(HISTORY, single_run()).at(SAMPLING_START + 3 * 50.0)
        assert forecast is not None
        self.assertEqual((forecast.stage, forecast.stage_done, forecast.stage_total), ("sampling", 3, 10))

    def test_the_run_gets_no_nearer_to_the_end_than_the_work_that_is_left(self) -> None:
        playback = Playback(HISTORY, single_run())
        for seconds in (0.0, 30.0, 200.0, 400.0, 520.0):
            forecast = playback.at(seconds)
            assert forecast is not None and forecast.run is not None
            self.assertAlmostEqual(forecast.run.end_at - ORIGIN, RUN_SECONDS, delta=0.02 * RUN_SECONDS, msg=f"at {seconds}")
            self.assertGreaterEqual(forecast.run.end_at, ORIGIN + seconds)

    def test_a_draw_things_last_step_line_is_not_the_end_of_the_run(self) -> None:
        # `Sampling... 10 / 10` is printed as the last step begins: nine steps are done.
        forecast = Playback(HISTORY, single_run()).at(SAMPLING_START + 9 * 50.0)
        assert forecast is not None and forecast.run is not None
        self.assertEqual((forecast.stage, forecast.stage_done), ("sampling", 9))
        left = forecast.run.end_at - (ORIGIN + SAMPLING_START + 9 * 50.0)
        self.assertAlmostEqual(left, 50.0 + 11.0 + 2.0, delta=6.0)

    def test_an_early_h3_counter_that_is_already_complete_is_not_the_end_of_the_run(self) -> None:
        history = [h3_run(execution, 1) for execution in range(1, 5)]
        execution = HistoryExecution(9, CooldownPolicy("off"), (h3_run(9, 1),))
        playback = Playback(history, execution)
        total = h3_run(9, 1).seconds
        # `tokenizer 1/1` and `text encoder 50/50` are complete long before the run is.
        for seconds in (1.3, 6.5):
            forecast = playback.at(seconds)
            assert forecast is not None and forecast.run is not None
            self.assertAlmostEqual(forecast.run.end_at - ORIGIN, total, delta=0.02 * total, msg=f"at {seconds}")

    def test_the_pace_of_the_run_moves_the_forecast_toward_what_it_is_doing(self) -> None:
        slow = single_run(step=60.0)
        playback = Playback(HISTORY, slow)
        forecast = playback.at(SAMPLING_START + 5 * 60.0)
        assert forecast is not None and forecast.run is not None
        truth = ORIGIN + slow.runs[0].seconds
        self.assertAlmostEqual(forecast.run.end_at, truth, delta=0.03 * (truth - ORIGIN - SAMPLING_START - 300.0) + 5.0)
        self.assertLess(forecast.run.low, truth)
        self.assertGreater(forecast.run.high, truth - 5.0)

    def test_a_pause_counts_toward_elapsed_time_and_not_toward_the_rate(self) -> None:
        gaps = [50.0, 50.0, 900.0] + [50.0] * 7
        paused = single_run(gaps=gaps)
        forecast = Playback(HISTORY, paused).at(SAMPLING_START + 1000.0 + 50.0)
        assert forecast is not None and forecast.run is not None
        now = ORIGIN + SAMPLING_START + 1050.0
        # Four steps are done and six remain: the 900 s step did not slow the rate.
        self.assertAlmostEqual(forecast.run.end_at - now, 6 * 50.0 + 11.0 + 2.0, delta=0.04 * 313.0)

    def test_a_first_gap_with_no_known_ratio_does_not_move_the_rate(self) -> None:
        # Rows rebuilt from a log have an estimated first mark, so no ratio of the first gap to the rest can be known.
        history = [replace(run, stages=tuple(replace(stage, uncertain=True) if stage.unit == "steps" else stage for stage in run.stages)) for run in HISTORY]
        short = single_run(gaps=[5.0] + [50.0] * 9)
        playback = Playback(history, short)
        self.assertIsNone(playback.forecaster._plans[1].first_ratio)  # noqa: SLF001
        forecast = playback.at(SAMPLING_START + 5.0 + 2 * 50.0)
        assert forecast is not None and forecast.run is not None
        now = ORIGIN + SAMPLING_START + 105.0
        # Three steps are done and seven remain.
        self.assertAlmostEqual(forecast.run.end_at - now, 7 * 50.0 + 13.0, delta=0.03 * 363.0)

    def test_a_first_gap_that_the_history_says_is_typical_is_read_as_a_step(self) -> None:
        short = single_run(gaps=[5.0] + [50.0] * 9)
        playback = Playback(HISTORY, short)
        self.assertEqual(playback.forecaster._plans[1].first_ratio, 1.0)  # noqa: SLF001
        forecast = playback.at(SAMPLING_START + 5.0 + 2 * 50.0)
        assert forecast is not None and forecast.run is not None
        # One step in three was a tenth of the others, and the run is forecast as faster for it.
        self.assertLess(forecast.run.end_at - (ORIGIN + SAMPLING_START + 105.0), 7 * 50.0)

    def test_a_step_that_outlasts_its_prediction_never_leaves_the_end_in_the_past_and_widens_the_range(self) -> None:
        playback = Playback(HISTORY, single_run())
        base = SAMPLING_START + 5 * 50.0
        playback.feed(ORIGIN + base)
        # Nothing more is heard: the step goes on, and the forecaster is asked at later and later times.
        early, late, later = (playback.forecaster.forecast(ORIGIN + base + seconds) for seconds in (10.0, 200.0, 800.0))
        assert early and late and later and early.run and late.run and later.run
        self.assertGreater(late.run.end_at, ORIGIN + base + 200.0)
        self.assertGreater(later.run.end_at, ORIGIN + base + 800.0)
        self.assertGreater(later.run.high - later.run.low, late.run.high - late.run.low)
        self.assertGreater(late.run.high - late.run.low, early.run.high - early.run.low)

    def test_an_unknown_phase_widens_the_range_and_is_named_in_the_basis(self) -> None:
        history = [h3_run(execution, 1) for execution in range(1, 5)]
        base = h3_run(9, 1)
        stages = list(base.stages)
        index = next(i for i, stage in enumerate(stages) if stage.stage == "text_encode")
        mystery = HistoryStage("other", "child", stages[index].start, stages[index].start + 10.0, 3, 3)
        shifted = [replace(stage, start=stage.start + 10.0, end=stage.end + 10.0, marks=tuple((offset + 10.0, done) for offset, done in stage.marks)) if i >= index else stage for i, stage in enumerate(stages)]
        odd = replace(base, stages=(*shifted[:index], mystery, *shifted[index:]))
        known = Playback(history, HistoryExecution(9, CooldownPolicy("off"), (base,)))
        unknown = Playback(history, HistoryExecution(9, CooldownPolicy("off"), (odd,)))
        before = known.at(30.0)
        during = unknown.at(30.0)
        assert before and during and before.run and during.run
        self.assertEqual(during.basis.unseen, ("other",))
        self.assertEqual(before.basis.unseen, ())
        self.assertGreater(during.run.high - during.run.low, before.run.high - before.run.low)

    def test_without_history_there_is_no_number_until_the_steps_have_two_readings(self) -> None:
        playback = Playback([], single_run())
        start, one = playback.at(0.0), playback.at(SAMPLING_START + 1.0)
        assert start is not None and one is not None
        self.assertEqual((start.run, start.job, one.run, one.job), (None, None, None, None))
        two = playback.at(SAMPLING_START + 50.0)
        assert two is not None
        self.assertIsNotNone(two.run)
        forecast = playback.at(SAMPLING_START + 2 * 50.0)
        assert forecast is not None and forecast.run is not None
        self.assertEqual(forecast.basis.tier, "live_only")
        now = ORIGIN + SAMPLING_START + 100.0
        self.assertAlmostEqual(forecast.run.end_at - now, 8 * 50.0, delta=0.05 * 400.0)
        self.assertGreater(forecast.run.high - forecast.run.low, 0.0)

    def test_the_other_backends_history_does_not_price_a_run(self) -> None:
        history = [h3_run(execution, 1) for execution in range(1, 6)]
        forecast = Playback(history, single_run()).at(0.0)
        assert forecast is not None
        self.assertIsNone(forecast.run)
        self.assertEqual(forecast.basis.tier, "live_only")


class JobForecastTests(unittest.TestCase):
    def chain(self, policy: CooldownPolicy, count: int = 3) -> HistoryExecution:
        return HistoryExecution(9, policy, tuple(dt_run(9, number + 1) for number in range(count)))

    def test_the_job_is_the_run_the_waits_and_the_runs_still_to_come(self) -> None:
        policy = CooldownPolicy("manual", seconds=100.0)
        forecast = Playback(HISTORY, self.chain(policy)).at(0.0)
        assert forecast is not None and forecast.job is not None and forecast.run is not None
        self.assertAlmostEqual(forecast.job.end_at - ORIGIN, 3 * RUN_SECONDS + 2 * 100.0, delta=0.015 * 1781.0)
        self.assertEqual(forecast.job.started_at, ORIGIN)

    def test_a_wait_is_the_policy_applied_to_the_runs_child_time(self) -> None:
        policy = CooldownPolicy("auto", ratio=0.5)
        wait = policy.wait_after(CHILD_SECONDS).seconds
        forecast = Playback(HISTORY, self.chain(policy)).at(0.0)
        assert forecast is not None and forecast.job is not None
        self.assertAlmostEqual(forecast.job.end_at - ORIGIN, 3 * RUN_SECONDS + 2 * wait, delta=0.015 * (3 * RUN_SECONDS + 2 * wait))

    def test_a_wait_in_auto_mode_widens_the_job_range_more_than_a_fixed_one(self) -> None:
        auto = Playback(HISTORY, self.chain(CooldownPolicy("auto", ratio=0.5))).at(0.0)
        fixed = Playback(HISTORY, self.chain(CooldownPolicy("manual", seconds=262.0))).at(0.0)
        assert auto and fixed and auto.job and fixed.job
        self.assertGreater(auto.job.high - auto.job.low, fixed.job.high - fixed.job.low)

    def test_during_a_cooldown_there_is_no_stage_and_no_run_and_the_job_counts_what_is_left(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("manual", seconds=100.0)))
        forecast = playback.at(RUN_SECONDS + 20.0)
        assert forecast is not None and forecast.job is not None
        self.assertEqual((forecast.stage, forecast.stage_done, forecast.stage_total, forecast.run), (None, None, None, None))
        cooldown_until = ORIGIN + RUN_SECONDS + 100.0
        # Runs 2 and 3, and the wait between them.
        self.assertAlmostEqual(forecast.job.end_at, cooldown_until + 2 * RUN_SECONDS + 100.0, delta=0.015 * 2 * RUN_SECONDS)
        self.assertGreater(forecast.job.end_at, cooldown_until)

    def test_a_slow_run_makes_the_runs_after_it_forecast_slower_by_the_share_an_execution_has_in_common(self) -> None:
        def left_after_the_wait(step: float) -> float:
            chain = HistoryExecution(9, CooldownPolicy("manual", seconds=100.0), tuple(dt_run(9, number + 1, step=step if number == 0 else 50.0) for number in range(3)))
            playback = Playback(HISTORY, chain)
            run_one = chain.runs[0].seconds
            forecast = playback.at(run_one + 20.0)
            assert forecast is not None and forecast.job is not None
            return forecast.job.end_at - (ORIGIN + run_one + 100.0)

        normal, slow = left_after_the_wait(50.0), left_after_the_wait(57.5)
        # Runs 2 and 3 are still 50 s a step; the first run's 15% is carried into them, in part.
        self.assertGreater(slow, normal * 1.03)
        self.assertLess(slow, normal * 1.15)

    def test_the_remaining_wait_of_a_cooldown_is_exact(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("manual", seconds=100.0), count=2))
        forecast = playback.at(RUN_SECONDS + 20.0)
        before = playback.at(RUN_SECONDS + 90.0)
        assert forecast and before and forecast.job and before.job
        # Ten seconds from the end of the wait: the job is the next run's forecast, and its range is that run's alone.
        self.assertAlmostEqual(before.job.end_at - (ORIGIN + RUN_SECONDS + 100.0), forecast.job.end_at - (ORIGIN + RUN_SECONDS + 100.0), delta=1.0)

    def test_the_last_run_of_a_job_has_no_wait_after_it(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("manual", seconds=100.0), count=2))
        forecast = playback.at(RUN_SECONDS + 100.0 + 10.0)
        assert forecast is not None and forecast.job is not None and forecast.run is not None
        self.assertAlmostEqual(forecast.job.end_at, forecast.run.end_at, delta=1e-6)

    def test_with_a_park_reserved_the_job_ends_with_the_current_run(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("manual", seconds=100.0)))
        playback.feed(ORIGIN + 100.0)
        playback.forecaster.set_parking(True)
        forecast = playback.forecaster.forecast(ORIGIN + 100.0)
        assert forecast is not None and forecast.job is not None and forecast.run is not None
        self.assertEqual(forecast.job, forecast.run)
        playback.forecaster.set_parking(False)
        again = playback.forecaster.forecast(ORIGIN + 100.0)
        assert again is not None and again.job is not None and again.run is not None
        self.assertGreater(again.job.end_at, again.run.end_at)

    def test_a_park_reserved_in_a_cooldown_ends_the_job_so_it_has_no_forecast_of_its_end(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("manual", seconds=100.0)))
        playback.feed(ORIGIN + RUN_SECONDS + 20.0)
        playback.forecaster.set_parking(True)
        forecast = playback.forecaster.forecast(ORIGIN + RUN_SECONDS + 20.0)
        assert forecast is not None
        self.assertIsNone(forecast.job)
        self.assertIsNone(forecast.run)

    def test_a_cancel_clears_the_forecast(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("off")))
        self.assertIsNotNone(playback.at(10.0))
        playback.forecaster.cancel()
        self.assertIsNone(playback.forecaster.forecast(ORIGIN + 11.0))

    def test_when_the_job_finishes_there_is_no_forecast(self) -> None:
        playback = Playback(HISTORY, self.chain(CooldownPolicy("off"), count=1))
        playback.feed(ORIGIN + RUN_SECONDS + 1.0)
        self.assertIsNone(playback.forecaster.forecast(ORIGIN + RUN_SECONDS + 1.0))

    def test_a_resumed_entry_forecasts_only_the_runs_it_will_execute(self) -> None:
        execution = self.chain(CooldownPolicy("manual", seconds=100.0), count=4)
        forecast = Playback(HISTORY, execution, first_run=3).at(0.0)
        assert forecast is not None and forecast.job is not None
        self.assertAlmostEqual(forecast.job.end_at - ORIGIN, 2 * RUN_SECONDS + 100.0, delta=0.015 * 1154.0)

    def test_a_later_run_with_no_history_leaves_the_job_unpriced_until_a_run_of_its_kind_has_finished(self) -> None:
        playback = Playback([], self.chain(CooldownPolicy("manual", seconds=100.0), count=3))
        during = playback.at(SAMPLING_START + 2 * 50.0)
        assert during is not None and during.run is not None
        self.assertIsNone(during.job)
        between = playback.at(RUN_SECONDS + 20.0)
        assert between is not None and between.job is not None
        self.assertAlmostEqual(between.job.end_at, ORIGIN + RUN_SECONDS + 100.0 + 2 * RUN_SECONDS + 100.0, delta=0.05 * 2 * RUN_SECONDS)

    def test_a_run_of_another_backend_is_priced_from_its_own_history_and_not_from_the_one_before(self) -> None:
        history = [dt_run(execution, 1) for execution in range(1, 5)] + [h3_run(execution, 1) for execution in range(5, 9)]
        execution = HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1, step=70.0), h3_run(9, 2)))
        forecast = Playback(history, execution).at(0.0)
        assert forecast is not None and forecast.job is not None
        h3_seconds = h3_run(9, 2).seconds
        self.assertAlmostEqual(forecast.job.end_at - ORIGIN, RUN_SECONDS + h3_seconds, delta=0.03 * (RUN_SECONDS + h3_seconds))


class SpeedTests(unittest.TestCase):
    def test_a_forecast_update_takes_at_most_5_milliseconds_an_event(self) -> None:
        runs = synthetic_history().runs
        target = next(run for run in runs if run.backend == "h3" and run.pipeline == "r2av" and run.execution > 300)
        history = [run for run in runs if run.execution <= 300]
        execution = HistoryExecution(target.execution, CooldownPolicy("off"), (replace(target, run=1),))
        model, job = FittedModel(history), replay_job(execution, origin=ORIGIN)
        best = float("inf")
        for _ in range(3):
            forecaster = Forecaster(model, plan_of(execution))
            slowest = 0.0
            for when, event in job.events:
                started = time.process_time()
                forecaster.observe(event, when)
                forecaster.forecast(when)
                slowest = max(slowest, time.process_time() - started)
            best = min(best, slowest)
        self.assertLess(best, 0.005)
