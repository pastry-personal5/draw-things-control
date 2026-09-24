"""Tests for loading and validating job definitions."""

import subprocess
import sys
from pathlib import Path
from unittest import mock

from job_fixtures import BASE_CONFIG, JobTestCase, job_data
from loguru import logger
from PIL import Image

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

    def reported(self, job) -> list[str]:
        messages: list[str] = []
        sink = logger.add(messages.append, format="{level} {message}")
        try:
            report_ignored_config(job)
        finally:
            logger.remove(sink)
        return [message.rstrip("\n") for message in messages]

    def test_desired_size_sets_the_job_size_in_i2v_and_i2i(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        for mode in ("i2v", "i2i"):
            with self.subTest(mode):
                job = self.load(mode=mode, input="photo.jpg", desired_input_width=850)
                self.assertEqual(job.size, (832, 448))
                assert job.input_resize is not None
                self.assertEqual((job.input_resize.fit, job.input_resize.max_crop_percent), ("crop", 10))

    def test_without_desired_keys_nothing_changes(self) -> None:
        job = self.load()
        self.assertEqual((job.size, job.input_resize, job.ignored_size), (None, None, ()))
        self.write_image("photo.jpg", (1920, 1080))
        self.assert_invalid("is 1920x1080, but the job size is 832x448", input="photo.jpg")

    def test_desired_size_needs_no_width_or_height_from_the_config(self) -> None:
        self.write_base_config({"model": "m.ckpt"}, name="nosize.json")
        self.assert_invalid("config_override.width", config_file="nosize.json")
        job = self.load(config_file="nosize.json", desired_input_width=832)
        self.assertEqual((job.size, job.ignored_size), ((832, 448), ()))

    def test_invalid_desired_keys_name_the_key(self) -> None:
        self.assert_invalid("'desired_input_width' is not allowed in t2v jobs, which have no input image", mode="t2v", input=None, desired_input_width=832)
        self.assert_invalid("'desired_input_height' is not allowed in t2v", mode="t2v", input=None, desired_input_height=448)
        for value in (0, 8193, 832.0, True, "832"):
            with self.subTest(value=value):
                self.assert_invalid("'desired_input_width' must be an integer from 1 to 8192", desired_input_width=value)
        self.load(desired_input_width=8192, max_input_crop_percent=100)
        self.assert_invalid(r"'desired_input_width' is 50, which floors to 0", desired_input_width=50)

    def test_max_input_crop_percent_needs_exactly_one_size_key(self) -> None:
        message = "'max_input_crop_percent' requires exactly one of desired_input_width or desired_input_height"
        self.assert_invalid(message, max_input_crop_percent=10)
        self.assert_invalid(message, desired_input_width=832, desired_input_height=448, max_input_crop_percent=10)
        for value in (-1, 101, "10", False):
            with self.subTest(value=value):
                self.assert_invalid("'max_input_crop_percent' must be a number from 0 to 100", desired_input_width=832, max_input_crop_percent=value)
        self.assertEqual(self.load(desired_input_width=832, max_input_crop_percent=2.5).input_resize.max_crop_percent, 2.5)

    def test_crop_over_the_limit_names_the_job_file(self) -> None:
        self.write_image("panorama.png", (6000, 400))
        with self.assertRaisesRegex(ValueError, r"job.yaml: 'max_input_crop_percent' is 10, but panorama.png \(6000x400\) at width 1600"):
            self.load(input="panorama.png", desired_input_width=1600)
        self.assertEqual(self.load(input="panorama.png", desired_input_width=1600, max_input_crop_percent=45).size, (1600, 64))

    def test_undecodable_input_fails_only_when_a_copy_is_made(self) -> None:
        # A truncated JPEG still has a readable header, so only a full decode finds the damage.
        path = self.input_directory / "noise.jpg"
        Image.effect_noise((832, 448), 64).convert("RGB").save(path)
        path.write_bytes(path.read_bytes()[:2000])
        self.load(input="noise.jpg")
        # Already the target and upright: the original is used as-is, so it is not decoded.
        with mock.patch("job_definition.decode_image") as decode:
            self.assertIsNone(self.load(input="noise.jpg", desired_input_width=832).input_copy)
        decode.assert_not_called()
        self.assert_invalid("'input' could not be decoded", input="noise.jpg", desired_input_width=640)
        self.assert_invalid("'input' could not be decoded", input="noise.jpg", desired_input_width=640, desired_input_height=448)
        # A real run writes the copy right away, which decodes the input, so it skips this decode.
        load_job(self.write_job(job_data(input="noise.jpg", desired_input_width=640)), self.global_config, self.dt_config, decode_input=False)

    def test_loading_a_job_does_not_import_the_resizer(self) -> None:
        code = "import sys, job_definition; sys.exit('input_resize' in sys.modules or 'numpy' in sys.modules)"
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, check=False).returncode, 0)

    def test_image_over_the_pixel_limit_is_a_validation_error(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        with mock.patch("PIL.Image.MAX_IMAGE_PIXELS", 1000):
            self.assert_invalid("'input' is not a readable image: .*decompression bomb", input="photo.jpg", desired_input_width=832)

    def test_ignored_width_and_height_are_reported(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        job = self.load(input="photo.jpg", desired_input_width=1280, desired_input_height=720, config_override={"width": 832})
        self.assertEqual(job.ignored_size, (("config_override", "width", 832), ("config_file", "width", 832), ("config_file", "height", 448)))
        self.assertEqual(
            self.reported(job),
            [
                "INFO Ignoring config_override.width (832): desired_input_width/desired_input_height set the size (1280x704)",
                "INFO Ignoring width (832) from config_file base.json: desired_input_width/desired_input_height set the size (1280x704)",
                "INFO Ignoring height (448) from config_file base.json: desired_input_width/desired_input_height set the size (1280x704)",
                "INFO Input photo.jpg (1920x1080) will be scaled to 1252x704 and letterboxed to 1280x704 for run 1",
            ],
        )
