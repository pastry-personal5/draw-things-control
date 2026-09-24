"""Tests for loading and validating job definitions."""

from job_fixtures import BASE_CONFIG, JobTestCase, job_data
from loguru import logger

from job_definition import GenerationMode, load_job, report_ignored_config


class JobDefinitionTests(JobTestCase):
    def load(self, **changes: object):
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.dt_config)

    def assert_invalid(self, field: str, **changes: object) -> None:
        with self.assertRaisesRegex(ValueError, field):
            self.load(**changes)

    def test_valid_job_resolves_paths_and_defaults(self) -> None:
        job = self.load()
        self.assertEqual(job.mode, GenerationMode.I2V)
        self.assertEqual(job.input, self.input_directory / "first-frame.png")
        self.assertEqual(job.output_directory, self.output_directory / "sunset-walk")
        self.assertEqual(job.extension, "mov")
        self.assertEqual(job.model, "base.ckpt")

    def test_schedule_uses_explicit_batches_then_the_default_pair(self) -> None:
        pairs = [
            {"name": "walk", "positive": "walk", "batches": [1, 3, 5]},
            {"name": "wave", "positive": "wave", "batches": [2, 4]},
            {"name": "idle", "positive": "idle", "default": True},
        ]
        job = self.load(batch_count=7, prompt_pairs=pairs)
        self.assertEqual([pair.name for pair in job.schedule()], ["walk", "wave", "walk", "wave", "walk", "idle", "idle"])

    def test_single_pair_is_the_default(self) -> None:
        job = self.load(batch_count=3, prompt_pairs=[{"name": "only", "positive": "text"}])
        self.assertEqual([pair.name for pair in job.schedule()], ["only"] * 3)

    def test_output_directory_is_relative_to_global_output(self) -> None:
        job = self.load(output={"directory": "custom", "extension": "mp4"})
        self.assertEqual(job.output_directory, self.output_directory / "custom")
        self.assertEqual(job.extension, "mp4")

    def test_input_file_name_is_stripped_of_whitespace(self) -> None:
        self.assertEqual(self.load(input="  first-frame.png\t\n").input, self.input_directory / "first-frame.png")
        self.assert_invalid("'input' must be a file path", input="   ")

    def test_i2i_defaults_to_png_and_t2v_has_no_input(self) -> None:
        self.assertEqual(self.load(mode="i2i").extension, "png")
        job = self.load(mode="t2v", input=None)
        self.assertIsNone(job.input)
        self.assertEqual(job.extension, "mov")

    def test_invalid_jobs_name_the_field(self) -> None:
        self.assert_invalid("extra", extra=1)
        self.assert_invalid("'name'", name="Sunset Walk")
        self.assert_invalid("'name'", name=None)
        self.assert_invalid("'version'", version=2)
        self.assert_invalid("'mode'", mode="t2i")
        self.assert_invalid("'input'", input=None)
        self.assert_invalid("'input'", mode="t2v")
        self.assert_invalid("'input' file does not exist", input="missing.png")
        self.assert_invalid("batch_count", batch_count=0)
        self.assert_invalid("prompt_pairs", prompt_pairs=[])
        self.assert_invalid("positive", prompt_pairs=[{"name": "a"}])
        self.assert_invalid("duplicates", prompt_pairs=[{"name": "a", "positive": "x"}, {"name": "a", "positive": "y", "default": True}])
        self.assert_invalid("at most one", prompt_pairs=[{"name": "a", "positive": "x", "default": True}, {"name": "b", "positive": "y", "default": True}])
        self.assert_invalid("batch 5", batch_count=5, prompt_pairs=[{"name": "a", "positive": "x", "batches": [1, 2, 3, 4]}, {"name": "b", "positive": "y"}])
        self.assert_invalid("outside 1..5", prompt_pairs=[{"name": "a", "positive": "x", "batches": [6]}, {"name": "b", "positive": "y", "default": True}])
        self.assert_invalid("already assigned", prompt_pairs=[{"name": "a", "positive": "x", "batches": [1]}, {"name": "b", "positive": "y", "batches": [1], "default": True}])
        self.assert_invalid("output.extension", output={"extension": "png"})
        self.assert_invalid("output.extension", mode="i2i", output={"extension": "mov"})
        self.assert_invalid("config_file", config_file=None)
        self.assert_invalid("not a path", config_file="../base.json")
        self.assert_invalid("available: base.json", config_file="missing.json")
        self.assert_invalid("run_timeout_seconds", run_timeout_seconds=0)

    def test_invalid_overrides_name_the_key(self) -> None:
        cases = {
            "unknown": {"sampler": 17},
            "steps": {"steps": 0},
            "guidance_scale": {"guidance_scale": -1},
            "shift": {"shift": 0},
            "width": {"width": 800},
            "strength": {"strength": 1.5},
            "seed": {"seed": -1},
            "refiner_start": {"refiner_start": 2},
            "model": {"model": ""},
        }
        for field, override in cases.items():
            with self.subTest(field):
                self.assert_invalid("sampler" if field == "unknown" else field, config_override=override)
        self.assert_invalid("frame_count", mode="i2i", config_override={"frame_count": 17})
        self.assert_invalid("config_override.seed", config_override={"seed": 2**32})

    def test_model_and_refiner_requirements(self) -> None:
        self.write_base_config({"width": 832, "height": 448}, name="bare.json")
        self.assert_invalid("config_override.model", config_file="bare.json")
        self.assert_invalid("refiner_start", config_file="bare.json", config_override={"model": "m.ckpt", "refiner_start": 0.1})
        job = self.load(config_file="bare.json", config_override={"model": "m.ckpt", "refiner_model": "r.ckpt", "refiner_start": 0.1})
        self.assertEqual(job.model, "m.ckpt")

    def test_seed_precedence(self) -> None:
        self.assertEqual(self.load(config_override={"seed": 7}).configured_seed(), (7, "config_override"))
        self.assertEqual(self.load().configured_seed(), (42, "config_file"))
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.json")
        self.assertEqual(self.load(config_file="noseed.json").configured_seed(), (None, "random"))
        self.assertEqual(self.load(config_override={"seed": 2**32 - 1}).configured_seed(), (2**32 - 1, "config_override"))
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448, "seed": 2**32}, name="bigseed.json")
        self.assert_invalid("config_file", config_file="bigseed.json")

    def test_i2v_ignores_batch_count_from_the_config_file(self) -> None:
        self.write_base_config({**BASE_CONFIG, "batchCount": 4}, name="batch.json")
        job = self.load(config_file="batch.json")
        self.assertNotIn("batchCount", job.base_config)
        self.assertEqual(job.ignored_config, {"batchCount": 4})
        messages: list[str] = []
        sink = logger.add(messages.append, format="{level} {message}")
        try:
            report_ignored_config(job)
        finally:
            logger.remove(sink)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("INFO Ignoring batchCount (4) from config_file batch.json"))

    def test_other_modes_keep_batch_count(self) -> None:
        self.write_base_config({**BASE_CONFIG, "batchCount": 4}, name="batch.json")
        for mode, changes in (("i2i", {}), ("t2v", {"input": None})):
            with self.subTest(mode):
                job = self.load(config_file="batch.json", mode=mode, **changes)
                self.assertEqual(job.base_config["batchCount"], 4)
                self.assertEqual(job.ignored_config, {})
