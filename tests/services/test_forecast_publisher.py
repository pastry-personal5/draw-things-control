"""The publisher: when a forecast goes out, the share it holds so a bar never moves back, and that it never breaks the worker."""

from __future__ import annotations

import math
import unittest
from collections import Counter
from typing import Any

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import JobEvent, JobFinished, RunOutput, StageStarted
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.forecast import Forecast, Forecaster, JobPlan, PlannedRun
from framecontrol.jobs.timing.knobs import Tunables
from framecontrol.jobs.timing.replay_stream import replay_job
from framecontrol.services.forecast_publisher import MIN_INTERVAL_SECONDS, ForecastPublisher
from framecontrol.services.forecast_view import progress
from tests.timing.builders import dt_run, h3_run

ORIGIN = 5000.0
HISTORY = [dt_run(execution, 1) for execution in range(1, 5)]


def plan_of(execution: HistoryExecution) -> JobPlan:
    return JobPlan(tuple(PlannedRun(run.run, run.backend, run.pipeline, dict(run.workload), dict(run.environment)) for run in execution.runs), execution.cooldown)


class Sink:
    def __init__(self) -> None:
        self.published: list[Forecast | None] = []

    def __call__(self, forecast: Forecast | None) -> None:
        self.published.append(forecast)

    @property
    def forecasts(self) -> list[Forecast]:
        return [item for item in self.published if item is not None]


def publisher_for(history: list[HistoryRun], execution: HistoryExecution) -> tuple[ForecastPublisher, Sink, list[tuple[float, JobEvent]]]:
    sink = Sink()
    publisher = ForecastPublisher(Forecaster(FittedModel(history), plan_of(execution)), sink, clock=lambda: 0.0)
    return publisher, sink, list(replay_job(execution, origin=ORIGIN).events)


def play(publisher: ForecastPublisher, events: list[tuple[float, JobEvent]], until: float = math.inf) -> None:
    for when, event in events:
        if when <= until:
            publisher.observe(event, when)


