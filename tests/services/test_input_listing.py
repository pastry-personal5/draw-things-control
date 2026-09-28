"""Tests for listing the images available as a job's input."""

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from draw_things_control.services.input_listing import list_inputs


class ListInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)

    def write_image(self, relative: str, size: tuple[int, int]) -> Path:
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "orange").save(path)
        return path

    def test_a_missing_directory_lists_nothing(self) -> None:
        self.assertEqual(list_inputs(self.directory / "missing"), [])

    def test_images_in_subdirectories_are_listed_by_relative_path(self) -> None:
        self.write_image("top.png", (100, 50))
        self.write_image("chain/frame-1.png", (200, 100))
        images = {image.path: image for image in list_inputs(self.directory)}
        self.assertEqual(set(images), {"top.png", "chain/frame-1.png"})
        self.assertEqual((images["chain/frame-1.png"].width, images["chain/frame-1.png"].height), (200, 100))
        self.assertGreater(images["top.png"].bytes, 0)

    def test_a_non_image_file_is_skipped_not_reported_as_an_error(self) -> None:
        (self.directory / "notes.txt").write_text("not an image")
        self.assertEqual(list_inputs(self.directory), [])

    def test_a_symbolic_link_to_a_file_is_skipped(self) -> None:
        real = self.write_image("real.png", (10, 10))
        (self.directory / "link.png").symlink_to(real)
        self.assertEqual([image.path for image in list_inputs(self.directory)], ["real.png"])

    def test_a_symbolic_link_to_a_directory_is_not_followed(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        Image.new("RGB", (10, 10), "orange").save(outside / "secret.png")
        (self.directory / "escape").symlink_to(outside)
        self.assertEqual(list_inputs(self.directory), [])

    def test_the_listing_is_sorted_by_path(self) -> None:
        self.write_image("b.png", (1, 1))
        self.write_image("a.png", (1, 1))
        self.assertEqual([image.path for image in list_inputs(self.directory)], ["a.png", "b.png"])


if __name__ == "__main__":
    unittest.main()
