"""The events a stored run would have produced, in the order and at the times the executor emits them."""

from __future__ import annotations

import unittest

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.jobs.events import CooldownEnded, CooldownStarted, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted, StageFinished, StageStarted, TimingFinished, TimingStarted
from framecontrol.jobs.timing.features import HistoryExecution
from framecontrol.jobs.timing.replay_stream import replay_job
from tests.timing.builders import dt_run, dt_workload, h3_run


def execution(cooldown: CooldownPolicy, *runs) -> HistoryExecution:
    return HistoryExecution(7, cooldown, tuple(runs))


class RunStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stored = dt_run(7, 1, step=50.0, workload=dt_workload(steps=4))
        self.job = replay_job(execution(CooldownPolicy("off"), self.stored), origin=1000.0)

    def kinds(self) -> list[type]:
        return [type(event) for _, event in self.job.events]

    def test_a_job_starts_with_jobstarted_and_ends_with_jobfinished(self) -> None:
        self.assertIs(self.kinds()[0], JobStarted)
        self.assertIs(self.kinds()[-1], JobFinished)
        started = self.job.events[0][1]
        assert isinstance(started, JobStarted)
        self.assertEqual((started.total_runs, started.first_run, started.cooldown), (1, 1, CooldownPolicy("off")))
        self.assertEqual(started.at, "1970-01-01T00:16:40.000+00:00")

    def test_a_run_has_the_events_the_executor_emits_in_its_order(self) -> None:
        kinds = self.kinds()
        self.assertEqual(kinds[1:4], [RunStarted, TimingStarted, StageStarted])
        self.assertEqual(kinds[-4:], [StageFinished, TimingFinished, RunFinished, JobFinished])
        started = next(event for _, event in self.job.events if isinstance(event, TimingStarted))
        self.assertEqual((started.backend, started.pipeline, started.workload["steps"]), ("draw-things-cli", "i2v", 4))

    def test_the_times_are_the_runs_own_offsets_from_its_start(self) -> None:
        times = {event.stage: time for time, event in self.job.events if isinstance(event, StageStarted)}
        for stage in self.stored.counted_stages:
            self.assertAlmostEqual(times[stage.stage], 1000.0 + stage.start, places=6)
        self.assertEqual((self.job.started, self.job.starts[1]), (1000.0, 1000.0))
        self.assertAlmostEqual(self.job.ends[1], 1000.0 + self.stored.seconds, places=6)
        self.assertAlmostEqual(self.job.finished, self.job.ends[1], places=6)

    def test_every_mark_of_the_steps_is_a_reading_with_its_counter(self) -> None:
        readings = [(time, event) for time, event in self.job.events if isinstance(event, RunOutput) and event.stage == "sampling"]
        self.assertEqual([event.done for _, event in readings], [0, 1, 2, 3, 4])
        self.assertTrue(all(event.total == 4 for _, event in readings))
        sampling = next(stage for stage in self.stored.counted_stages if stage.stage == "sampling")
        self.assertEqual([round(time - 1000.0, 6) for time, _ in readings], [offset for offset, _ in sampling.marks])
        self.assertTrue(all(abs(time - 1000.0 - event.offset) < 1e-6 for time, event in readings))

    def test_a_stage_with_a_counter_and_no_marks_is_read_at_its_start_and_its_end(self) -> None:
        job = replay_job(execution(CooldownPolicy("off"), h3_run(7, 1)), origin=0.0)
        text = [(time, event.done, event.total) for time, event in job.events if isinstance(event, RunOutput) and event.stage == "text_encode"]
        self.assertEqual([(done, total) for _, done, total in text], [(0, 50), (50, 50)])
        stage = next(stage for stage in h3_run(7, 1).counted_stages if stage.stage == "text_encode")
        self.assertAlmostEqual(text[0][0], stage.start, places=6)
        self.assertAlmostEqual(text[1][0], stage.end, places=6)

    def test_a_stage_finishes_before_the_next_one_starts_at_the_same_time(self) -> None:
        order = [(type(event).__name__, getattr(event, "stage", None)) for time, event in self.job.events if abs(time - (1000.0 + 1.0)) < 1e-9]
        self.assertEqual(order[:2], [("StageFinished", "prepare"), ("StageStarted", "starting")])


class ChainStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runs = [dt_run(7, 1, step=10.0, workload=dt_workload(steps=3)), dt_run(7, 2, step=10.0, workload=dt_workload(steps=3)), dt_run(7, 3, step=10.0, workload=dt_workload(steps=3))]

    def test_runs_are_spaced_by_the_cooldown_the_policy_gives_for_the_child_time(self) -> None:
        policy = CooldownPolicy("auto", ratio=0.5)
        job = replay_job(execution(policy, *self.runs), origin=0.0)
        wait = policy.wait_after(self.runs[0].child_seconds).seconds
        self.assertAlmostEqual(job.starts[2], job.ends[1] + wait, places=6)
        self.assertAlmostEqual(job.starts[3], job.ends[2] + wait, places=6)
        started = [event for _, event in job.events if isinstance(event, CooldownStarted)]
        self.assertEqual([(event.after_run, event.seconds) for event in started], [(1, wait), (2, wait)])
        self.assertEqual(len([event for _, event in job.events if isinstance(event, CooldownEnded)]), 2)

    def test_no_wait_follows_the_last_run_or_an_off_policy(self) -> None:
        job = replay_job(execution(CooldownPolicy("off"), *self.runs), origin=0.0)
        self.assertFalse([event for _, event in job.events if isinstance(event, CooldownStarted)])
        self.assertAlmostEqual(job.starts[2], job.ends[1], places=6)

    def test_recorded_waits_replace_the_policys_when_the_execution_has_them(self) -> None:
        recorded = HistoryExecution(7, CooldownPolicy("auto", ratio=0.5), tuple(self.runs), waits=(40.0, 0.0))
        job = replay_job(recorded, origin=0.0)
        self.assertAlmostEqual(job.starts[2], job.ends[1] + 40.0, places=6)
        self.assertAlmostEqual(job.starts[3], job.ends[2], places=6)
        started = [event for _, event in job.events if isinstance(event, CooldownStarted)]
        self.assertEqual([(event.after_run, event.seconds) for event in started], [(1, 40.0)])

    def test_a_resumed_job_replays_only_the_runs_it_will_execute(self) -> None:
        job = replay_job(execution(CooldownPolicy("off"), *self.runs), first_run=2, origin=0.0)
        self.assertEqual(sorted(job.starts), [2, 3])
        started = job.events[0][1]
        assert isinstance(started, JobStarted)
        self.assertEqual((started.first_run, started.total_runs), (2, 3))

    def test_events_are_in_time_order(self) -> None:
        job = replay_job(execution(CooldownPolicy("manual", seconds=30.0), *self.runs), origin=5.0)
        times = [time for time, _ in job.events]
        self.assertEqual(times, sorted(times))
