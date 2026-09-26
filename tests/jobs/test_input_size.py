"""Tests for the input image size check and resize planning."""

import unittest

from draw_things_control.jobs.input_size import floor_to_step, read_image_info, resize_plan
from draw_things_control.jobs.job_definition import load_job
from tests.fixtures import JobTestCase, job_data


class InputSizeTests(JobTestCase):
    def load(self, **changes: object):
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.params)

    def test_matching_size_passes(self) -> None:
        self.assertEqual(self.load().input, self.input_directory / "first-frame.png")

    def test_mismatched_size_names_both_sizes_and_the_source(self) -> None:
        self.write_image("photo.png", (1920, 1080))
        with self.assertRaisesRegex(ValueError, r"is 1920x1080, but the job size is 832x448 \(width and height from config_file base.yaml\)"):
            self.load(input="photo.png")

    def test_override_size_takes_precedence(self) -> None:
        self.write_image("square.png", (512, 512))
        self.load(input="square.png", config_override={"width": 512, "height": 512})
        with self.assertRaisesRegex(ValueError, "width from config_override, height from config_file"):
            self.load(input="square.png", config_override={"width": 512})

    def test_exif_rotation_swaps_width_and_height(self) -> None:
        path = self.write_image("rotated.jpg", (448, 832), orientation=6)
        self.assertEqual(read_image_info(path), ((832, 448), 6))
        self.load(input="rotated.jpg")

    def test_unreadable_image_is_rejected(self) -> None:
        (self.input_directory / "broken.png").write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "'input' is not a readable image"):
            self.load(input="broken.png")

    def test_missing_width_or_height_is_rejected_for_image_modes(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832}, name="noheight.yaml")
        with self.assertRaisesRegex(ValueError, "config_override.height"):
            self.load(config_file="noheight.yaml")
        self.assertIsNone(self.load(config_file="noheight.yaml", mode="t2v", input=None).input)


