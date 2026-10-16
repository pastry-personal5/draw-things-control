"""Tests for ``WorkerStatus``: the claim/run/cooldown bookkeeping ``QueueWorker`` exposes to the server (``GET
/queue``, ``GET /queue/{id}``, ``WatchQueueEntry``)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from framecontrol.jobs.events import CooldownEnded, CooldownStarted, RunFinished, RunStarted, RunStatus
from framecontrol.services.queue_worker_status import WorkerStatus

NOW = datetime(2026, 9, 28, 12, 0, 0)


def run_started(number: int) -> RunStarted:
    return RunStarted(at="2026-09-28T12:00:00+00:00", number=number, total=1, pair="only", positive="text", negative=None, input=None, resized_input=None, output="out.png", last_frame=None, command=())


def run_finished(number: int) -> RunFinished:
    return RunFinished(at="2026-09-28T12:00:00+00:00", number=number, status=RunStatus.SUCCEEDED, exit_code=0, seconds=1.0, output="out.png", last_frame=None)


class CurrentRunTests(unittest.TestCase):
    def test_nothing_is_reported_before_a_run_starts(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        self.assertIsNone(status.current_run())

    def test_releasing_the_entry_clears_the_run_even_without_a_run_finished(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.entry_claimed()
        status.observe_run(run_started(1))
        self.assertIsNotNone(status.current_run())
        status.entry_released()
        self.assertIsNone(status.current_run())


class BetweenRunsTests(unittest.TestCase):
    """The run the entry is between runs after, which a front end words a park's outcome from."""

    def test_it_lasts_from_a_succeeded_run_through_its_cooldown_until_the_next_run_starts(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        self.assertIsNone(status.between_runs_after())
        status.observe_run(run_started(2))
        self.assertIsNone(status.between_runs_after())
        status.observe_run(run_finished(2))
        self.assertEqual(status.between_runs_after(), 2)
        status.observe_run(CooldownStarted(at="2026-09-28T12:00:00+00:00", after_run=2, seconds=60.0, until="12:01:00"))
        self.assertEqual(status.between_runs_after(), 2)
        # A park that ends the cooldown ends it before the entry reads parked; the run is still named.
        status.observe_run(CooldownEnded(at="2026-09-28T12:00:30+00:00", waited_seconds=30.0, cut_short=True))
        self.assertEqual(status.between_runs_after(), 2)
        status.observe_run(run_started(3))
        self.assertIsNone(status.between_runs_after())

    def test_a_failed_run_and_the_entrys_release_clear_it(self) -> None:
        status = WorkerStatus(clock=lambda: NOW)
        status.observe_run(run_finished(1))
        status.observe_run(replace(run_finished(2), status=RunStatus.FAILED))
        self.assertIsNone(status.between_runs_after())
        status.observe_run(run_finished(3))
        status.entry_released()
        self.assertIsNone(status.between_runs_after())
