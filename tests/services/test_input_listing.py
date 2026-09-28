"""Tests for listing the images available as a job's input."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from draw_things_control.jobs.inputs.size import read_image_info as real_read_image_info
from draw_things_control.services.input_listing import Cache, InputCatalog, list_inputs


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


class ListInputsCacheTests(unittest.TestCase):
    """``cache`` (Milestone 02's efficiency fix, phase-3 changelog 2026-09-28): a file already listed is read again
    only when its modification time or size changed."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)

    def write_image(self, relative: str, size: tuple[int, int]) -> Path:
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "orange").save(path)
        return path

    def test_an_unchanged_file_is_read_only_once(self) -> None:
        self.write_image("a.png", (10, 10))
        cache: Cache = {}
        list_inputs(self.directory, cache=cache)
        with patch("draw_things_control.services.input_listing.read_image_info", wraps=real_read_image_info) as spy:
            images = list_inputs(self.directory, cache=cache)
        spy.assert_not_called()
        self.assertEqual([image.path for image in images], ["a.png"])

    def test_a_changed_file_is_read_again(self) -> None:
        path = self.write_image("a.png", (10, 10))
        cache: Cache = {}
        list_inputs(self.directory, cache=cache)
        Image.new("RGB", (20, 20), "orange").save(path)
        with patch("draw_things_control.services.input_listing.read_image_info", wraps=real_read_image_info) as spy:
            images = list_inputs(self.directory, cache=cache)
        spy.assert_called_once()
        self.assertEqual((images[0].width, images[0].height), (20, 20))

    def test_a_removed_file_is_dropped_from_the_cache(self) -> None:
        path = self.write_image("a.png", (10, 10))
        cache: Cache = {}
        list_inputs(self.directory, cache=cache)
        path.unlink()
        self.assertEqual(list_inputs(self.directory, cache=cache), [])
        self.assertEqual(cache, {})

    def test_input_catalog_reuses_its_own_cache_across_calls(self) -> None:
        self.write_image("a.png", (10, 10))
        catalog = InputCatalog(self.directory)
        catalog.list()
        with patch("draw_things_control.services.input_listing.read_image_info", wraps=real_read_image_info) as spy:
            images = catalog.list()
        spy.assert_not_called()
        self.assertEqual([image.path for image in images], ["a.png"])


if __name__ == "__main__":
    unittest.main()
