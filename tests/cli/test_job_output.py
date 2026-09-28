"""Pins the exact text of validate-job, so moving its helpers cannot change it."""

import itertools
from datetime import datetime
from unittest import mock

from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli.app import CliServices, app
from draw_things_control.jobs.parsing import load_job
from draw_things_control.jobs.text import job_summary
from tests.fixtures import BASE_CONFIG, FakeToolkit, JobTestCase, job_data, job_executor

IGNORED = [
    "INFO Ignoring batchCount (4) from config_file batch.yaml: not used in i2v jobs; the job's run_count sets the number of runs",
    "INFO Ignoring width (640) from config_file batch.yaml: desired_input_width/desired_input_height set the size (832x448)",
    "INFO Ignoring height (448) from config_file batch.yaml: desired_input_width/desired_input_height set the size (832x448)",
    "INFO Input photo.jpg (1920x1080) will be scaled to 832x468 and cropped to 832x448 (4.3%) for run 1",
]


class JobOutputTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.global_path = self.root / "global-config.yaml"
        self.global_path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\ncooldown: {{mode: manual, seconds: 90}}\n", encoding="utf-8")
        self.write_base_config({**BASE_CONFIG, "batchCount": 4, "width": 640}, name="batch.yaml")
        self.write_image("photo.jpg", (1920, 1080))
        pairs = [{"name": "walk", "positive": "walk", "negative": "blurry", "runs": [1, 3]}, {"name": "wave", "positive": "wave", "default": True}]
        self.job_path = self.write_job(job_data(run_count=3, prompt_pairs=pairs, input="photo.jpg", desired_input_width=850, config_file="batch.yaml", config_override={"steps": 8}))
        numbers = itertools.count(1000)
        service = job_executor(runner_factory=mock.Mock(), find_executable=lambda executable: executable, frame_extractor=mock.Mock(), require_ffmpeg=lambda: "ffmpeg", clock=lambda: datetime(2026, 9, 24, 15, 30, 12), random_number=lambda: next(numbers), random_seed=lambda: 777)
        self.services = CliServices(self.paths, FakeToolkit(service))
        self.logged: list[str] = []
        sink = logger.add(lambda message: self.logged.append(str(message).rstrip("\n").replace(str(self.root), "ROOT")), format="{level} {message}")
        self.addCleanup(logger.remove, sink)

    def invoke(self, *arguments: str) -> str:
        result = self.runner.invoke(app, [*arguments, "--global-config", str(self.global_path)], obj=self.services)
        self.assertEqual(result.exit_code, 0, result.output)
        return result.stdout.replace(str(self.root), "ROOT")

    def test_validate_job_output(self) -> None:
        expected = "Valid job: ROOT/job.yaml\n  name: sunset-walk\n  mode: i2v\n  runs: 3 (walk, wave, walk)\n  cooldown: 90 s between runs, from global_config (2 waits, 3 min total)\n  input: ROOT/input/photo.jpg\n  output directory: ROOT/output/sunset-walk\n  config file: batch.yaml\n  model: base.ckpt\n  seed: 42 (config_file)\n"
        self.assertEqual(self.invoke("validate-job", str(self.job_path)), expected)
        self.assertEqual(self.logged, IGNORED)

    def test_validate_job_output_with_a_random_seed(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        job_path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}], config_file="noseed.yaml", cooldown={"mode": "off"}), name="noseed.yaml")
        output = self.invoke("validate-job", str(job_path))
        self.assertEqual(output.splitlines()[-6:], ["  cooldown: off (job)", "  input: ROOT/input/first-frame.png", "  output directory: ROOT/output/sunset-walk", "  config file: noseed.yaml", "  model: m.ckpt", "  seed: (random, drawn when the job starts) (random)"])
        self.assertEqual(self.logged, [])

    def test_auto_and_off_cooldown_lines(self) -> None:
        self.global_path.write_text(self.global_path.read_text(encoding="utf-8").replace("cooldown: {mode: manual, seconds: 90}", "cooldown: {mode: auto, minimum_seconds: 300, maximum_seconds: 1800}"), encoding="utf-8")
        self.assertIn("  cooldown: auto: half of each run's time, 5 min to 30 min, from global_config (up to 2 waits, 1 h total at most)\n", self.invoke("validate-job", str(self.job_path)))
        self.write_job(job_data(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}], cooldown={"mode": "auto", "ratio": 0.4}), name="ratio.yaml")
        self.assertIn("  cooldown: auto: 40% of each run's time, 0 s to 1 h, from job", self.invoke("validate-job", str(self.root / "ratio.yaml")))
        self.write_job(job_data(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}], cooldown={"mode": "off"}), name="off.yaml")
        self.assertIn("  cooldown: off (job)\n", self.invoke("validate-job", str(self.root / "off.yaml")))

    def test_job_summary_can_describe_a_random_seed_its_own_way(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        random_job = load_job(self.write_job(job_data(config_file="noseed.yaml"), name="noseed.yaml"), self.global_config, self.params)
        seeded_job = load_job(self.job_path, self.global_config, self.params)
        self.assertEqual(dict(job_summary(random_job))["seed"], "(random, drawn when the job starts) (random)")
        self.assertEqual(dict(job_summary(random_job, random_seed_text="random (later)"))["seed"], "random (later)")
        self.assertEqual(dict(job_summary(seeded_job, random_seed_text="random (later)"))["seed"], "42 (config_file)")
