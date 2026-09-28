"""Tests for writing job manifests."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from loguru import logger

from draw_things_control.jobs.events import JobStatus
from draw_things_control.jobs.parsing import load_job
from draw_things_control.jobs.records import JobManifest, JobRecords, RunRecord, write_manifest
from tests.fixtures import JobTestCase, job_data


class JobManifestTests(unittest.TestCase):
    def test_rewrite_is_atomic_and_leaves_no_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job-20260924-153012-job.json"
            manifest = JobManifest(job_file="job.yaml", name="job", mode="i2v", config_file="base.json", config_override={}, seed=1, seed_source="random", cooldown_seconds=0.0, cooldown_source="default", cooldown={"mode": "off"}, started_at="t", log_file="job.log")
            write_manifest(path, manifest)
            manifest.runs.append(RunRecord(pair="walk", positive="p", negative=None, input=None, output="o.mov", last_frame=None, command=["x"], started_at="t"))
            manifest.status = JobStatus.SUCCEEDED
            write_manifest(path, manifest)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["runs"][0]["pair"], "walk")
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], [path.name])


class JobLogScopeTests(JobTestCase):
    """A job's log file holds only its own lines, never an unrelated line logged from the same process."""

    def test_a_line_logged_outside_the_job_never_reaches_its_log_file(self) -> None:
        job = load_job(self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params)
        logger.info("a line logged before the job, such as a server starting up")
        with JobRecords.open(job, write_records=True, seed=1, seed_source="random", execution_id=None, clock=lambda: datetime(2026, 9, 27, 10, 0, 0), random_number=lambda: 1000) as records:
            logger.info("a line from inside the job")
        logger.info("a line logged after the job, such as a server's own API request")
        assert records.log_path is not None
        log_text = records.log_path.read_text(encoding="utf-8")
        self.assertIn("a line from inside the job", log_text)
        self.assertNotIn("before the job", log_text)
        self.assertNotIn("after the job", log_text)
