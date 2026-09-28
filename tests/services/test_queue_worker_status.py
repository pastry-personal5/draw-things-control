"""Tests for ``WorkerStatus``: the claim/run/cooldown bookkeeping ``QueueWorker`` exposes to the server (``GET
/queue``, ``GET /queue/{id}``, ``WatchQueueEntry``)."""

from __future__ import annotations

import unittest
from datetime import datetime

from draw_things_control.jobs.events import RunFinished, RunOutput, RunStarted, RunStatus
from draw_things_control.services.queue_worker_status import WorkerStatus

NOW = datetime(2026, 9, 28, 12, 0, 0)


def run_started(number: int) -> RunStarted:
    return RunStarted(at="2026-09-28T12:00:00+00:00", number=number, total=1, pair="only", positive="text", negative=None, input=None, resized_input=None, output="out.png", last_frame=None, command=())


def run_output(number: int, progress: tuple[int, int] | None) -> RunOutput:
    return RunOutput(at="2026-09-28T12:00:00+00:00", number=number, stream="stdout", text="", progress=progress)


def run_finished(number: int) -> RunFinished:
    return RunFinished(at="2026-09-28T12:00:00+00:00", number=number, status=RunStatus.SUCCEEDED, exit_code=0, seconds=1.0, output="out.png", last_frame=None)


class CurrentStepTests(unittest.TestCase):
    def test_nothing_is_reported_before_a_run_starts(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        self.assertIsNone(status.current_step())

    def test_a_progress_reading_is_reported_once_output_carries_one(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.observe_run(run_started(1))
        self.assertIsNone(status.current_step())
        status.observe_run(run_output(1, (3, 20)))
        self.assertEqual(status.current_step(), (3, 20))
        status.observe_run(run_output(1, (7, 20)))
        self.assertEqual(status.current_step(), (7, 20))

    def test_output_with_no_progress_does_not_clear_the_last_reading(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.observe_run(run_started(1))
        status.observe_run(run_output(1, (3, 20)))
        status.observe_run(run_output(1, None))
        self.assertEqual(status.current_step(), (3, 20))

    def test_a_new_run_starts_with_no_step_reading_of_its_own(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.observe_run(run_started(1))
        status.observe_run(run_output(1, (18, 20)))
        status.observe_run(run_finished(1))
        status.observe_run(run_started(2))
        self.assertIsNone(status.current_step())

    def test_the_reading_clears_when_the_run_finishes(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.observe_run(run_started(1))
        status.observe_run(run_output(1, (10, 20)))
        status.observe_run(run_finished(1))
        self.assertIsNone(status.current_step())

    def test_releasing_the_entry_clears_the_reading_even_without_a_run_finished(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.entry_claimed()
        status.observe_run(run_started(1))
        status.observe_run(run_output(1, (5, 20)))
        status.entry_released()
        self.assertIsNone(status.current_step())
        self.assertIsNone(status.current_run())


if __name__ == "__main__":
    unittest.main()
