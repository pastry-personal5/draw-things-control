"""H3 reference inspection: the Jobs list's header-only read skips the decode a run's own parse does."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from framecontrol.jobs.backends.h3 import _inspect_reference


class InspectReferenceTests(unittest.TestCase):
    def truncated_jpeg(self, directory: Path) -> Path:
        """A JPEG whose header reads but whose pixel data is cut short."""
        path = directory / "cut.jpg"
        Image.effect_noise((256, 256), 80).convert("RGB").save(path)
        path.write_bytes(path.read_bytes()[:-8000])
        return path

    def test_an_image_with_a_readable_header_and_broken_pixels_fails_only_when_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.truncated_jpeg(Path(directory))
            facts = _inspect_reference("image", path, None, decode=False)
            with self.assertRaises(ValueError):
                _inspect_reference("image", path, None)
        self.assertEqual(facts["image_size"], [256, 256])


if __name__ == "__main__":
    unittest.main()
