"""Tests for loading the global configuration."""

import tempfile
import unittest
from pathlib import Path

from draw_things_control.core.global_config import load_global_config


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

    def test_cooldown_seconds_is_optional_and_in_range(self) -> None:
        base = f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n"
        self.write(base)
        self.assertIsNone(load_global_config(self.path).cooldown_seconds)
        for text, expected in (("900", 900.0), ("0", 0.0), ("3600", 3600.0), ("0.5", 0.5)):
            with self.subTest(text):
                self.write(base + f"cooldown_seconds: {text}\n")
                self.assertEqual(load_global_config(self.path).cooldown_seconds, expected)
        for text in ("-1", "3600.5", ".nan", ".inf", "true", "15 min"):
            with self.subTest(text):
                self.write(base + f"cooldown_seconds: {text}\n")
                with self.assertRaisesRegex(ValueError, r"'cooldown_seconds' must be a number of seconds from 0 to 3600"):
                    load_global_config(self.path)

    def test_history_retention_days_defaults_to_14_and_is_a_whole_number_from_0_to_3650(self) -> None:
        base = f"version: 1\ninput_directory: {self.root / 'input'}\noutput_directory: /tmp/out\n"
        self.write(base)
        self.assertEqual(load_global_config(self.path).history_retention_days, 14)
        for text, expected in (("0", 0), ("14", 14), ("3650", 3650)):
            with self.subTest(text):
                self.write(base + f"history_retention_days: {text}\n")
                self.assertEqual(load_global_config(self.path).history_retention_days, expected)
        for text in ("-1", "3651", "1.5", "true", "forever", "null"):
            with self.subTest(text):
                self.write(base + f"history_retention_days: {text}\n")
                with self.assertRaisesRegex(ValueError, rf"{self.path}: 'history_retention_days' must be a whole number of days from 0 to 3650"):
                    load_global_config(self.path)
