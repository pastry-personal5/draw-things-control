"""Shared helpers for job tests: temporary directories, configs, images, and job files."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from draw_things_control.core.cooldown import CooldownPolicy
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.executor import JobExecutor, JobOutcome, JobRunOptions
from draw_things_control.jobs.media.info import MediaInfo
from draw_things_control.jobs.media.toolkit import MediaTools

BASE_CONFIG = {"model": "base.ckpt", "refinerModel": "base-refiner.ckpt", "refinerStart": 0.2, "width": 832, "height": 448, "seed": 42, "steps": 30}


def job_data(**changes: Any) -> dict[str, Any]:
    """A valid i2v job; keyword arguments replace or (with None) remove keys."""
    data: dict[str, Any] = {
        "version": 1,
        "name": "sunset-walk",
        "mode": "i2v",
        "input": "first-frame.png",
        "run_count": 5,
        "prompt_pairs": [
            {"name": "walk", "positive": "walk", "negative": "blurry", "runs": [1, 3, 5]},
            {"name": "wave", "positive": "wave", "runs": [2, 4]},
        ],
        "config_file": "base.yaml",
    }
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class JobTestCase(unittest.TestCase):
    """Creates input, output, and params directories under a temporary root."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.input_directory = self.root / "input"
        self.output_directory = self.root / "output"
        self.params = self.root / "params"
        self.input_directory.mkdir()
        self.params.mkdir()
        # No cooldown unless a test sets one: the default auto cooldown would follow a fake run with a wait of 0 or 1 second, by chance.
        self.global_config = GlobalConfig(input_directory=self.input_directory, output_directory=self.output_directory, cooldown=CooldownPolicy(mode="off"))
        self.write_base_config(BASE_CONFIG)
        self.write_image("first-frame.png", (832, 448))

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write_base_config(self, config: dict[str, Any], name: str = "base.yaml") -> Path:
        path = self.params / name
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
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


class Knobs:
    """The collaborators of a test executor, which a test can replace after the executor is built."""

    def __init__(self, runner_factory: Callable[..., Any], find_executable: Callable[[str], str | None], frame_extractor: Callable[[Path, Path], None], require_ffmpeg: Callable[[], object]) -> None:
        self.runner_factory = runner_factory
        self.find_executable = find_executable
        self.frame_extractor = frame_extractor
        self.require_ffmpeg = require_ffmpeg
        self.require_ffprobe: Callable[[], object] | None = None
        self.video_tagger: Callable[[Path], bool] | None = None
        self.output_measurer: Callable[[Path], MediaInfo] | None = None


class TestExecutor(JobExecutor):
    """A JobExecutor whose tools are ``knobs``, so a test can swap one (``executor.knobs.find_executable = ...``) between runs."""

    __test__ = False
    knobs: Knobs


def job_executor(*, runner_factory: Callable[..., Any], find_executable: Callable[[str], str | None], frame_extractor: Callable[[Path, Path], None], require_ffmpeg: Callable[[], object], video_tagger: Callable[[Path], bool] | None = None, require_ffprobe: Callable[[], object] | None = None, output_measurer: Callable[[Path], MediaInfo] | None = None, **options: Any) -> TestExecutor:
    """A JobExecutor built from the tools a test gives, with its ``knobs`` to change them later."""
    knobs = Knobs(runner_factory, find_executable, frame_extractor, require_ffmpeg)
    knobs.video_tagger, knobs.require_ffprobe, knobs.output_measurer = video_tagger, require_ffprobe, output_measurer

    def tag(video: Path) -> bool:
        return knobs.video_tagger(video) if knobs.video_tagger is not None else False

    def probe() -> object:
        return knobs.require_ffprobe() if knobs.require_ffprobe is not None else None

    def measure(output: Path) -> MediaInfo:
        return knobs.output_measurer(output) if knobs.output_measurer is not None else MediaInfo(None, None, None)

    media = MediaTools(require_ffmpeg=lambda: knobs.require_ffmpeg(), frame_extractor=lambda video, png: knobs.frame_extractor(video, png), require_ffprobe=probe, video_tagger=tag, output_measurer=measure)
    executor = TestExecutor(lambda *args: knobs.runner_factory(*args), lambda name: knobs.find_executable(name), media, **options)
    executor.knobs = knobs
    return executor


def run_job_with(executor: JobExecutor, job: JobDefinition, **options: Any) -> JobOutcome:
    """Run ``job`` with the options as keywords (executable, shutdown_grace, write_records, observer, ...)."""
    return executor.run(job, JobRunOptions(**options))
