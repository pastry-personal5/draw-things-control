"""Tests for the job log lines, read from events."""

from __future__ import annotations

import unittest

from loguru import logger

from draw_things_control.core.cooldown import CooldownPolicy
from draw_things_control.jobs.events import JobFinished, JobStarted, JobStatus
from draw_things_control.jobs.log_writer import JobLogWriter


class JobLogWriterResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lines: list[str] = []
        self.sink = logger.add(lambda message: self.lines.append(str(message)), format="{message}", level="INFO")
        self.addCleanup(logger.remove, self.sink)
        self.writer = JobLogWriter()

    def started(self, *, first_run: int, total_runs: int) -> JobStarted:
        return JobStarted(at="t", job_name="walk", job_file="walk.yaml", source_text="", mode="i2v", total_runs=total_runs, output_directory="/out", input=None, model="m", seed=1, seed_source="random", cooldown=CooldownPolicy(mode="off"), cooldown_source="default", manifest=None, log=None, first_run=first_run)

    def test_a_resumed_chains_full_completion_logs_the_whole_chain_not_just_this_leg(self) -> None:
        self.writer(self.started(first_run=2, total_runs=3))
        # This leg's own completed_runs is 2 (runs 2 and 3); run 1 already succeeded, in an earlier execution.
        self.writer(JobFinished(at="t", status=JobStatus.SUCCEEDED, exit_code=0, completed_runs=2, total_runs=3, signal=None))
        self.assertTrue(any("3/3 runs completed" in line for line in self.lines), self.lines)

    def test_a_plain_chain_logs_its_own_completed_runs_unchanged(self) -> None:
        self.writer(self.started(first_run=1, total_runs=3))
        self.writer(JobFinished(at="t", status=JobStatus.FAILED, exit_code=3, completed_runs=1, total_runs=3, signal=None))
        self.assertTrue(any("1/3 runs completed" in line for line in self.lines))
