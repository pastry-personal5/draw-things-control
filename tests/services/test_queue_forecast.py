"""The queue worker's forecast: built when the job is parsed, fed every job event, published as a queue event, and cleared
when the entry is released, a cancel is accepted, or the job ends."""

from __future__ import annotations

import threading
from typing import Any
from unittest import mock

from framecontrol.jobs.parsing import load_job
from framecontrol.services.forecast_publisher import job_plan
from framecontrol.services.generation_submit import GenerationSnapshot
from framecontrol.services.queue_park import park_entry, unpark_entry
from framecontrol.state.executions import ExecutionSettings
from framecontrol.state.queue import NewQueueEntry
from tests.fixtures import job_data
from tests.jobs.test_executor import BlockingRunner
from tests.services.test_queue_worker import QueueWorkerCase


class ForecastCase(QueueWorkerCase):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.worker = self.build_worker(on_event=self.record)
        self.during: Any = None

    def record(self, kind: str, data: dict[str, Any]) -> None:
        self.events.append((kind, data))
        if self.during is not None:
            self.during(kind, data)

    def forecasts(self) -> list[dict[str, Any]]:
        return [data for kind, data in self.events if kind == "forecast_updated"]


class QueueForecastTests(ForecastCase):
    def test_a_job_publishes_its_forecast_as_it_runs_and_an_empty_one_when_it_ends(self) -> None:
        label = self.submit(run_count=2)
        self.assertTrue(self.worker.claim_and_run_one())
        published = self.forecasts()
        self.assertGreater(len(published), 3)
        self.assertTrue(all(data["queue_id"] == label for data in published))
        self.assertIsNone(published[-1]["forecast"])
        stages = [data["forecast"]["stage"] for data in published[:-1]]
        self.assertIn("prepare", stages)
        # No history yet: the basis says so, and no number is invented.
        first = published[0]["forecast"]
        self.assertEqual((first["basis"]["tier"], first["run"], first["job"]), ("live_only", None, None))

    def test_the_worker_holds_the_latest_forecast_of_the_entry_it_runs_and_nothing_after(self) -> None:
        label = self.submit(run_count=1)
        entry_id = self.entry(label).id
        held: list[tuple[dict[str, Any] | None, Any]] = []

        def note(kind: str, data: dict[str, Any]) -> None:
            if kind == "forecast_updated":
                latest = self.worker.latest_forecast(entry_id)
                held.append((data["forecast"], latest.as_dict() if latest is not None else None))

        self.during = note
        self.worker.claim_and_run_one()
        self.assertTrue(held)
        # The event and the held forecast are one and the same at the moment it is published.
        self.assertTrue(all(sent == kept for sent, kept in held))
        self.assertIsNone(self.worker.latest_forecast(entry_id))
        self.assertIsNone(self.worker.latest_forecast(entry_id + 1))

    def test_a_one_off_generation_has_no_forecast(self) -> None:
        self.output_directory.mkdir()
        snapshot = GenerationSnapshot(model="model.ckpt", output="cube.png", output_path=str(self.output_directory / "cube.png"), timeout=60, prompt="cube")
        self.store.queue.submit(NewQueueEntry(job_path="", job_text="", config_file="", config_text="", input_directory=str(self.input_directory), output_directory=str(self.output_directory), cooldown_default=None, settings=ExecutionSettings(output_directory=str(self.output_directory)), submitted_at="2026-09-27T15:30:12+00:00", total_runs=1, kind="generate", snapshot=snapshot.to_json()))
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.forecasts(), [])

    def test_a_job_whose_runs_cannot_be_described_still_runs_with_no_forecast(self) -> None:
        label = self.submit(run_count=2)
        with mock.patch.object(self.executor, "describe_run", side_effect=ValueError("no description")):
            self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertEqual(self.forecasts(), [])

    def test_an_entry_that_dies_before_its_job_finishes_leaves_no_forecast_behind(self) -> None:
        from framecontrol.core.cooldown import CooldownPolicy
        from framecontrol.jobs.events import combine_observers
        from framecontrol.services.job_runs import JobRunSession
        from tests.timing.test_frozen_estimator import job_started, run_started

        label = self.submit(run_count=1)
        entry_id = self.entry(label).id

        def dies(_session: Any, job: Any, **options: Any) -> None:
            observe = combine_observers(*options["observers"])
            observe(job_started(1, CooldownPolicy("off")))
            observe(run_started(1, 1))
            raise RuntimeError("the job died with no JobFinished")

        with mock.patch.object(JobRunSession, "run", dies):
            self.worker.claim_and_run_one()
        published = self.forecasts()
        self.assertIsNotNone(published[0]["forecast"])
        self.assertIsNone(published[-1]["forecast"])
        self.assertIsNone(self.worker.latest_forecast(entry_id))

    def test_a_model_that_fails_on_an_event_never_stops_the_job(self) -> None:
        label = self.submit(run_count=2)
        with mock.patch("framecontrol.jobs.timing.forecast.Forecaster.forecast", side_effect=RuntimeError("model bug")):
            self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertEqual([data["forecast"] for data in self.forecasts() if data["forecast"] is not None], [])

    def test_a_history_that_cannot_be_read_gives_a_forecast_of_no_history(self) -> None:
        label = self.submit(run_count=1)
        with mock.patch.object(type(self.store.timing), "complete_runs", side_effect=OSError("disk")):
            self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertEqual(self.forecasts()[0]["forecast"]["basis"]["tier"], "live_only")


