"""Tests for loading and validating job definitions."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from PIL import Image

from draw_things_control.core.global_config import DEFAULT_COOLDOWN, CooldownPolicy
from draw_things_control.jobs.job_definition import GenerationMode, load_job
from draw_things_control.jobs.job_report import auto_wait_text, cooldown_details, duration_text, ignored_config_lines, policy_text, seconds_text, share_text
from tests.fixtures import BASE_CONFIG, JobTestCase, job_data


class JobDefinitionTests(JobTestCase):
    def load(self, **changes: object):
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.params)

    def assert_invalid(self, field: str, **changes: object) -> None:
        with self.assertRaisesRegex(ValueError, field):
            self.load(**changes)

    def test_renamed_batch_keys_name_their_replacements(self) -> None:
        self.assert_invalid("'batch_count' was renamed to run_count", batch_count=5)
        self.assert_invalid(r"'prompt_pairs\[0\]\.batches' was renamed to runs", prompt_pairs=[{"name": "a", "positive": "x", "batches": [1]}])

    def test_valid_job_resolves_paths_and_defaults(self) -> None:
        job = self.load()
        self.assertEqual(job.mode, GenerationMode.I2V)
        self.assertEqual(job.input, self.input_directory / "first-frame.png")
        self.assertEqual(job.output_directory, self.output_directory / "sunset-walk")
        self.assertEqual(job.extension, "mov")
        self.assertEqual(job.model, "base.ckpt")

    def test_schedule_uses_explicit_runs_then_the_default_pair(self) -> None:
        pairs = [
            {"name": "walk", "positive": "walk", "runs": [1, 3, 5]},
            {"name": "wave", "positive": "wave", "runs": [2, 4]},
            {"name": "idle", "positive": "idle", "default": True},
        ]
        job = self.load(run_count=7, prompt_pairs=pairs)
        self.assertEqual([pair.name for pair in job.schedule()], ["walk", "wave", "walk", "wave", "walk", "idle", "idle"])

    def test_single_pair_is_the_default(self) -> None:
        job = self.load(run_count=3, prompt_pairs=[{"name": "only", "positive": "text"}])
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
        self.assert_invalid("run_count", run_count=0)
        self.assert_invalid("prompt_pairs", prompt_pairs=[])
        self.assert_invalid("positive", prompt_pairs=[{"name": "a"}])
        self.assert_invalid("duplicates", prompt_pairs=[{"name": "a", "positive": "x"}, {"name": "a", "positive": "y", "default": True}])
        self.assert_invalid("at most one", prompt_pairs=[{"name": "a", "positive": "x", "default": True}, {"name": "b", "positive": "y", "default": True}])
        self.assert_invalid("run 5", run_count=5, prompt_pairs=[{"name": "a", "positive": "x", "runs": [1, 2, 3, 4]}, {"name": "b", "positive": "y"}])
        self.assert_invalid("outside 1..5", prompt_pairs=[{"name": "a", "positive": "x", "runs": [6]}, {"name": "b", "positive": "y", "default": True}])
        self.assert_invalid("already assigned", prompt_pairs=[{"name": "a", "positive": "x", "runs": [1]}, {"name": "b", "positive": "y", "runs": [1], "default": True}])
        self.assert_invalid("output.extension", output={"extension": "png"})
        self.assert_invalid("output.extension", mode="i2i", output={"extension": "mov"})
        self.assert_invalid("config_file", config_file=None)
        self.assert_invalid("not a path", config_file="../base.yaml")
        self.assert_invalid("available: base.yaml", config_file="missing.yaml")
        self.assert_invalid("run_timeout_seconds", run_timeout_seconds=0)

    def test_config_file_must_name_a_yaml_file(self) -> None:
        (self.params / "base.json").write_text(json.dumps(BASE_CONFIG), encoding="utf-8")
        (self.params / "only.json").write_text(json.dumps(BASE_CONFIG), encoding="utf-8")
        self.assert_invalid(r"'config_file' base\.json is JSON; name base\.yaml instead", config_file="base.json")
        self.assert_invalid(r"'config_file' only\.json is JSON, but a job needs a YAML configuration; write only\.yaml", config_file="only.json")
        self.assert_invalid(r"must name a YAML file \(\.yaml or \.yml\)", config_file="base.txt")
        self.assertEqual(self.load(config_file="base.yaml").config_file, "base.yaml")

    def test_invalid_yaml_base_configuration_is_a_validation_error(self) -> None:
        (self.params / "twice.yml").write_text("model: a\nmodel: b\n", encoding="utf-8")
        self.assert_invalid(r"twice\.yml \(key 'model' appears twice on line 2\)", config_file="twice.yml")

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
        self.write_base_config({"width": 832, "height": 448}, name="bare.yaml")
        self.assert_invalid("config_override.model", config_file="bare.yaml")
        self.assert_invalid("refiner_start", config_file="bare.yaml", config_override={"model": "m.ckpt", "refiner_start": 0.1})
        job = self.load(config_file="bare.yaml", config_override={"model": "m.ckpt", "refiner_model": "r.ckpt", "refiner_start": 0.1})
        self.assertEqual(job.model, "m.ckpt")

    def test_seed_precedence(self) -> None:
        self.assertEqual(self.load(config_override={"seed": 7}).configured_seed(), (7, "config_override"))
        self.assertEqual(self.load().configured_seed(), (42, "config_file"))
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        self.assertEqual(self.load(config_file="noseed.yaml").configured_seed(), (None, "random"))
        self.assertEqual(self.load(config_override={"seed": 2**32 - 1}).configured_seed(), (2**32 - 1, "config_override"))
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448, "seed": 2**32}, name="bigseed.yaml")
        self.assert_invalid("config_file", config_file="bigseed.yaml")

    def test_i2v_ignores_run_count_from_the_config_file(self) -> None:
        self.write_base_config({**BASE_CONFIG, "batchCount": 4}, name="batch.yaml")
        job = self.load(config_file="batch.yaml")
        self.assertNotIn("batchCount", job.base_config)
        self.assertEqual(job.ignored_config, {"batchCount": 4})
        messages = ignored_config_lines(job)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("Ignoring batchCount (4) from config_file batch.yaml"))

    def test_other_modes_keep_run_count(self) -> None:
        self.write_base_config({**BASE_CONFIG, "batchCount": 4}, name="batch.yaml")
        for mode, changes in (("i2i", {}), ("t2v", {"input": None})):
            with self.subTest(mode):
                job = self.load(config_file="batch.yaml", mode=mode, **changes)
                self.assertEqual(job.base_config["batchCount"], 4)
                self.assertEqual(job.ignored_config, {})

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
        self.write_base_config({"model": "m.ckpt"}, name="nosize.yaml")
        self.assert_invalid("config_override.width", config_file="nosize.yaml")
        job = self.load(config_file="nosize.yaml", desired_input_width=832)
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
        with mock.patch("draw_things_control.jobs.job_definition.decode_image") as decode:
            self.assertIsNone(self.load(input="noise.jpg", desired_input_width=832).input_copy)
        decode.assert_not_called()
        self.assert_invalid("'input' could not be decoded", input="noise.jpg", desired_input_width=640)
        self.assert_invalid("'input' could not be decoded", input="noise.jpg", desired_input_width=640, desired_input_height=448)
        # A real run writes the copy right away, which decodes the input, so it skips this decode.
        load_job(self.write_job(job_data(input="noise.jpg", desired_input_width=640)), self.global_config, self.params, decode_input=False)

    def test_loading_a_job_does_not_import_the_resizer(self) -> None:
        code = "import sys, draw_things_control.jobs.job_definition; sys.exit('input_resize' in sys.modules or 'numpy' in sys.modules)"
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2], check=False).returncode, 0)

    def test_image_over_the_pixel_limit_is_a_validation_error(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        with mock.patch("PIL.Image.MAX_IMAGE_PIXELS", 1000):
            self.assert_invalid("'input' is not a readable image: .*decompression bomb", input="photo.jpg", desired_input_width=832)

    def test_ignored_width_and_height_are_reported(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        job = self.load(input="photo.jpg", desired_input_width=1280, desired_input_height=720, config_override={"width": 832})
        self.assertEqual(job.ignored_size, (("config_override", "width", 832), ("config_file", "width", 832), ("config_file", "height", 448)))
        self.assertEqual(
            ignored_config_lines(job),
            [
                "Ignoring config_override.width (832): desired_input_width/desired_input_height set the size (1280x704)",
                "Ignoring width (832) from config_file base.yaml: desired_input_width/desired_input_height set the size (1280x704)",
                "Ignoring height (448) from config_file base.yaml: desired_input_width/desired_input_height set the size (1280x704)",
                "Input photo.jpg (1920x1080) will be scaled to 1252x704 and letterboxed to 1280x704 for run 1",
            ],
        )

    def test_cooldown_comes_from_the_job_then_the_global_config_then_the_default(self) -> None:
        self.global_config = replace(self.global_config, cooldown=None)
        job = self.load()
        self.assertEqual((job.cooldown, job.cooldown_source), (DEFAULT_COOLDOWN, "default"))
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="manual", seconds=900.0))
        job = self.load()
        self.assertEqual((job.cooldown, job.cooldown_source), (CooldownPolicy(mode="manual", seconds=900.0), "global_config"))
        for value, expected in (({"mode": "manual", "seconds": 120}, CooldownPolicy(mode="manual", seconds=120.0)), ({"mode": "off"}, CooldownPolicy(mode="off")), ({"mode": False}, CooldownPolicy(mode="off")), ({"mode": "auto", "ratio": 0.4}, CooldownPolicy(mode="auto", ratio=0.4))):
            with self.subTest(value=value):
                job = self.load(cooldown=value)
                self.assertEqual((job.cooldown, job.cooldown_source), (expected, "job"))

    def test_a_job_cooldown_replaces_the_global_one_as_a_whole(self) -> None:
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="auto", ratio=0.25, minimum_seconds=300.0, maximum_seconds=1800.0))
        job = self.load(cooldown={"mode": "auto"})
        self.assertEqual((job.cooldown, job.cooldown_source), (DEFAULT_COOLDOWN, "job"))

    def test_cooldown_is_allowed_in_every_mode(self) -> None:
        self.assertEqual(self.load(mode="t2v", input=None, cooldown={"mode": "manual", "seconds": 60}).cooldown.seconds, 60.0)
        self.assertEqual(self.load(mode="i2i", cooldown={"mode": "manual", "seconds": 60}).cooldown.seconds, 60.0)

    def test_invalid_cooldown_is_rejected(self) -> None:
        for value, message in (
            ({"mode": "manual", "seconds": 3601}, r"'cooldown.seconds' must be a number of seconds from 0 to 3600"),
            ({"mode": "manual", "seconds": True}, r"'cooldown.seconds' must be a number of seconds from 0 to 3600"),
            ({"mode": "auto", "seconds": 60}, r"'cooldown.seconds' is not used with mode auto"),
            ({"mode": "off", "minimum_seconds": 0}, r"'cooldown.minimum_seconds' is not used with mode off"),
            ({"mode": "slow"}, r"'cooldown.mode' must be auto, manual, or off"),
            (900, r"'cooldown' must be a mapping"),
        ):
            with self.subTest(value=value):
                self.assert_invalid(rf"{self.root / 'job.yaml'}: {message}", cooldown=value)

    def test_the_old_cooldown_seconds_key_names_its_replacement(self) -> None:
        for value, same in ((300, "{mode: manual, seconds: 300}"), (0, "{mode: off}")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    self.load(cooldown_seconds=value)
                self.assertEqual(str(caught.exception), f"{self.root / 'job.yaml'}: 'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {same} for the same wait, or use mode auto or off (see config/global-config.example.yaml)")

    def test_cooldown_details(self) -> None:
        only = {"prompt_pairs": [{"name": "only", "positive": "text"}]}
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="manual", seconds=900.0))
        self.assertEqual(cooldown_details(self.load(run_count=7, **only)), "900 s between runs, from global_config (6 waits, 1 h 30 min total)")
        self.assertEqual(cooldown_details(self.load(run_count=2, **only, cooldown={"mode": "manual", "seconds": 90})), "90 s between runs, from job (1 wait, 1 min 30 s total)")
        self.assertEqual(cooldown_details(self.load(run_count=1, **only)), "900 s between runs, from global_config (no waits: 1 run)")
        self.assertEqual(cooldown_details(self.load(cooldown={"mode": "manual", "seconds": 0})), "none (job)")
        self.assertEqual(cooldown_details(self.load(cooldown={"mode": "off"})), "off (job)")
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="auto", minimum_seconds=300.0, maximum_seconds=1800.0))
        self.assertEqual(cooldown_details(self.load(run_count=3, **only)), "auto: half of each run's time, 5 min to 30 min, from global_config (up to 2 waits, 1 h total at most)")
        self.assertEqual(cooldown_details(self.load(run_count=2, **only, cooldown={"mode": "auto", "ratio": 0.4})), "auto: 40% of each run's time, 0 s to 1 h, from job (up to 1 wait, 1 h total at most)")
        self.assertEqual(cooldown_details(self.load(run_count=1, **only)), "auto: half of each run's time, 5 min to 30 min, from global_config (no waits: 1 run)")

    def test_cooldown_summaries_and_wait_reasons(self) -> None:
        auto = CooldownPolicy(mode="auto", minimum_seconds=300.0, maximum_seconds=1800.0)
        self.assertEqual([share_text(ratio) for ratio in (0.5, 0.4, 0.125, 1)], ["half", "40%", "12.5%", "100%"])
        self.assertEqual([policy_text(policy) for policy in (auto, CooldownPolicy(mode="manual", seconds=900.0), CooldownPolicy(mode="off"))], ["auto, half, 5 min to 30 min", "900 s", "off"])
        self.assertEqual(auto_wait_text(720, 0.5, 1, 1440, None), "12 min (half of run 1's 24 min)")
        self.assertEqual(auto_wait_text(720, 0.5, 1, 1440, None, commas=True), "12 min, half of run 1's 24 min,")
        self.assertEqual(auto_wait_text(300, 0.5, 1, 180, "minimum"), "5 min (the minimum; half of run 1's 3 min is less)")
        self.assertEqual(auto_wait_text(1800, 0.4, 2, 4800, "maximum"), "30 min (the maximum; 40% of run 2's 1 h 20 min is more)")

    def test_seconds_and_durations_read_as_written(self) -> None:
        self.assertEqual([seconds_text(value) for value in (900, 900.0, 0.5, 1234.5678, 0.00001, 412.3)], ["900 s", "900 s", "0.5 s", "1234.5678 s", "0.00001 s", "412.3 s"])
        self.assertEqual([duration_text(value) for value in (5400, 3600, 90, 0.4, 2.5, 0, 3661.25)], ["1 h 30 min", "1 h", "1 min 30 s", "0.4 s", "2.5 s", "0 s", "1 h 1 min 1.2 s"])
