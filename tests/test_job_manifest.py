"""Tests for writing job manifests."""

import json
import tempfile
import unittest
from pathlib import Path

from job_manifest import JobManifest, RunRecord, write_manifest


class JobManifestTests(unittest.TestCase):
    def test_rewrite_is_atomic_and_leaves_no_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job-20260924-153012-job.json"
            manifest = JobManifest(job_file="job.yaml", name="job", mode="i2v", config_file="base.json", config_override={}, seed=1, seed_source="random", started_at="t", log_file="job.log")
            write_manifest(path, manifest)
            manifest.runs.append(RunRecord(batch=1, pair="walk", positive="p", negative=None, input=None, output="o.mov", last_frame=None, command=["x"], started_at="t"))
            manifest.status = "succeeded"
            write_manifest(path, manifest)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["runs"][0]["pair"], "walk")
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], [path.name])
