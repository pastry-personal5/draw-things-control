"""Tests for finding base configurations and applying overrides."""

import tempfile
import unittest
from pathlib import Path

from draw_things_control.core.generation_config import DT_CONFIG_DIRECTORY, build_config_json, find_config_file


class GenerationConfigTests(unittest.TestCase):
    def test_config_directory_is_the_repository_folder(self) -> None:
        self.assertEqual(DT_CONFIG_DIRECTORY, Path(__file__).resolve().parents[2] / "dt-config")

    def test_bare_name_is_found_in_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "base.json").write_text("{}", encoding="utf-8")
            self.assertEqual(find_config_file("base.json", Path(directory)), Path(directory) / "base.json")

    def test_paths_are_rejected(self) -> None:
        for name in ("../pyproject.toml", "dt-config/x.json", "/etc/hosts", ".."):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "not a path"):
                find_config_file(name)

    def test_missing_file_lists_available_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "one.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"available: one.json"):
                find_config_file("two.json", Path(directory))

    def test_named_config_keys_override_the_base(self) -> None:
        base = {"refinerModel": "base.ckpt", "refinerStart": 0.2, "shift": 1.0, "steps": 30}
        config = build_config_json(base, {"refiner_model": "job.ckpt", "refiner_start": 0.1, "shift": 3.99, "steps": 40})
        self.assertEqual(config, {"refinerModel": "job.ckpt", "refinerStart": 0.1, "shift": 3.99, "steps": 30})
        self.assertEqual(base["refinerModel"], "base.ckpt")
