"""Tests for the input image size check."""

from job_fixtures import JobTestCase, job_data

from input_size import read_image_size
from job_definition import load_job


class InputSizeTests(JobTestCase):
    def load(self, **changes: object):
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.dt_config)

    def test_matching_size_passes(self) -> None:
        self.assertEqual(self.load().input, self.input_directory / "first-frame.png")

    def test_mismatched_size_names_both_sizes_and_the_source(self) -> None:
        self.write_image("photo.png", (1920, 1080))
        with self.assertRaisesRegex(ValueError, r"is 1920x1080, but the job size is 832x448 \(width and height from config_file base.json\)"):
            self.load(input="photo.png")

    def test_override_size_takes_precedence(self) -> None:
        self.write_image("square.png", (512, 512))
        self.load(input="square.png", config_override={"width": 512, "height": 512})
        with self.assertRaisesRegex(ValueError, "width from config_override, height from config_file"):
            self.load(input="square.png", config_override={"width": 512})

    def test_exif_rotation_swaps_width_and_height(self) -> None:
        path = self.write_image("rotated.jpg", (448, 832), orientation=6)
        self.assertEqual(read_image_size(path), (832, 448))
        self.load(input="rotated.jpg")

    def test_unreadable_image_is_rejected(self) -> None:
        (self.input_directory / "broken.png").write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "'input' is not a readable image"):
            self.load(input="broken.png")

    def test_missing_width_or_height_is_rejected_for_image_modes(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832}, name="noheight.json")
        with self.assertRaisesRegex(ValueError, "config_override.height"):
            self.load(config_file="noheight.json")
        self.assertIsNone(self.load(config_file="noheight.json", mode="t2v", input=None).input)
