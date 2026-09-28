"""Tests for the rules and limits every job the HTTP API runs or writes must meet (Milestone 02)."""

from __future__ import annotations

import shutil

from draw_things_control.core.errors import LimitExceededError, OutsideDirectoryError, TimeoutRequiredError
from draw_things_control.core.global_config import ApiLimits
from draw_things_control.jobs.parsing import load_job
from draw_things_control.services.api_rules import check_api_rules, check_job_limits, check_job_rules, check_queue_not_full
from tests.fixtures import JobTestCase, job_data

ONE_PAIR = [{"name": "only", "positive": "text"}]


class ApiRulesTests(JobTestCase):
    def job(self, **changes: object):
        path = self.write_job(job_data(prompt_pairs=ONE_PAIR, **changes))
        return load_job(path, self.global_config, self.params)

    def test_run_timeout_seconds_is_required(self) -> None:
        job = self.job(run_count=1)
        with self.assertRaises(TimeoutRequiredError) as context:
            check_job_rules(job, self.global_config)
        self.assertEqual(context.exception.field, "run_timeout_seconds")

    def test_a_job_with_run_timeout_seconds_and_directories_inside_passes(self) -> None:
        job = self.job(run_count=1, run_timeout_seconds=60)
        check_job_rules(job, self.global_config)  # does not raise

    def test_input_outside_the_input_directory_is_refused_even_through_a_symbolic_link(self) -> None:
        written = self.write_image("outside.png", (832, 448))
        outside = self.root / "outside.png"
        shutil.move(written, outside)
        link = self.input_directory / "link.png"
        link.symlink_to(outside)
        job = self.job(run_count=1, run_timeout_seconds=60, input=str(link))
        with self.assertRaises(OutsideDirectoryError) as context:
            check_job_rules(job, self.global_config)
        self.assertEqual(context.exception.field, "input")

    def test_output_directory_outside_the_global_output_directory_is_refused(self) -> None:
        outside = self.root / "elsewhere-out"
        job = self.job(run_count=1, run_timeout_seconds=60, output={"directory": str(outside)})
        with self.assertRaises(OutsideDirectoryError) as context:
            check_job_rules(job, self.global_config)
        self.assertEqual(context.exception.field, "output.directory")

    def test_a_job_exactly_at_the_run_limit_is_accepted_and_one_over_is_refused(self) -> None:
        job = self.job(run_count=3, run_timeout_seconds=60)
        check_job_limits(job, ApiLimits(max_job_runs=3, max_job_seconds=10_000_000))
        with self.assertRaises(LimitExceededError) as context:
            check_job_limits(job, ApiLimits(max_job_runs=2, max_job_seconds=10_000_000))
        self.assertEqual((context.exception.key, context.exception.limit, context.exception.value), ("max_job_runs", 2, 3))

    def test_a_resumes_remaining_runs_are_counted_not_the_whole_chain(self) -> None:
        job = self.job(run_count=7, run_timeout_seconds=60)
        # The whole chain (7) is over the limit, but only 2 runs are left.
        with self.assertRaises(LimitExceededError):
            check_job_limits(job, ApiLimits(max_job_runs=3, max_job_seconds=10_000_000))
        check_job_limits(job, ApiLimits(max_job_runs=3, max_job_seconds=10_000_000), remaining_runs=2)

    def test_the_worst_case_seconds_match_the_milestone_documents_worked_example(self) -> None:
        # 7 runs, run_timeout_seconds 3600, the default auto cooldown: 7*3600 + 6*1800 = 36,000s.
        job = self.job(run_count=7, run_timeout_seconds=3600, cooldown={"mode": "auto"})
        check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=36_000))
        with self.assertRaises(LimitExceededError) as context:
            check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=35_999))
        self.assertEqual(context.exception.key, "max_job_seconds")

    def test_queue_not_full_at_the_limit_is_accepted_and_one_over_is_refused(self) -> None:
        check_queue_not_full(19, ApiLimits(max_queued_jobs=20))
        with self.assertRaises(LimitExceededError) as context:
            check_queue_not_full(20, ApiLimits(max_queued_jobs=20))
        self.assertEqual((context.exception.key, context.exception.limit, context.exception.value), ("max_queued_jobs", 20, 20))

    def test_check_api_rules_checks_the_structural_rules_before_the_limits(self) -> None:
        job = self.job(run_count=1)  # no run_timeout_seconds
        with self.assertRaises(TimeoutRequiredError):
            check_api_rules(job, self.global_config, ApiLimits(), queued_count=999)

    def test_check_api_rules_passes_a_job_within_every_rule_and_limit(self) -> None:
        job = self.job(run_count=1, run_timeout_seconds=60)
        check_api_rules(job, self.global_config, ApiLimits(), queued_count=0)
