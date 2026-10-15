"""Tests for the rules and limits every job the HTTP API runs or writes must meet."""

from __future__ import annotations

import shutil

from framecontrol.core.errors import LimitExceededError, OutsideDirectoryError, TimeoutRequiredError
from framecontrol.core.global_config import ApiLimits
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.services.api_rules import check_api_rules, check_job_limits, check_job_rules, check_queue_not_full
from tests.fixtures import JobTestCase, job_data, load_legacy_job

ONE_PAIR = [{"name": "only", "positive": "text"}]


class ApiRulesTests(JobTestCase):
    def job(self, **changes: object):
        path = self.write_legacy_job(job_data(prompt_pairs=ONE_PAIR, **changes))
        return load_legacy_job(path, self.global_config, self.params)

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
        with self.assertRaises(OutsideDirectoryError) as context:
            self.job(run_count=1, run_timeout_seconds=60, input=str(link))
        self.assertEqual(context.exception.field, "input")

    def test_output_directory_outside_the_global_output_directory_is_refused(self) -> None:
        outside = self.root / "elsewhere-out"
        with self.assertRaises(OutsideDirectoryError) as context:
            self.job(run_count=1, run_timeout_seconds=60, output={"directory": str(outside)})
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
        # 7 runs, run_timeout_seconds 3600, the default auto cooldown: 7*3600 + 6*1800 = 36,000s, for an image job,
        # which has no color_drift check.
        job = self.job(mode="i2i", run_count=7, run_timeout_seconds=3600, cooldown={"mode": "auto"}, config_override=None)
        check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=36_000))
        with self.assertRaises(LimitExceededError) as context:
            check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=35_999))
        self.assertEqual(context.exception.key, "max_job_seconds")

    def test_a_video_jobs_worst_case_adds_the_drift_check_limit_to_each_run(self) -> None:
        # 7*3600 + 6*1800 = 36,000s, and 7 color_drift checks of 300 seconds each: 38,100s.
        job = self.job(run_count=7, run_timeout_seconds=3600, cooldown={"mode": "auto"})
        check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=38_100))
        with self.assertRaises(LimitExceededError):
            check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=38_099))

    def test_a_correcting_jobs_worst_case_adds_the_correction_limit_to_each_run(self) -> None:
        # 38,100s as above, and 7 corrections of 17 frames at 10 + 17 seconds each: 38,289s. The drift
        # check counts too, since a correction stopped before its first pass is done leaves it to run afterward.
        job = self.job(run_count=7, run_timeout_seconds=3600, cooldown={"mode": "auto"}, color={"anchor": "blend"}, config_override={"frame_count": 17})
        check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=38_289))
        with self.assertRaises(LimitExceededError):
            check_job_limits(job, ApiLimits(max_job_runs=100, max_job_seconds=38_288))

    def test_h3_output_limit_counts_staged_correction_copies_and_remaining_runs(self) -> None:
        data = job_data(backend="h3", mode="i2av", run_count=2, run_timeout_seconds=60, config_file=None, prompt_pairs=[{"name": "scene", "positive": "wind", "default": True}], backend_settings={"width": 768, "height": 1344, "frames": 96, "seconds": 4, "steps": 30, "seed": 42}, output={"extension": "mp4"}, color={"anchor": "previous"})
        job = load_legacy_job(self.write_legacy_job(data), self.global_config, self.params)
        backend = DEFAULT_BACKENDS.get("h3")
        estimated = backend.estimated_output_bytes(job, 2)
        self.assertGreater(estimated, backend.estimated_output_bytes(job, 1))
        self.assertEqual(estimated, 2 * backend.estimated_output_bytes(job, 1))
        check_job_limits(job, ApiLimits(max_job_seconds=10_000_000, max_job_output_bytes=estimated))
        with self.assertRaises(LimitExceededError) as context:
            check_job_limits(job, ApiLimits(max_job_seconds=10_000_000, max_job_output_bytes=estimated - 1))
        self.assertEqual(context.exception.key, "max_job_output_bytes")
        check_job_limits(job, ApiLimits(max_job_seconds=10_000_000, max_job_output_bytes=backend.estimated_output_bytes(job, 1)), remaining_runs=1)

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
