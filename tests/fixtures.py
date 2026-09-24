"""Shared helpers for job tests: temporary directories, configs, images, and job files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from draw_things_control.core.global_config import GlobalConfig

BASE_CONFIG = {"model": "base.ckpt", "refinerModel": "base-refiner.ckpt", "refinerStart": 0.2, "width": 832, "height": 448, "seed": 42, "steps": 30}


def job_data(**changes: Any) -> dict[str, Any]:
    """A valid i2v job; keyword arguments replace or (with None) remove keys."""
    data: dict[str, Any] = {
        "version": 1,
        "name": "sunset-walk",
        "mode": "i2v",
        "input": "first-frame.png",
        "batch_count": 5,
        "prompt_pairs": [
            {"name": "walk", "positive": "walk", "negative": "blurry", "batches": [1, 3, 5]},
            {"name": "wave", "positive": "wave", "batches": [2, 4]},
        ],
        "config_file": "base.json",
    }
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class JobTestCase(unittest.TestCase):
    """Creates input, output, and dt-config directories under a temporary root."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.input_directory = self.root / "input"
        self.output_directory = self.root / "output"
        self.dt_config = self.root / "dt-config"
        self.input_directory.mkdir()
        self.dt_config.mkdir()
        self.global_config = GlobalConfig(input_directory=self.input_directory, output_directory=self.output_directory)
        self.write_base_config(BASE_CONFIG)
        self.write_image("first-frame.png", (832, 448))

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write_base_config(self, config: dict[str, Any], name: str = "base.json") -> Path:
        path = self.dt_config / name
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def write_image(self, name: str, size: tuple[int, int], orientation: int | None = None) -> Path:
        path = self.input_directory / name
        image = Image.new("RGB", size, "orange")
        if orientation is None:
            image.save(path)
        else:
            exif = Image.Exif()
            exif[0x0112] = orientation
            image.save(path, exif=exif)
        return path

    def write_job(self, data: dict[str, Any], name: str = "job.yaml") -> Path:
        path = self.root / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path