class CancelAndParkTests(ForecastCase):
    def stage_of(self, entry_id: int) -> str | None:
        latest = self.worker.latest_forecast(entry_id)
        return latest.stage if latest is not None else None

    def run_blocked(self, action: Any) -> str:
        """Run a two-run entry whose first run blocks until it is stopped, calling ``action`` while it does."""
        label = self.submit(run_count=2)
        entry_id = self.entry(label).id
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        outcome: list[bool] = []

        thread = threading.Thread(target=lambda: outcome.append(self.worker.claim_and_run_one()))
        thread.start()
        self.wait_until(lambda: self.stage_of(entry_id) == "prepare")
        action(label, entry_id)
        thread.join(10)
        return label

    def test_a_cancel_clears_the_forecast_and_says_so(self) -> None:
        def cancel(label: str, entry_id: int) -> None:
            self.assertTrue(self.worker.cancel_running(entry_id))
            self.assertIsNone(self.worker.latest_forecast(entry_id))

        self.run_blocked(cancel)
        self.assertIsNone(self.forecasts()[-1]["forecast"])

    def test_a_park_reserved_and_withdrawn_is_published(self) -> None:
        def park(label: str, entry_id: int) -> None:
            before = len(self.forecasts())
            park_entry(self.store, self.worker, entry_id)
            self.assertGreater(len(self.forecasts()), before)
            before = len(self.forecasts())
            unpark_entry(self.store, self.worker, entry_id)
            self.assertGreater(len(self.forecasts()), before)
            self.worker.cancel_running(entry_id)

        self.run_blocked(park)


class JobPlanTests(QueueWorkerCase):
    def job(self, runs: int):
        path = self.write_job(job_data(run_count=runs, prompt_pairs=[{"name": "only", "positive": "text"}]))
        return load_job(path, self.global_config, self.params)

    def test_the_plan_holds_each_run_the_entry_will_execute_described_as_its_timing_will_record_it(self) -> None:
        job = self.job(4)
        plan = job_plan(self.executor, job, first_run=1, executable="draw-things-cli")
        self.assertEqual([run.number for run in plan.runs], [1, 2, 3, 4])
        self.assertEqual(plan.cooldown, job.cooldown)
        self.assertEqual({(run.backend, run.pipeline) for run in plan.runs}, {("draw-things-cli", "i2v")})
        self.assertEqual(plan.runs[0].workload, self.executor.describe_run(job, 1, executable="draw-things-cli").workload)

    def test_a_resumed_entry_forecasts_only_the_runs_it_will_execute(self) -> None:
        plan = job_plan(self.executor, self.job(5), first_run=3, executable="draw-things-cli")
        self.assertEqual([run.number for run in plan.runs], [3, 4, 5])


class QueueLearningTests(QueueWorkerCase):
    """The worker tells the knob updater when a run's timing is complete, and nothing else."""

    def test_the_updater_is_told_after_each_complete_run_and_when_the_job_ends(self) -> None:
        updater = mock.Mock()
        worker = self.build_worker(knob_updater=updater)
        label = self.submit(run_count=2)
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")
        # Once for each of the two runs, and once more when the job ends, in case the last run's row was stored after its event.
        self.assertEqual(updater.notify.call_count, 3)

    def test_a_worker_with_no_updater_runs_as_before(self) -> None:
        worker = self.build_worker()
        label = self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")

    def test_the_updater_is_started_and_stopped_with_the_worker(self) -> None:
        updater = mock.Mock()
        worker = self.build_worker(knob_updater=updater)
        worker.start()
        worker.stop()
        updater.start.assert_called_once_with()
        updater.stop.assert_called_once_with()

    def test_the_queue_is_quiet_when_idle_and_between_runs_but_not_while_generating(self) -> None:
        worker = self.build_worker()
        self.assertTrue(worker.is_quiet())
        seen: list[bool] = []
        label = self.submit(run_count=2)

        def note(kind: str, data: dict[str, Any]) -> None:
            if kind == "forecast_updated" and data["forecast"] is not None and data["forecast"]["stage"] == "prepare":
                seen.append(worker.is_quiet())

        worker = self.build_worker(on_event=note)
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertTrue(seen)
        self.assertFalse(any(seen))
        self.assertTrue(worker.is_quiet())


class QueueLearningEndToEndTests(QueueWorkerCase):
    """A real updater thread reading the real store the worker writes, from another thread."""

    def build(self):
        from framecontrol.services.knob_updater import KnobUpdater
        from framecontrol.services.timing_knob_files import KnobFiles

        updater = KnobUpdater(self.paths, self.store, poll=0.01)
        worker = self.build_worker(knob_updater=updater)
        updater.set_quiet(worker.is_quiet)
        updater.start()
        self.addCleanup(updater.stop)
        return worker, updater, KnobFiles(self.paths.timing_data)

    def test_runs_that_completed_reach_the_ring_and_the_files_after_the_job(self) -> None:
        worker, updater, files = self.build()
        label = self.submit(run_count=2)
        self.assertTrue(worker.claim_and_run_one())
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertTrue(updater.wait_idle(15))
        counts, entries = files.read_ring()
        self.assertEqual(len(entries), 2, updater.last_report)
        self.assertEqual(sum(counts.values()), 2)
        self.assertEqual(sorted(entry["r"] for entry in entries), [1, 2])
        self.assertTrue((files.directory / "tuning.yaml").is_file())
        self.assertTrue((files.directory / "draw-things-cli.yaml").is_file())

    def test_the_last_run_of_a_session_is_not_left_for_the_next_one(self) -> None:
        worker, updater, files = self.build()
        self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        self.assertTrue(updater.wait_idle(15))
        self.assertEqual(len(files.read_ring()[1]), 1)

    def test_a_worker_with_no_updater_writes_nothing_under_the_project(self) -> None:
        worker = self.build_worker()
        self.submit(run_count=1)
        self.assertTrue(worker.claim_and_run_one())
        self.assertFalse(self.paths.timing_data.exists())