class ResizePlanTests(unittest.TestCase):
    def plan(self, input_size: tuple[int, int], width: int | None = None, height: int | None = None, orientation: int | None = None, max_crop: float = 10):
        return resize_plan("photo.jpg", input_size, orientation, width, height, max_crop)

    def test_floor_to_step(self) -> None:
        self.assertEqual([floor_to_step(value) for value in (850, 470, 64, 63, 8192, 8200)], [832, 448, 64, 0, 8192, 8192])

    def test_width_alone_derives_height_and_crops(self) -> None:
        plan = self.plan((1920, 1080), width=850)
        self.assertEqual((plan.target_size, plan.fit, plan.scaled_size), ((832, 448), "crop", (832, 468)))
        self.assertAlmostEqual(plan.crop_percent, 20 / 468 * 100)
        self.assertEqual(plan.describe("photo.jpg"), "Input photo.jpg (1920x1080) will be scaled to 832x468 and cropped to 832x448 (4.3%) for run 1")

    def test_height_alone_derives_width_and_crops(self) -> None:
        plan = self.plan((1920, 1080), height=720)
        self.assertEqual((plan.target_size, plan.fit, plan.scaled_size), ((1216, 704), "crop", (1252, 704)))

    def test_both_given_letterboxes_without_derivation(self) -> None:
        plan = self.plan((1920, 1080), width=1280, height=720)
        self.assertEqual((plan.target_size, plan.fit, plan.scaled_size, plan.crop_percent), ((1280, 704), "letterbox", (1252, 704), None))
        self.assertIn("letterboxed to 1280x704", plan.describe("photo.jpg"))

    def test_upright_input_at_the_target_needs_no_copy(self) -> None:
        plan = self.plan((832, 448), width=832, height=448)
        self.assertEqual((plan.fit, plan.needs_copy), ("none", False))
        self.assertEqual(plan.describe("photo.jpg"), "Input photo.jpg is already 832x448; no resize needed")
        self.assertFalse(self.plan((832, 448), width=832, orientation=1).needs_copy)
        # Values that floor to the input's size, and a zero crop limit, still need no copy.
        for plan in (self.plan((832, 448), width=850), self.plan((832, 448), height=470), self.plan((832, 448), width=850, height=470), self.plan((832, 448), width=832, max_crop=0)):
            with self.subTest(desired=(plan.desired_width, plan.desired_height)):
                self.assertEqual((plan.target_size, plan.fit, plan.needs_copy, plan.box), ((832, 448), "none", False, None))

    def test_rotated_input_at_the_target_gets_an_upright_copy(self) -> None:
        plan = self.plan((832, 448), width=832, orientation=6)
        self.assertTrue(plan.needs_copy)
        self.assertEqual((plan.fit, plan.scaled_size, plan.crop_percent), ("rotate", (832, 448), 0.0))
        self.assertEqual(plan.describe("photo.jpg"), "Input photo.jpg (EXIF orientation 6) will be rotated upright; already 832x448")

    def test_exact_fit_is_scale_not_crop_or_letterbox(self) -> None:
        for plan in (self.plan((1664, 896), width=832, height=448), self.plan((1664, 896), width=832)):
            with self.subTest(desired=(plan.desired_width, plan.desired_height)):
                self.assertEqual((plan.fit, plan.scaled_size), ("scale", (832, 448)))
                self.assertEqual(plan.describe("photo.jpg"), "Input photo.jpg (1664x896) will be scaled to 832x448 for run 1")

    def test_crop_box_is_the_exact_centered_area(self) -> None:
        self.assertEqual(self.plan((1920, 1080), width=850).box, (0.0, 1080 * 10 / 468, 1920.0, 1080 - 1080 * 10 / 468))
        # At scale 1, whole pixels: an odd leftover comes off the bottom.
        self.assertEqual(self.plan((64, 71), width=64).box, (0.0, 3.0, 64.0, 67.0))
        self.assertIsNone(self.plan((1920, 1080), width=1280, height=720).box)

    def test_upscales_to_the_target(self) -> None:
        plan = self.plan((512, 512), width=1024)
        self.assertEqual((plan.target_size, plan.fit, plan.scaled_size, plan.crop_percent), ((1024, 1024), "scale", (1024, 1024), 0.0))
        self.assertEqual(plan.describe("photo.jpg"), "Input photo.jpg (512x512) will be scaled to 1024x1024 for run 1")

    def test_values_under_64_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, r"^'desired_input_width' is 50, which floors to 0 \(minimum 64\)$"):
            self.plan((1920, 1080), width=50)
        with self.assertRaisesRegex(ValueError, r"^'desired_input_height' derived from photo.jpg \(6000x400\) and width 640 is 42.7, which floors to 0 \(minimum 64\)$"):
            self.plan((6000, 400), width=640)

    def test_derived_value_over_8192_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, r"'desired_input_height' derived from .* is 16384.0, which floors to 16384 \(maximum 8192\)"):
            self.plan((1000, 2000), width=8192)

    def test_crop_over_the_limit_is_rejected(self) -> None:
        message = r"^'max_input_crop_percent' is 10, but photo.jpg \(6000x400\) at width 1600 scales to 1600x107 and would lose 43 px of 107 \(40.2%\) to reach 1600x64. Choose a larger desired_input_width, raise max_input_crop_percent, or set both keys to letterbox instead.$"
        with self.assertRaisesRegex(ValueError, message):
            self.plan((6000, 400), width=1600)
        self.assertEqual(self.plan((6000, 400), width=1600, max_crop=50).fit, "crop")

    def test_crop_limit_boundaries(self) -> None:
        # 1920x1080 at width 850 crops 4.27%.
        with self.assertRaisesRegex(ValueError, "max_input_crop_percent"):
            self.plan((1920, 1080), width=850, max_crop=4)
        self.plan((1920, 1080), width=850, max_crop=4.3)
        with self.assertRaisesRegex(ValueError, "max_input_crop_percent"):
            self.plan((1920, 1080), width=850, max_crop=0)
        # An exact fit crops nothing, so a limit of 0 allows it.
        self.assertEqual(self.plan((1920, 1088), width=1920, max_crop=0).crop_percent, 0.0)

    def test_manifest_record(self) -> None:
        self.assertEqual(
            self.plan((1920, 1080), width=850).as_manifest(),
            {"desired_width": 850, "desired_height": None, "max_crop_percent": 10, "input_size": [1920, 1080], "exif_orientation": None, "target_size": [832, 448], "fit": "crop", "scaled_size": [832, 468], "crop_percent": 4.3},
        )
        record = self.plan((1920, 1080), width=1280, height=720).as_manifest()
        self.assertEqual((record["fit"], record["max_crop_percent"], record["crop_percent"]), ("letterbox", None, None))
