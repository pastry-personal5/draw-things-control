"""Shared helpers for job tests: temporary directories, configs, images, and job files."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from PIL import Image

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.core.global_config import GlobalConfig
from framecontrol.core.paths import ProjectPaths
from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.events import JobEvent, JobObserver, StageFinished, StageStarted, TimingFinished, TimingStarted
from framecontrol.jobs.executor import JobExecutor, JobOutcome, JobRunOptions
from framecontrol.jobs.media.info import MediaInfo
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.parsing import load_job, load_job_text
from framecontrol.services.toolkit import Toolkit
from framecontrol.state.executions import ExecutionRow, ExecutionSettings, RunRow

BASE_CONFIG = {"model": "base.ckpt", "refinerModel": "base-refiner.ckpt", "refinerStart": 0.2, "width": 832, "height": 448, "seed": 42, "steps": 30}


TIMING_EVENTS = (StageStarted, StageFinished, TimingStarted, TimingFinished)


def without_timing(observer: JobObserver) -> JobObserver:
    """An observer that hears every event but the timing ones, for a test that pins the order of the rest."""

    def observe(event: JobEvent) -> None:
        if not isinstance(event, TIMING_EVENTS):
            observer(event)

    return observe


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
        self.paths = ProjectPaths(self.root)
        self.params = self.paths.params
        self.input_directory.mkdir()
        self.params.mkdir(parents=True)
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
        """Write a job file in the format the product accepts: a version-1 mapping, which tests still build with
        ``job_data``, is written as its version-2 equivalent (``to_version_2``, below)."""
        return self.write_legacy_job(to_version_2(data, self.params), name)

    def write_legacy_job(self, data: dict[str, Any], name: str = "job.yaml") -> Path:
        """Write ``data`` exactly as given, for a test of the version-1 engine or of a job queued before the cutover."""
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


def job_executor(*, runner_factory: Callable[..., Any], find_executable: Callable[[str], str | None], frame_extractor: Callable[[Path, Path], None], require_ffmpeg: Callable[[], object], video_tagger: Callable[[Path], bool] | None = None, require_ffprobe: Callable[[], object] | None = None, output_measurer: Callable[[Path], MediaInfo] | None = None, checker: Any = None, corrector: Any = None, **options: Any) -> TestExecutor:
    """A JobExecutor built from the tools a test gives, with its ``knobs`` to change them later."""
    knobs = Knobs(runner_factory, find_executable, frame_extractor, require_ffmpeg)
    knobs.video_tagger, knobs.require_ffprobe, knobs.output_measurer = video_tagger, require_ffprobe, output_measurer

    # The tools take the color the stream states; the tests' fakes do not need it.
    def tag(video: Path, _color: object) -> bool:
        return knobs.video_tagger(video) if knobs.video_tagger is not None else False

    def probe() -> object:
        return knobs.require_ffprobe() if knobs.require_ffprobe is not None else None

    def measure(output: Path) -> MediaInfo:
        return knobs.output_measurer(output) if knobs.output_measurer is not None else MediaInfo(None, None, None)

    media = MediaTools(require_ffmpeg=lambda: knobs.require_ffmpeg(), frame_extractor=lambda video, png, _color: knobs.frame_extractor(video, png), require_ffprobe=probe, video_tagger=tag, output_measurer=measure, checker=checker, corrector=corrector)
    executor = TestExecutor(lambda *args: knobs.runner_factory(*args), lambda name: knobs.find_executable(name), media, **options)
    executor.knobs = knobs
    return executor


def run_job_with(executor: JobExecutor, job: JobDefinition, **options: Any) -> JobOutcome:
    """Run ``job`` with the options as keywords (executable, shutdown_grace, write_records, observer, ...)."""
    return executor.run(job, JobRunOptions(**options))


def run_row(**changes: Any) -> RunRow:
    """A stored run, without the database: keyword arguments replace the defaults."""
    base = RunRow(number=1, pair="walk", positive="text", negative=None, input=None, resized_input=None, output=None, last_frame=None, command=(), started_at="2026-01-01T00:00:00+00:00", seconds=None, exit_code=None, status="succeeded", cooldown_after_seconds=None, output_width=None, output_height=None, output_frames=None)
    return replace(base, **changes)


def execution_row(**changes: Any) -> ExecutionRow:
    """A stored execution, without the database: keyword arguments replace the defaults."""
    base = ExecutionRow(
        id=1,
        execution_number=1,
        job_name="walk",
        job_file="/jobs/walk.yaml",
        mode="i2v",
        status="succeeded",
        model=None,
        seed=None,
        seed_source=None,
        cooldown_seconds=None,
        cooldown_source=None,
        total_runs=None,
        started_at="2026-01-01T00:00:00+00:00",
        started_epoch=0.0,
        finished_at=None,
        finished_epoch=None,
        exit_code=None,
        signal=None,
        manifest_path=None,
        log_path=None,
        config_file=None,
        job_yaml=None,
        settings=ExecutionSettings(),
        recovered_at=None,
    )
    return replace(base, **changes)


class FakeToolkit(Toolkit):
    """A Toolkit whose job executor is the one a test built; without one, the real one."""

    def __init__(self, executor: JobExecutor | None = None) -> None:
        super().__init__()
        self._executor = executor

    def job_executor(self, *, handle_signals: bool = True) -> JobExecutor:
        return self._executor or super().job_executor(handle_signals=handle_signals)


# Version-1 job mappings, and their version-2 equivalents.
#
# Version 1 is no longer an authoring format: every live entry point refuses it. The tests still build jobs as version-1
# mappings (``job_data``), for two reasons. The version-1 engine still runs jobs queued before the cutover, and a test of
# it loads the mapping with ``allow_version_1=True`` (``load_legacy_job``, ``write_legacy_job``, ``submit_legacy_job``). A
# test of something else, such as the queue, the HTTP API, or a front end, does not care which format its job is in, and
# gets the equivalent version-2 job from ``to_version_2`` (``JobTestCase.write_job`` applies it).

DEFAULT_TIMEOUT_SECONDS = 3600


def load_legacy_job(path: Path, global_config: GlobalConfig, params_directory: Path, **options: Any) -> JobDefinition:
    """``load_job`` for a version-1 file: what a job queued before the cutover is read with."""
    return load_job(path, global_config, params_directory, allow_version_1=True, **options)


def load_legacy_job_text(text: str, path: Path, global_config: GlobalConfig, params_directory: Path, **options: Any) -> JobDefinition:
    return load_job_text(text, path, global_config, params_directory, allow_version_1=True, **options)


def to_version_2(data: dict[str, Any], params_directory: Path) -> dict[str, Any]:
    """The version-2 job that does what the version-1 mapping ``data`` does; a version-2 mapping is returned as it is.

    Covers what the tests write: Draw Things ``i2i``, ``t2v``, and ``i2v`` jobs, and H3 ``i2av`` and ``h3av`` jobs. A
    version-1 ``t2v`` job's later runs continue from the previous run's last frame, so they are ``i2v`` runs.
    """
    if data.get("version") == 2:
        return data
    pairs = data["prompt_pairs"]
    count = data["run_count"]
    assigned = {number: pair["name"] for pair in pairs for number in pair.get("runs", [])}
    default = next((pair["name"] for pair in pairs if pair.get("default")), pairs[0]["name"] if len(pairs) == 1 else None)
    schedule: list[str] = []
    for number in range(1, count + 1):
        chosen = assigned.get(number, default)
        if chosen is None:
            raise ValueError(f"the version-1 mapping gives run {number} no prompt pair, which a version-2 job cannot express; write the invalid job as version 2")
        schedule.append(chosen)
    used = set(schedule)
    catalog = {pair["name"]: {key: pair[key] for key in ("positive", "negative") if pair.get(key) is not None} for pair in pairs if pair["name"] in used}
    timeout = data.get("run_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    backend = data.get("backend", "draw-things-cli")
    job: dict[str, Any] = {"version": 2, "name": data["name"], "prompt_pairs": catalog}
    if data.get("cooldown") is not None:
        job["cooldown"] = data["cooldown"]
    output = data.get("output") or {}
    if "directory" in output:
        job["output"] = {"directory": output["directory"]}
    if backend == "h3-metal-cli":
        _h3(data, job, schedule, timeout)
    else:
        _draw_things(data, job, schedule, timeout, params_directory)
    first = job["runs"][0]
    if (first["backend"], first["pipeline"]) not in (("h3-metal-cli", "r2av"), ("draw-things-cli", "t2v")) and data.get("input") is not None:
        job["input"] = data["input"]
    return job


def _draw_things(data: dict[str, Any], job: dict[str, Any], schedule: list[str], timeout: float, params_directory: Path) -> None:
    mode = data["mode"]
    override = dict(data.get("config_override") or {})
    settings: dict[str, Any] = {"config_file": data["config_file"]}
    frames = override.pop("frame_count", None)
    seed = override.pop("seed", None)
    if seed is None:
        seed = _base_seed(params_directory / data["config_file"])
    if override:
        settings["config_override"] = override
    output = data.get("output") or {}
    profile_output = {key: output[key] for key in ("extension", "video_format") if key in output}
    if profile_output:
        settings["output"] = profile_output
    resize = {legacy: data[key] for key, legacy in (("desired_input_width", "desired_width"), ("desired_input_height", "desired_height"), ("max_input_crop_percent", "max_crop_percent")) if key in data}
    if resize:
        settings["input_resize"] = resize
    if data.get("color") is not None:
        settings["color"] = data["color"]
    job["settings_profiles"] = {"main": {"backend": "draw-things-cli", "settings": settings}}
    runs = []
    for number, pair in enumerate(schedule, start=1):
        pipeline = "i2v" if mode == "t2v" and number > 1 else mode
        knobs: dict[str, Any] = {"timeout_seconds": timeout}
        if seed is not None:
            knobs["seed"] = seed
        if frames is not None and pipeline != "i2i":
            knobs["frames"] = frames
        runs.append({"backend": "draw-things-cli", "pipeline": pipeline, "prompt_pair": pair, "settings_profile": "main", "knobs": knobs})
    job["runs"] = runs


def _base_seed(config: Path) -> int | None:
    try:
        seed = yaml.safe_load(config.read_text(encoding="utf-8")).get("seed")
    except (OSError, AttributeError, yaml.YAMLError):
        return None
    return seed if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0 else None


def _h3(data: dict[str, Any], job: dict[str, Any], schedule: list[str], timeout: float) -> None:
    settings = dict(data["backend_settings"])
    seconds, seed = settings.pop("seconds"), settings.pop("seed")
    settings.pop("frames", None)
    profile: dict[str, Any] = dict(settings)
    if data.get("color") is not None:
        profile["color"] = data["color"]
    job["settings_profiles"] = {"main": {"backend": "h3-metal-cli", "settings": profile}}
    h3_runs = data.get("h3_runs") or [{"run": number, "pipeline": "i2av"} for number in range(1, data["run_count"] + 1)]
    if data.get("reference_pairs"):
        job["reference_pairs"] = {pair["name"]: {"references": pair["references"]} for pair in data["reference_pairs"]}
    runs = []
    for item, pair in zip(h3_runs, schedule, strict=True):
        knobs: dict[str, Any] = {"seconds": seconds, "seed": seed, "timeout_seconds": timeout}
        if item.get("reference_pair") is not None:
            knobs["reference_pair"] = item["reference_pair"]
        runs.append({"backend": "h3-metal-cli", "pipeline": item["pipeline"], "prompt_pair": pair, "settings_profile": "main", "knobs": knobs})
    job["runs"] = runs


def submit_legacy_job(job_path: Path, global_config: GlobalConfig, params_directory: Path, store: Any, **options: Any) -> Any:
    """The queue entry a version-1 job file made before the cutover: ``submit_job``, with its parse allowing version 1.

    Production ``submit_job`` refuses version 1 (it never passes ``allow_version_1``); a test of what the queue does
    with an entry queued earlier, such as its snapshot or its resume, needs one, and this builds it with the real
    submission code rather than a copy of it.
    """
    from unittest import mock

    from framecontrol.services import queue_submit

    with mock.patch.object(queue_submit, "load_job_text", load_legacy_job_text):
        return queue_submit.submit_job(job_path, global_config, params_directory, store, **options)