class PublishingRuleTests(unittest.TestCase):
    def test_a_forecast_goes_out_at_each_stage_run_and_cooldown_change(self) -> None:
        execution = HistoryExecution(9, CooldownPolicy("manual", seconds=50.0), (dt_run(9, 1), dt_run(9, 2)))
        publisher, sink, events = publisher_for(HISTORY, execution)
        play(publisher, events)
        stages = [forecast.stage for forecast in sink.forecasts]
        for name in ("prepare", "starting", "processing", "sampling", "finishing", "verify"):
            self.assertIn(name, stages)
        # The cooldown has no stage, and the second run starts over.
        self.assertIn(None, stages)
        self.assertEqual(stages.count("prepare"), 2)

    def test_within_a_stage_every_completed_step_shows_when_it_is_two_seconds_after_the_last(self) -> None:
        publisher, sink, events = publisher_for(HISTORY, HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        play(publisher, events)
        units = [forecast.stage_done for forecast in sink.forecasts if forecast.stage == "sampling"]
        # Ten steps of 50 s: the stage's start, then a forecast at each step's reading (done 0 to 9) and at the stage's end.
        self.assertEqual(units, [None, *range(11)])

    def test_a_243_frame_ffmpeg_counter_does_not_flood_the_backlog(self) -> None:
        # An H3 run whose FFmpeg stage prints a reading for each of 243 frames in 100 s.
        base = h3_run(9, 1)
        stages = []
        for stage in base.stages:
            if stage.stage == "ffmpeg":
                frames = tuple((round(stage.start + 100.0 * index / 243, 6), index) for index in range(244))
                stage = type(stage)(stage.stage, stage.source, stage.start, stage.end, 243, 243, "frames", frames)
            stages.append(stage)
        run = type(base)(base.execution, base.run, base.fidelity, base.backend, base.pipeline, base.workload, base.environment, tuple(stages))
        history = [h3_run(execution, 1) for execution in range(1, 5)]
        execution = HistoryExecution(9, CooldownPolicy("off"), (run,))
        publisher, sink, events = publisher_for(history, execution)
        play(publisher, events)
        per_stage = Counter(forecast.stage for forecast in sink.forecasts)
        self.assertLessEqual(per_stage["ffmpeg"], math.ceil(100.0 / MIN_INTERVAL_SECONDS) + 2)
        self.assertGreater(per_stage["ffmpeg"], 10)
        self.assertLess(len(sink.forecasts), 200)

    def test_the_last_reading_of_a_stage_is_always_published(self) -> None:
        base = h3_run(9, 1)
        stages = []
        for stage in base.stages:
            if stage.stage == "ffmpeg":
                frames = tuple((round(stage.start + 0.01 * index, 6), index) for index in range(244))
                stage = type(stage)(stage.stage, stage.source, stage.start, stage.end, 243, 243, "frames", frames)
            stages.append(stage)
        run = type(base)(base.execution, base.run, base.fidelity, base.backend, base.pipeline, base.workload, base.environment, tuple(stages))
        publisher, sink, events = publisher_for([h3_run(execution, 1) for execution in range(1, 5)], HistoryExecution(9, CooldownPolicy("off"), (run,)))
        play(publisher, events)
        self.assertEqual([forecast.stage_done for forecast in sink.forecasts if forecast.stage == "ffmpeg"][-1], 243)

    def test_an_event_that_changes_nothing_a_reader_sees_is_not_published(self) -> None:
        publisher, sink, events = publisher_for(HISTORY, HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        play(publisher, events, until=ORIGIN + 20.0)
        before = len(sink.published)
        stage = next(event for _, event in events if isinstance(event, StageStarted) and event.stage == "processing")
        publisher.observe(stage, ORIGIN + 20.0)
        self.assertEqual(len(sink.published), before + 1)
        # The same stage and the same ends again: nothing new to show.
        again = Forecast.from_dict(sink.forecasts[-1].as_dict())
        self.assertEqual(again.stage, "processing")
        reading = next(event for _, event in events if isinstance(event, RunOutput) and event.stage == "processing")
        publisher.observe(reading, ORIGIN + 20.0)
        self.assertEqual(len(sink.published), before + 1)

    def test_timing_started_for_the_run_that_was_described_adds_nothing_to_show(self) -> None:
        publisher, sink, events = publisher_for(HISTORY, HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        kinds = [type(event).__name__ for _, event in events[:4]]
        self.assertEqual(kinds, ["JobStarted", "RunStarted", "TimingStarted", "StageStarted"])
        play(publisher, events[:1])
        self.assertIsNotNone(sink.forecasts[0].run)
        self.assertIsNotNone(sink.forecasts[0].job)
        play(publisher, events[1:3])
        # JobStarted publishes the first rough forecast; RunStarted publishes its actual start.
        self.assertEqual(len(sink.published), 2)
        play(publisher, events[3:4])
        self.assertEqual([forecast.stage for forecast in sink.forecasts], [None, None, "prepare"])

    def test_a_line_of_output_that_is_not_a_reading_costs_no_forecast(self) -> None:
        from unittest import mock

        publisher, sink, events = publisher_for(HISTORY, HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        play(publisher, events, until=ORIGIN + 20.0)
        before = len(sink.published)
        chatter = RunOutput(at="", number=1, stream="stdout", text="Models directory: models", phase=None, offset=15.0)
        with mock.patch.object(Forecaster, "forecast", autospec=True) as forecast:
            for _ in range(50):
                publisher.observe(chatter, ORIGIN + 20.0)
        forecast.assert_not_called()
        self.assertEqual(len(sink.published), before)

    def test_a_park_reserved_or_withdrawn_is_published(self) -> None:
        execution = HistoryExecution(9, CooldownPolicy("manual", seconds=50.0), (dt_run(9, 1), dt_run(9, 2)))
        publisher, sink, events = publisher_for(HISTORY, execution)
        play(publisher, events, until=ORIGIN + 100.0)
        before = len(sink.published)
        publisher.park_changed(True, ORIGIN + 100.0)
        parked = sink.forecasts[-1]
        self.assertEqual(len(sink.published), before + 1)
        assert parked.run is not None and parked.job is not None
        self.assertEqual((parked.job.end_at, parked.job.low, parked.job.high), (parked.run.end_at, parked.run.low, parked.run.high))
        publisher.park_changed(False, ORIGIN + 100.0)
        released = sink.forecasts[-1]
        assert released.job is not None and released.run is not None
        self.assertGreater(released.job.end_at, released.run.end_at)


class FloorTests(unittest.TestCase):
    @staticmethod
    def run_share(forecast: Forecast) -> float:
        assert forecast.run is not None
        return progress(forecast.run.as_dict(), forecast.as_of)

    @staticmethod
    def job_share(forecast: Forecast) -> float:
        assert forecast.job is not None
        return progress(forecast.job.as_dict(), forecast.as_of)

    def test_the_share_a_client_shows_never_moves_back_within_a_run_or_a_job(self) -> None:
        # Run 1 is a third slower than its history said, so the forecast grows as it goes.
        execution = HistoryExecution(9, CooldownPolicy("manual", seconds=50.0), (dt_run(9, 1, step=70.0), dt_run(9, 2), dt_run(9, 3)))
        publisher, sink, events = publisher_for(HISTORY, execution)
        play(publisher, events)
        jobs = [self.job_share(forecast) for forecast in sink.forecasts if forecast.job]
        self.assertTrue(jobs)
        self.assertEqual(jobs, sorted(jobs))
        by_run: dict[float, list[float]] = {}
        for forecast in sink.forecasts:
            if forecast.run:
                by_run.setdefault(forecast.run.started_at, []).append(self.run_share(forecast))
        for shares in by_run.values():
            self.assertEqual(shares, sorted(shares))

    def test_the_old_forecast_at_the_moment_of_a_new_one_would_have_shown_no_more_than_the_new_floor(self) -> None:
        execution = HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1, step=70.0),))
        publisher, sink, events = publisher_for(HISTORY, execution)
        play(publisher, events)
        forecasts = sink.forecasts
        for earlier, later in zip(forecasts, forecasts[1:], strict=False):
            if earlier.run and later.run and earlier.run.started_at == later.run.started_at:
                self.assertGreaterEqual(progress(later.run.as_dict(), later.as_of), progress(earlier.run.as_dict(), later.as_of) - 1e-9)

    def test_a_client_that_attaches_mid_run_draws_the_same_bar_with_no_history(self) -> None:
        execution = HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1, step=70.0),))
        publisher, sink, events = publisher_for(HISTORY, execution)
        play(publisher, events)
        last = sink.forecasts[len(sink.forecasts) // 2]
        assert last.run is not None
        # The floor is in the published span, so the bar needs only the span and the client's clock.
        self.assertEqual(progress(last.run.as_dict(), last.as_of), progress(Forecast.from_dict(last.as_dict()).run.as_dict(), last.as_of))  # type: ignore[union-attr]
        self.assertGreater(last.run.floor, 0.0)

    def test_the_run_floor_starts_over_with_the_next_run_and_the_jobs_does_not(self) -> None:
        execution = HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1), dt_run(9, 2)))
        publisher, sink, events = publisher_for(HISTORY, execution)
        play(publisher, events)
        second = [forecast for forecast in sink.forecasts if forecast.run and forecast.run.started_at > ORIGIN + 100.0]
        assert second[0].run is not None and second[0].job is not None
        self.assertLess(second[0].run.floor, 0.05)
        self.assertGreater(second[0].job.floor, 0.4)

    def test_the_jobs_bar_does_not_fall_when_a_park_is_reserved_or_withdrawn(self) -> None:
        execution = HistoryExecution(9, CooldownPolicy("manual", seconds=50.0), (dt_run(9, 1), dt_run(9, 2), dt_run(9, 3)))
        publisher, sink, events = publisher_for(HISTORY, execution)
        # Early in run 2: the run is a seventh done and the job two fifths.
        play(publisher, events, until=ORIGIN + 650.0)
        before = self.job_share(sink.forecasts[-1])
        publisher.park_changed(True, ORIGIN + 650.0)
        parked = self.job_share(sink.forecasts[-1])
        publisher.park_changed(False, ORIGIN + 650.0)
        after = self.job_share(sink.forecasts[-1])
        # Under a park the job is the run, which is far behind the job; the bar holds where it was.
        self.assertGreater(before, 0.3)
        self.assertGreaterEqual(parked, before)
        self.assertGreaterEqual(after, parked)


