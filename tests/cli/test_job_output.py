"""Pins the exact text of validate-job and run-job --dry-run, so moving their helpers cannot change it."""

import itertools
from datetime import datetime
from unittest import mock

from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli import app as cli
from draw_things_control.cli.app import app
from draw_things_control.jobs.job_definition import load_job
from draw_things_control.jobs.job_report import job_summary
from draw_things_control.jobs.job_service import JobService
from tests.fixtures import BASE_CONFIG, JobTestCase, job_data

CONFIG_JSON = '{"model":"base.ckpt","refinerModel":"base-refiner.ckpt","refinerStart":0.2,"width":832,"height":448,"seed":42,"steps":30}'
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
        service = JobService(runner_factory=mock.Mock(), find_executable=lambda executable: executable, frame_extractor=mock.Mock(), require_ffmpeg=lambda: "ffmpeg", clock=lambda: datetime(2026, 9, 24, 15, 30, 12), random_number=lambda: next(numbers), random_seed=lambda: 777)
        for patcher in (mock.patch("draw_things_control.core.generation_config.PARAMS_DIRECTORY", self.params), mock.patch.object(cli, "job_service", service)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logged: list[str] = []
        sink = logger.add(lambda message: self.logged.append(str(message).rstrip("\n").replace(str(self.root), "ROOT")), format="{level} {message}")
        self.addCleanup(logger.remove, sink)

    def invoke(self, *arguments: str) -> str:
        result = self.runner.invoke(app, [*arguments, "--global-config", str(self.global_path)])
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

    def test_dry_run_output(self) -> None:
        output = "ROOT/output/sunset-walk/sunset-walk-20260924-153012"
        expected = f"# Job sunset-walk (i2v): 3 runs, seed 42 (config_file), cooldown 90 s (global_config)\n# Output names are examples; a real run generates new ones.\n# Run 1/3 (pair walk)\ndraw-things-cli generate --model base.ckpt --prompt walk --negative-prompt blurry --steps 8 --width 832 --height 448 --seed 42 --config-json '{CONFIG_JSON}' --image '<photo.jpg resized to 832x448>' --output {output}-1000.mov\n# Cooldown 90 s\n# Run 2/3 (pair wave)\ndraw-things-cli generate --model base.ckpt --prompt wave --steps 8 --width 832 --height 448 --seed 42 --config-json '{CONFIG_JSON}' --image {output}-1000-last-frame.png --output {output}-1001.mov\n# Cooldown 90 s\n# Run 3/3 (pair walk)\ndraw-things-cli generate --model base.ckpt --prompt walk --negative-prompt blurry --steps 8 --width 832 --height 448 --seed 42 --config-json '{CONFIG_JSON}' --image {output}-1001-last-frame.png --output {output}-1002.mov\n"
        self.assertEqual(self.invoke("run-job", str(self.job_path), "--dry-run"), expected)
        self.assertEqual(self.logged, IGNORED)

    def test_auto_and_off_cooldown_lines(self) -> None:
        self.global_path.write_text(self.global_path.read_text(encoding="utf-8").replace("cooldown: {mode: manual, seconds: 90}", "cooldown: {mode: auto, minimum_seconds: 300, maximum_seconds: 1800}"), encoding="utf-8")
        self.assertIn("  cooldown: auto: half of each run's time, 5 min to 30 min, from global_config (up to 2 waits, 1 h total at most)\n", self.invoke("validate-job", str(self.job_path)))
        lines = self.invoke("run-job", str(self.job_path), "--dry-run").splitlines()
        self.assertEqual(lines[0], "# Job sunset-walk (i2v): 3 runs, seed 42 (config_file), cooldown auto, half of each run, 5 min to 30 min (global_config)")
        self.assertEqual([line for line in lines if line.startswith("# Cooldown")], ["# Cooldown auto: half of run 1's time, 5 min to 30 min", "# Cooldown auto: half of run 2's time, 5 min to 30 min"])
        self.write_job(job_data(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}], cooldown={"mode": "auto", "ratio": 0.4}), name="ratio.yaml")
        lines = self.invoke("run-job", str(self.root / "ratio.yaml"), "--dry-run").splitlines()
        self.assertTrue(lines[0].endswith(", cooldown auto, 40% of each run, 0 s to 1 h (job)"), lines[0])
        self.assertIn("# Cooldown auto: 40% of run 1's time, 0 s to 1 h", lines)
        self.write_job(job_data(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}], cooldown={"mode": "off"}), name="off.yaml")
        self.assertIn("  cooldown: off (job)\n", self.invoke("validate-job", str(self.root / "off.yaml")))
        lines = self.invoke("run-job", str(self.root / "off.yaml"), "--dry-run").splitlines()
        self.assertTrue(lines[0].endswith(", no cooldown (job)"), lines[0])
        self.assertFalse(any(line.startswith("# Cooldown") for line in lines))

    def test_dry_run_header_with_a_random_seed(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        job_path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}], config_file="noseed.yaml", cooldown={"mode": "off"}), name="noseed.yaml")
        lines = self.invoke("run-job", str(job_path), "--dry-run").splitlines()
        self.assertEqual(lines[:3], ["# Job sunset-walk (i2v): 1 runs, seed 777 (random), no cooldown (job)", "# Output names are examples; a real run generates new ones.", "# Run 1/1 (pair only)"])
        self.assertIn("--seed 777", lines[3])

    def test_job_summary_can_describe_a_random_seed_its_own_way(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        random_job = load_job(self.write_job(job_data(config_file="noseed.yaml"), name="noseed.yaml"), self.global_config)
        seeded_job = load_job(self.job_path, self.global_config)
        self.assertEqual(dict(job_summary(random_job))["seed"], "(random, drawn when the job starts) (random)")
        self.assertEqual(dict(job_summary(random_job, random_seed_text="random (later)"))["seed"], "random (later)")
        self.assertEqual(dict(job_summary(seeded_job, random_seed_text="random (later)"))["seed"], "42 (config_file)")
