"""Tests for loading the global configuration."""

import tempfile
import unittest
from pathlib import Path

from global_config import load_global_config


class GlobalConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        (self.root / "input").mkdir()
        self.path = self.root / "global-config.yaml"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_valid_config_expands_home(self) -> None:
        self.write(f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: ~/draw-things-output\n")
        config = load_global_config(self.path)
        self.assertEqual(config.input_directory, self.root / "input")
        self.assertEqual(config.output_directory, Path.home() / "draw-things-output")

    def test_invalid_configs_name_the_field(self) -> None:
        cases = {
            "relative": ("version: 1\ninput_directory: input\noutput_directory: /tmp/out\n", "input_directory"),
            "missing key": (f"version: 1\ninput_directory: {self.root / 'input'}\n", "output_directory"),
            "unknown key": (f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\nextra: 1\n", "extra"),
            "missing input directory": (f"version: 1\ninput_directory: {self.root / 'nope'}\noutput_directory: /tmp/out\n", "input_directory"),
            "wrong version": (f"version: 2\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n", "version"),
        }
        for label, (text, field) in cases.items():
            with self.subTest(label):
                self.write(text)
                with self.assertRaisesRegex(ValueError, field):
                    load_global_config(self.path)

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            load_global_config(self.root / "absent.yaml")

    def test_write_job_records_is_optional_and_boolean(self) -> None:
        base = f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n"
        self.write(base)
        self.assertFalse(load_global_config(self.path).write_job_records)
        self.write(base + "write_job_records: true\n")
        self.assertTrue(load_global_config(self.path).write_job_records)
        self.write(base + "write_job_records: yes please\n")
        with self.assertRaisesRegex(ValueError, "write_job_records"):
            load_global_config(self.path)