class SafetyTests(unittest.TestCase):
    def test_a_forecaster_that_raises_never_stops_the_worker_and_leaves_no_forecast(self) -> None:
        class Broken(Forecaster):
            def forecast(self, now: float) -> Forecast | None:
                raise RuntimeError("model bug")

        sink = Sink()
        publisher = ForecastPublisher(Broken(FittedModel(HISTORY), plan_of(HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))), sink, clock=lambda: 0.0)
        events = list(replay_job(HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)), origin=ORIGIN).events)
        play(publisher, events)
        self.assertIsNone(publisher.latest)
        self.assertEqual(sink.forecasts, [])

    def test_a_cancel_clears_the_forecast_and_says_so(self) -> None:
        publisher, sink, events = publisher_for(HISTORY, HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        play(publisher, events, until=ORIGIN + 100.0)
        self.assertIsNotNone(publisher.latest)
        publisher.cancel()
        self.assertIsNone(publisher.latest)
        self.assertIsNone(sink.published[-1])
        publisher.observe(events[-2][1], ORIGIN + 200.0)
        self.assertIsNone(publisher.latest)

    def test_the_end_of_the_job_clears_it(self) -> None:
        publisher, sink, events = publisher_for(HISTORY, HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        play(publisher, events)
        self.assertIsInstance(events[-1][1], JobFinished)
        self.assertIsNone(publisher.latest)
        self.assertIsNone(sink.published[-1])

    def test_a_run_with_no_history_publishes_a_broad_number_at_start(self) -> None:
        publisher, sink, events = publisher_for([], HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),)))
        play(publisher, events, until=ORIGIN + 5.0)
        self.assertIsNotNone(publisher.latest)
        assert publisher.latest is not None
        self.assertIsNotNone(publisher.latest.run)
        self.assertIsNotNone(publisher.latest.job)
        self.assertEqual(publisher.latest.basis.tier, "heuristic")

    def test_without_an_explicit_time_the_clock_is_used(self) -> None:
        sink = Sink()
        times: list[Any] = [ORIGIN]
        execution = HistoryExecution(9, CooldownPolicy("off"), (dt_run(9, 1),))
        publisher = ForecastPublisher(Forecaster(FittedModel(HISTORY), plan_of(execution)), sink, clock=lambda: times[0])
        for _, event in replay_job(execution, origin=ORIGIN).events[:3]:
            publisher.observe(event)
        self.assertEqual(sink.forecasts[-1].as_of, ORIGIN)


