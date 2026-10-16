"""The job files checked in under ``data/jobs/`` are valid version-2 jobs, so the examples cannot drift from the schema."""

from __future__ import annotations

import shutil
from pathlib import Path

from framecontrol.jobs.parsing import load_job
from tests.fixtures import JobTestCase

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((ROOT / "data/jobs").glob("example-job-*.yaml"))
PARAMETER_EXAMPLE = ROOT / "data/params/image-to-video-wan-2-2.example.yaml"
# Every image the examples name, relative to the input directory, and the size it needs.
INPUTS = {
    "9999-example/example-first-frame.png": (832, 448),
    "story/opening.png": (832, 448),
    "story/opening-832x448.png": (832, 448),
    "person-1.png": (512, 512),
    "person-2.png": (512, 512),
    "references/person-1.png": (512, 512),
    "references/person-2.png": (512, 512),
}


class ExampleJobTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The example parameter file is read as the user has it; nothing here writes to data/params.
        shutil.copy(PARAMETER_EXAMPLE, self.params / PARAMETER_EXAMPLE.name)
        for name, size in INPUTS.items():
            (self.input_directory / name).parent.mkdir(parents=True, exist_ok=True)
            self.write_image(name, size)

    def test_there_are_examples_to_check(self) -> None:
        names = {path.name for path in EXAMPLES}
        for expected in ("example-job-for-backend-draw-things-cli.yaml", "example-job-for-backend-h3-metal-cli.yaml", "example-job-for-backend-h3-metal-cli-r2av-two-images.yaml", "example-job-for-backend-h3-metal-cli-r2av-to-i2av-two-images.yaml", "example-job-mixed-backends.yaml"):
            self.assertIn(expected, names)

    def test_every_example_is_a_valid_version_2_job(self) -> None:
        for path in EXAMPLES:
            with self.subTest(example=path.name):
                job = load_job(path, self.global_config, self.params)
                self.assertEqual(job.version, 2)
                self.assertGreaterEqual(job.run_count, 1)
                self.assertTrue(all(run.timeout_seconds for run in job.effective_runs))

    def test_the_mixed_example_runs_the_chain_the_guide_describes(self) -> None:
        job = load_job(ROOT / "data/jobs/example-job-mixed-backends.yaml", self.global_config, self.params)
        self.assertEqual([(run.backend, run.pipeline) for run in job.effective_runs], [("h3-metal-cli", "r2av"), ("h3-metal-cli", "i2av"), ("draw-things-cli", "i2v"), ("h3-metal-cli", "i2av")])
        self.assertEqual([run.knobs["seconds"] for run in job.effective_runs if run.backend == "h3-metal-cli"], [5, 5, 8])
        self.assertEqual(job.effective_runs[2].knobs["frames"], 81)

    def test_the_per_run_examples_show_what_they_name(self) -> None:
        seconds = load_job(ROOT / "data/jobs/example-job-h3-per-run-seconds.yaml", self.global_config, self.params)
        self.assertEqual([run.knobs["seconds"] for run in seconds.effective_runs], [4, 8, 12])
        frames = load_job(ROOT / "data/jobs/example-job-draw-things-per-run-frames.yaml", self.global_config, self.params)
        self.assertEqual([run.knobs["frames"] for run in frames.effective_runs], [81, 49, 97])
        defaults = load_job(ROOT / "data/jobs/example-job-h3-r2av-defaults.yaml", self.global_config, self.params)
        self.assertEqual([(run.pipeline, run.knobs["seconds"]) for run in defaults.effective_runs], [("r2av", 5), ("i2av", 8), ("i2av", 10)])
        self.assertEqual(defaults.effective_runs[0].knobs["reference_pair"], "two-subjects")