class PublishingTunablesTests(unittest.TestCase):
    """The interval and the same-end tolerance are the model's tunables, so an owner's edit changes the publishing."""

    def count(self, tunables: Tunables) -> int:
        base = h3_run(9, 1)
        stages = []
        for stage in base.stages:
            if stage.stage == "ffmpeg":
                frames = tuple((round(stage.start + 100.0 * index / 243, 6), index) for index in range(244))
                stage = type(stage)(stage.stage, stage.source, stage.start, stage.end, 243, 243, "frames", frames)
            stages.append(stage)
        run = type(base)(base.execution, base.run, base.fidelity, base.backend, base.pipeline, base.workload, base.environment, tuple(stages))
        execution = HistoryExecution(9, CooldownPolicy("off"), (run,))
        sink = Sink()
        model = FittedModel([h3_run(number, 1) for number in range(1, 5)], tunables=tunables)
        publisher = ForecastPublisher(Forecaster(model, plan_of(execution)), sink, clock=lambda: 0.0)
        play(publisher, list(replay_job(execution, origin=ORIGIN).events))
        return Counter(forecast.stage for forecast in sink.forecasts)["ffmpeg"]

    def test_a_longer_interval_publishes_fewer_readings(self) -> None:
        self.assertLess(self.count(Tunables(publish_interval_seconds=10.0)), self.count(Tunables()))

    def test_the_default_is_the_two_seconds_it_always_was(self) -> None:
        self.assertEqual(MIN_INTERVAL_SECONDS, 2.0)
        self.assertEqual(Tunables().publish_interval_seconds, 2.0)
