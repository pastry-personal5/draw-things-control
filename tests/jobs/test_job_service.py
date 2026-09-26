"""Tests for running jobs, with fake runners instead of draw-things-cli and ffmpeg."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from loguru import logger
from PIL import Image

from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.core.draw_things_runner import install_signal_handlers, restore_signal_handlers
from draw_things_control.core.generation_service import GenerationService
from draw_things_control.core.global_config import CooldownPolicy
from draw_things_control.core.process_output import OutputStream, ProcessMessage
from draw_things_control.jobs import job_service
from draw_things_control.jobs.job_definition import JobDefinition, load_job
from draw_things_control.jobs.job_events import CooldownEnded, CooldownStarted, JobFinished, JobStarted, RunFinished, RunOutput, RunStarted, combine_observers
from draw_things_control.jobs.job_service import JobService
from draw_things_control.jobs.media_info import MediaInfo
from tests.fixtures import JobTestCase, job_data

NOW = datetime(2026, 9, 24, 15, 30, 12)


@dataclass(frozen=True)
class FakeResult:
    return_code: int = 0
    timed_out: bool = False
    termination_signal: signal.Signals | None = None


class FakeRunner:
    def __init__(self, arguments: DrawThingsGenerateArguments, result: FakeResult, write_output: bool) -> None:
        self.arguments = arguments
        self.result = result
        self.write_output = write_output
        self.shutdown_signal: signal.Signals | None = None

    def run(self) -> FakeResult:
        if self.write_output:
            assert self.arguments.output is not None
            self.arguments.output.write_bytes(b"video")
        return self.result

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        self.shutdown_signal = received_signal


class TalkingRunner(FakeRunner):
    """A runner that reports child lines through its on_message callback, as the process runner does."""

    def __init__(self, arguments: DrawThingsGenerateArguments, on_message: object, lines: tuple[tuple[OutputStream, str], ...]) -> None:
        super().__init__(arguments, FakeResult(), write_output=True)
        self.on_message = on_message
        self.lines = lines

    def run(self) -> FakeResult:
        for stream, text in self.lines:
            self.on_message(ProcessMessage(stream=stream, text=text, elapsed_seconds=0.5, progress=(3, 8) if "3/8" in text else None))
        return super().run()


class BlockingRunner(FakeRunner):
    """A runner that runs until it is asked to shut down, like a long generation."""

    def __init__(self, arguments: DrawThingsGenerateArguments) -> None:
        super().__init__(arguments, FakeResult(), write_output=True)
        self.stopped = threading.Event()

    def run(self) -> FakeResult:
        assert self.stopped.wait(5), "the job never asked the runner to stop"
        super().run()
        return FakeResult(return_code=-15, termination_signal=self.shutdown_signal)

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        super().request_shutdown(received_signal)
        self.stopped.set()


class JobServiceTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple[DrawThingsGenerateArguments, float | None, float]] = []
        self.callbacks: list[object] = []
        self.results: dict[int, FakeResult] = {}
        self.missing_output: set[int] = set()
        self.extracted: list[tuple[Path, Path]] = []
        numbers = itertools.count(1000)
        self.service = JobService(
            runner_factory=self.create_runner,
            find_executable=lambda executable: executable,
            frame_extractor=self.extract,
            require_ffmpeg=lambda: "ffmpeg",
            clock=lambda: NOW,
            random_number=lambda: next(numbers),
            random_seed=lambda: 777,
            handle_signals=False,
            cooldown=lambda seconds: self.cooldown(seconds),
        )
        # Replaced by fake_cooldown; a job without a cooldown never calls it.
        self.cooldown = lambda seconds: seconds

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
        self.calls.append((arguments, timeout, grace))
        self.callbacks.append(on_message)
        number = len(self.calls)
        return FakeRunner(arguments, self.results.get(number, FakeResult()), write_output=number not in self.missing_output)

    def extract(self, video: Path, png: Path) -> None:
        self.extracted.append((video, png))
        png.write_bytes(b"png")

    def job(self, **changes: object) -> JobDefinition:
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.dt_config)

    def run_job(self, job: JobDefinition, write_records: bool = True):
        return self.service.run(job, executable="draw-things-cli", shutdown_grace=2, write_records=write_records)

    def manifest(self, outcome) -> dict:
        return json.loads(outcome.manifest.read_text(encoding="utf-8"))

    def test_i2v_runs_chain_last_frames_in_run_order(self) -> None:
        job = self.job(run_count=4, prompt_pairs=[{"name": "walk", "positive": "walk", "runs": [1, 3]}, {"name": "wave", "positive": "wave", "runs": [2, 4]}], run_timeout_seconds=60)
        outcome = self.run_job(job)
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 4))
        prompts = [arguments.prompt for arguments, _timeout, _grace in self.calls]
        self.assertEqual(prompts, ["walk", "wave", "walk", "wave"])
        self.assertEqual(self.calls[0][0].image, job.input)
        for (previous, _t, _g), (current, _t2, _g2), (_video, frame) in zip(self.calls, self.calls[1:], self.extracted, strict=False):
            self.assertEqual(current.image, frame)
            self.assertEqual(frame.name, previous.output.stem + "-last-frame.png")
        self.assertEqual({(timeout, grace) for _a, timeout, grace in self.calls}, {(60.0, 2)})
        names = sorted(path.name for path in job.output_directory.iterdir())
        self.assertEqual(len([name for name in names if name.endswith(".mov")]), 4)
        self.assertEqual(len([name for name in names if name.endswith("-last-frame.png")]), 4)
        self.assertTrue(all(name.startswith("sunset-walk-20260924-153012-") for name in names))
        manifest = self.manifest(outcome)
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual([(index, run["pair"], run["status"]) for index, run in enumerate(manifest["runs"], 1)], [(1, "walk", "succeeded"), (2, "wave", "succeeded"), (3, "walk", "succeeded"), (4, "wave", "succeeded")])
        self.assertIn("Run 4/4 (pair wave)", outcome.log.read_text(encoding="utf-8"))
        self.assertEqual(manifest["log_file"], outcome.log.name)

    def test_t2v_first_run_has_no_image(self) -> None:
        self.run_job(self.job(mode="t2v", input=None, run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertIsNone(self.calls[0][0].image)
        self.assertEqual(self.calls[1][0].image, self.extracted[0][1])

    def test_i2i_chains_png_outputs_without_extraction(self) -> None:
        self.run_job(self.job(mode="i2i", run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertEqual(self.calls[0][0].output.suffix, ".png")
        self.assertEqual(self.calls[1][0].image, self.calls[0][0].output)
        self.assertEqual(self.extracted, [])

    def test_arguments_carry_overrides_and_config_json(self) -> None:
        self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text", "negative": "blur"}], config_override={"steps": 40, "guidance_scale": 5.0, "refiner_model": "job-refiner.ckpt", "refiner_start": 0.1, "shift": 3.99}))
        arguments = self.calls[0][0]
        self.assertEqual((arguments.steps, arguments.cfg, arguments.seed, arguments.negative_prompt), (40, 5.0, 42, "blur"))
        config = json.loads(arguments.config_json or "{}")
        self.assertEqual((config["refinerModel"], config["refinerStart"], config["shift"], config["model"]), ("job-refiner.ckpt", 0.1, 3.99, "base.ckpt"))
        self.assertNotIn("--config-file", arguments.command)

    def test_random_seed_is_drawn_once_and_recorded(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.yaml")
        outcome = self.run_job(self.job(config_file="noseed.yaml"))
        self.assertEqual({arguments.seed for arguments, _t, _g in self.calls}, {777})
        manifest = self.manifest(outcome)
        self.assertEqual((manifest["seed"], manifest["seed_source"]), (777, "random"))

    def test_failed_run_stops_the_job_with_its_exit_code(self) -> None:
        self.results[2] = FakeResult(return_code=3)
        self.missing_output.add(2)
        outcome = self.run_job(self.job())
        self.assertEqual((outcome.exit_code, outcome.completed_runs, len(self.calls)), (3, 1, 2))
        manifest = self.manifest(outcome)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual([(run["status"], run["exit_code"], run["output"]) for run in manifest["runs"]][1], ("failed", 3, None))

    def test_exit_zero_without_output_fails_with_code_1(self) -> None:
        self.missing_output.add(1)
        outcome = self.run_job(self.job())
        self.assertEqual((outcome.exit_code, len(self.calls)), (1, 1))
        self.assertEqual(self.manifest(outcome)["runs"][0]["status"], "failed")

    def test_interrupted_run_keeps_partial_output(self) -> None:
        self.results[1] = FakeResult(return_code=0, termination_signal=signal.SIGINT)
        outcome = self.run_job(self.job())
        self.assertEqual((outcome.exit_code, len(self.calls)), (130, 1))
        manifest = self.manifest(outcome)
        self.assertEqual((manifest["status"], manifest["runs"][0]["status"]), ("interrupted", "interrupted"))
        self.assertTrue((self.calls[0][0].output).exists())
        self.assertEqual(manifest["runs"][0]["output"], self.calls[0][0].output.name)
        self.assertIn("partial output kept", outcome.log.read_text(encoding="utf-8"))

    def test_timed_out_run_is_recorded(self) -> None:
        self.results[1] = FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM)
        outcome = self.run_job(self.job())
        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(self.manifest(outcome)["runs"][0]["status"], "timed_out")

    def test_signal_between_runs_stops_before_the_next_run(self) -> None:
        def extract_then_interrupt(video: Path, png: Path) -> None:
            self.extract(video, png)
            self.service._interrupt = signal.SIGTERM

        self.service._frame_extractor = extract_then_interrupt
        outcome = self.run_job(self.job())
        self.assertEqual((outcome.exit_code, outcome.completed_runs, len(self.calls)), (143, 1, 1))
        self.assertEqual(self.manifest(outcome)["status"], "interrupted")

    def test_preview_writes_nothing_and_predicts_the_chain(self) -> None:
        job = self.job()
        preview = self.service.preview(job, executable="draw-things-cli")
        self.assertEqual(len(preview.runs), 5)
        self.assertEqual(len(preview.command_previews), 5)
        for previous, current in zip(preview.runs, preview.runs[1:], strict=False):
            self.assertEqual(current.input, previous.last_frame)
        self.assertEqual(len({run.output for run in preview.runs}), 5)
        self.assertFalse(job.output_directory.exists())
        self.assertEqual(self.calls, [])

    def test_a_yaml_base_configuration_plans_the_config_json_of_the_equal_json_file(self) -> None:
        text = "# Wan 2.2\nmodel: base.ckpt\nrefinerModel: base-refiner.ckpt\nrefinerStart: 0.2\nwidth: 832\nheight: 448\nseed: 42\nsteps: 30\nshift: 3.99\nfaceRestoration: ''\ncolorCalibration: none\ncontrols: []\nloras: [{file: l.ckpt, weight: 0.6}]\nhiresFix: false\n"
        (self.dt_config / "wan.yaml").write_text(text, encoding="utf-8")
        json_config = {"model": "base.ckpt", "refinerModel": "base-refiner.ckpt", "refinerStart": 0.2, "width": 832, "height": 448, "seed": 42, "steps": 30, "shift": 3.99, "faceRestoration": "", "colorCalibration": "none", "controls": [], "loras": [{"file": "l.ckpt", "weight": 0.6}], "hiresFix": False}
        (self.dt_config / "wan.json").write_text(json.dumps(json_config, indent=2), encoding="utf-8")
        yaml_job = self.job(config_file="wan.yaml", config_override={"steps": 8, "shift": 5.0})
        # Before YAML, a job's base_config was the JSON file's object; the plan must not change.
        json_job = replace(yaml_job, base_config=json.loads((self.dt_config / "wan.json").read_text(encoding="utf-8")))
        yaml_plan, json_plan = (self.service.preview(job, executable="draw-things-cli", seed=1) for job in (yaml_job, json_job))
        self.assertEqual([run.arguments.config_json for run in yaml_plan.runs], [run.arguments.config_json for run in json_plan.runs])
        self.assertIn('"loras":[{"file":"l.ckpt","weight":0.6}]', yaml_plan.runs[0].arguments.config_json)
        self.assertNotIn("--config-file", yaml_plan.runs[0].arguments.command)

    def test_missing_tools_fail_before_anything_runs(self) -> None:
        self.service._find_executable = lambda _executable: None
        with self.assertRaisesRegex(ValueError, "Could not find 'draw-things-cli'"):
            self.run_job(self.job())

        def no_ffmpeg() -> str:
            raise ValueError("Could not find 'ffmpeg'")

        self.service._find_executable = lambda executable: executable
        self.service._require_ffmpeg = no_ffmpeg
        with self.assertRaisesRegex(ValueError, "ffmpeg"):
            self.run_job(self.job())
        with self.assertRaisesRegex(ValueError, "ffmpeg"):
            self.service.preview(self.job(), executable="draw-things-cli")
        self.service.preview(self.job(mode="i2i"), executable="draw-things-cli")
        self.assertEqual(self.calls, [])

    def test_a_video_job_without_ffprobe_fails_before_anything_runs(self) -> None:
        def no_ffprobe() -> str:
            raise ValueError("Could not find 'ffprobe' beside ffmpeg or on PATH")

        self.service._require_ffprobe = no_ffprobe
        with self.assertRaisesRegex(ValueError, "ffprobe"):
            self.run_job(self.job())
        with self.assertRaisesRegex(ValueError, "ffprobe"):
            self.service.preview(self.job(), executable="draw-things-cli")
        self.assertEqual(self.calls, [])
        # An image job measures its PNG from the file header, so it needs no ffprobe.
        self.assertEqual(self.run_job(self.job(mode="i2i", run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}])).exit_code, 0)

    def test_each_successful_run_records_what_its_output_file_holds(self) -> None:
        measured: list[Path] = []

        def measure(path: Path) -> MediaInfo:
            measured.append(path)
            # The file as the run left it: written, tagged, and its last frame extracted.
            self.assertTrue(path.is_file())
            self.assertEqual(len(self.extracted), len(measured))
            return MediaInfo(832, 448, 77 + len(measured))

        self.service._output_measurer = measure
        events: list[object] = []
        outcome = self.service.run(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "walk"}], cooldown={"mode": "off"}), executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append)
        finished = [event for event in events if isinstance(event, RunFinished)]
        self.assertEqual([(event.output_width, event.output_height, event.output_frames) for event in finished], [(832, 448, 78), (832, 448, 79)])
        self.assertEqual(measured, [arguments.output for arguments, _timeout, _grace in self.calls])
        runs = self.manifest(outcome)["runs"]
        self.assertEqual([(run["output_width"], run["output_height"], run["output_frames"]) for run in runs], [(832, 448, 78), (832, 448, 79)])
        # The run's time is draw-things-cli's alone; measuring is not in it.
        self.assertTrue(all(isinstance(run["seconds"], float) for run in runs))

    def test_a_file_that_cannot_be_measured_is_a_warning_and_failed_runs_are_not_measured(self) -> None:
        def cannot(path: Path) -> MediaInfo:
            raise ValueError("ffprobe exited with 1")

        warnings: list[str] = []
        sink = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
        self.addCleanup(logger.remove, sink)
        self.service._output_measurer = cannot
        outcome = self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "walk"}]))
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual([(run["output_width"], run["output_frames"]) for run in self.manifest(outcome)["runs"]], [(None, None)])
        self.assertTrue(any("Could not measure" in warning and "ffprobe exited with 1" in warning for warning in warnings), warnings)

        calls: list[Path] = []
        self.service._output_measurer = lambda path: calls.append(path) or MediaInfo(1, 1, 1)
        self.results[len(self.calls) + 1] = FakeResult(return_code=3)
        self.assertEqual(self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "walk"}])).exit_code, 3)
        self.assertEqual(calls, [])

    def test_run_that_cannot_start_is_marked_failed(self) -> None:
        def cannot_start(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            raise ValueError("Could not start executable draw-things-cli: Permission denied")

        self.service._runner_factory = cannot_start
        job = self.job()
        with self.assertRaisesRegex(ValueError, "Could not start"):
            self.run_job(job)
        (manifest_path,) = job.output_directory.glob("*.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["runs"][0]["status"]), ("failed", "failed"))

    def test_interrupt_during_setup_removes_the_job_log(self) -> None:
        def interrupted(_on_signal: object) -> None:
            raise KeyboardInterrupt

        self.service._handle_signals = True
        with mock.patch("draw_things_control.jobs.job_service.install_signal_handlers", interrupted), mock.patch("draw_things_control.jobs.job_service.remove_job_log", wraps=job_service.remove_job_log) as remove:
            with self.assertRaises(KeyboardInterrupt):
                self.run_job(self.job())
        remove.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_i2v_config_json_omits_run_count_and_the_log_says_so(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448, "batchCount": 3}, name="batch.yaml")
        outcome = self.run_job(self.job(config_file="batch.yaml", run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertNotIn("batchCount", json.loads(self.calls[0][0].config_json or "{}"))
        self.assertIn("Ignoring batchCount (3) from config_file batch.yaml", outcome.log.read_text(encoding="utf-8"))

    def test_records_are_off_by_default(self) -> None:
        job = self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])
        outcome = self.service.run(job, executable="draw-things-cli", shutdown_grace=2)
        self.assertEqual((outcome.exit_code, outcome.manifest, outcome.log), (0, None, None))
        names = sorted(path.name for path in job.output_directory.iterdir())
        self.assertEqual(len(names), 4)
        self.assertFalse([name for name in names if name.endswith(("-job.json", "-job.log"))])

    def test_failed_job_without_records_writes_no_manifest(self) -> None:
        self.results[1] = FakeResult(return_code=3)
        self.missing_output.add(1)
        job = self.job()
        outcome = self.run_job(job, write_records=False)
        self.assertEqual(outcome.exit_code, 3)
        self.assertEqual(list(job.output_directory.iterdir()), [])

    def resize_job(self, **changes: object) -> JobDefinition:
        self.write_image("photo.jpg", (1920, 1080))
        return self.job(input="photo.jpg", desired_input_width=850, **changes)

    def test_run_one_gets_the_resized_copy_and_it_is_removed_after_run_one(self) -> None:
        seen: list[tuple[int, int]] = []

        def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            # Look at the image while the run is happening, since the copy is gone afterwards.
            if not self.calls:
                with Image.open(arguments.image) as image:
                    seen.append(image.size)
            return self.create_runner(arguments, timeout, grace, on_message, on_start)

        self.service._runner_factory = create_runner
        job = self.resize_job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])
        outcome = self.run_job(job)
        self.assertEqual(outcome.exit_code, 0)
        first, second = self.calls[0][0], self.calls[1][0]
        self.assertEqual(seen, [(832, 448)])
        self.assertEqual(first.image.name, "photo-832x448.png")
        self.assertFalse(first.image.parent.exists())
        self.assertEqual(second.image, self.extracted[0][1])
        for arguments in (first, second):
            config = json.loads(arguments.config_json or "{}")
            self.assertEqual((arguments.width, arguments.height, config["width"], config["height"]), (832, 448, 832, 448))
        manifest = self.manifest(outcome)
        self.assertEqual(manifest["runs"][0]["input"], str(job.input))
        self.assertEqual(manifest["runs"][0]["resized_input"], str(first.image))
        self.assertIn(str(first.image), manifest["runs"][0]["command"])
        self.assertIsNone(manifest["runs"][1]["resized_input"])
        self.assertEqual(manifest["input_resize"]["fit"], "crop")
        self.assertEqual(manifest["input_resize"]["target_size"], [832, 448])
        log = outcome.log.read_text(encoding="utf-8")
        self.assertIn("will be scaled to 832x468 and cropped to 832x448 (4.3%)", log)
        self.assertIn("Run 1 input: temporary copy", log)

    def test_temporary_copy_is_removed_when_run_one_fails_or_is_interrupted(self) -> None:
        for result in (FakeResult(return_code=3), FakeResult(return_code=0, termination_signal=signal.SIGINT)):
            with self.subTest(result=result):
                self.calls.clear()
                self.results[1] = result
                self.run_job(self.resize_job())
                self.assertEqual(len(self.calls), 1)
                self.assertFalse(self.calls[0][0].image.parent.exists())

    def test_temporary_copy_is_removed_when_the_job_raises(self) -> None:
        def cannot_start(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            self.calls.append((arguments, timeout, grace))
            raise KeyboardInterrupt

        self.service._runner_factory = cannot_start
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(self.resize_job())
        self.assertFalse(self.calls[0][0].image.parent.exists())

    def test_upright_input_at_the_target_is_used_as_is(self) -> None:
        # The 832x448 input already has the calculated size, however the keys reach it (850 floors to 832).
        for keys in ({"desired_input_width": 832}, {"desired_input_width": 850}, {"desired_input_height": 470}, {"desired_input_width": 832, "desired_input_height": 448}, {"desired_input_width": 832, "max_input_crop_percent": 0}):
            with self.subTest(keys=keys):
                self.calls.clear()
                job = self.job(**keys)
                self.assertIsNone(job.input_copy)
                with mock.patch("draw_things_control.jobs.input_resize.resize_image") as resize, mock.patch("draw_things_control.jobs.input_resize.tempfile.mkdtemp") as mkdtemp:
                    outcome = self.run_job(job)
                resize.assert_not_called()
                mkdtemp.assert_not_called()
                self.assertEqual(self.calls[0][0].image, job.input)
                manifest = self.manifest(outcome)
                self.assertEqual((manifest["input_resize"]["fit"], manifest["input_resize"]["target_size"]), ("none", [832, 448]))
                self.assertIsNone(manifest["runs"][0]["resized_input"])
                self.assertIn("Input first-frame.png is already 832x448; no resize needed", outcome.log.read_text(encoding="utf-8"))
                preview = self.service.preview(job, executable="draw-things-cli")
                self.assertEqual(preview.runs[0].input, job.input)

    def test_rotated_input_at_the_target_gets_an_upright_copy(self) -> None:
        self.write_image("rotated.jpg", (448, 832), orientation=6)
        seen: list[tuple[int, int]] = []

        def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            if not self.calls:
                with Image.open(arguments.image) as image:
                    seen.append((image.size, image.getexif().get(0x0112)))
            return self.create_runner(arguments, timeout, grace, on_message, on_start)

        self.service._runner_factory = create_runner
        job = self.job(input="rotated.jpg", desired_input_width=832)
        self.run_job(job)
        self.assertNotEqual(self.calls[0][0].image, job.input)
        self.assertEqual(seen, [((832, 448), None)])

    def test_jobs_without_desired_keys_build_the_same_commands(self) -> None:
        outcome = self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        arguments = self.calls[0][0]
        self.assertEqual((arguments.width, arguments.height), (None, None))
        self.assertNotIn("--width", arguments.command)
        self.assertEqual(json.loads(arguments.config_json or "{}")["width"], 832)
        self.assertIsNone(self.manifest(outcome)["input_resize"])

    def test_resize_failure_leaves_no_output_directory_or_manifest(self) -> None:
        job = self.resize_job()
        with mock.patch("draw_things_control.jobs.input_resize.resize_image", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "Could not resize input"):
                self.run_job(job)
        self.assertFalse(job.output_directory.exists())
        self.assertEqual(self.calls, [])

    def test_preview_shows_a_placeholder_and_writes_nothing(self) -> None:
        job = self.resize_job()
        with mock.patch("draw_things_control.jobs.input_resize.resize_image") as resize:
            preview = self.service.preview(job, executable="draw-things-cli")
        resize.assert_not_called()
        command = preview.command_previews[0]
        self.assertIn("--image '<photo.jpg resized to 832x448>'", command)
        self.assertIn("--width 832 --height 448", command)
        self.assertIn("--width 832 --height 448", preview.command_previews[1])
        self.assertFalse(job.output_directory.exists())

    # Cooldown between runs.

    def cooldown_job(self, **changes: object) -> JobDefinition:
        return self.job(**{"run_count": 3, "prompt_pairs": [{"name": "only", "positive": "text"}], **changes})

    def fake_cooldown(self, interrupt_on: int | None = None, waited: float | None = None):
        """A wait that sleeps for no time; it records each wait and the manifest as saved when the wait starts."""
        waits: list[tuple[float, int, list]] = []

        def cooldown(seconds: float) -> float:
            manifest_path = next(self.output_directory.rglob("*-job.json"), None)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))["runs"] if manifest_path is not None else []
            waits.append((seconds, len(self.calls), [run["cooldown_after_seconds"] for run in saved]))
            if interrupt_on == len(waits):
                self.service._interrupt = signal.SIGINT
                return waited if waited is not None else seconds / 2
            return seconds

        self.cooldown = cooldown
        return waits

    def run_times(self, *seconds: float) -> None:
        """Make the job service measure each run, in order, as taking ``seconds``."""
        readings = iter([value for run in seconds for value in (0.0, run)])
        patcher = mock.patch.object(job_service, "time", mock.Mock(monotonic=lambda: next(readings)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cooldown_waits_between_runs_but_not_after_the_last(self) -> None:
        waits = self.fake_cooldown()
        for mode, changes in (("i2v", {}), ("i2i", {}), ("t2v", {"input": None})):
            with self.subTest(mode=mode):
                waits.clear()
                self.calls.clear()
                shutil.rmtree(self.output_directory, ignore_errors=True)
                outcome = self.run_job(self.cooldown_job(mode=mode, cooldown={"mode": "manual", "seconds": 900}, **changes))
                self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 3))
                # Each wait comes after one run and before the next, and starts with the manifest showing 0.
                self.assertEqual(waits, [(900.0, 1, [0.0]), (900.0, 2, [900.0, 0.0])])
                manifest = self.manifest(outcome)
                self.assertEqual((manifest["cooldown_seconds"], manifest["cooldown_source"], manifest["cooldown"]), (900.0, "job", {"mode": "manual", "seconds": 900.0}))
                self.assertEqual([run["cooldown_after_seconds"] for run in manifest["runs"]], [900.0, 900.0, None])
                log = outcome.log.read_text(encoding="utf-8")
                self.assertIn("cooldown 900 s (from job)", log)
                self.assertIn("Cooldown: waiting 900 s before run 2/3 (until 15:45:12)", log)
                self.assertIn("Cooldown finished; starting run 3/3", log)

    def test_global_cooldown_applies_and_a_job_can_turn_it_off(self) -> None:
        waits = self.fake_cooldown()
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="manual", seconds=60.0))
        outcome = self.run_job(self.cooldown_job())
        self.assertEqual([seconds for seconds, _runs, _saved in waits], [60.0, 60.0])
        self.assertEqual(self.manifest(outcome)["cooldown_source"], "global_config")
        for cooldown in ({"mode": "manual", "seconds": 0}, {"mode": "off"}):
            with self.subTest(cooldown=cooldown):
                waits.clear()
                outcome = self.run_job(self.cooldown_job(cooldown=cooldown))
                self.assertEqual(waits, [])
                log = outcome.log.read_text(encoding="utf-8")
                self.assertIn("no cooldown (from job)", log)
                self.assertNotIn("Cooldown", log)
                self.assertEqual([run["cooldown_after_seconds"] for run in self.manifest(outcome)["runs"]], [None, None, None])
        self.assertEqual((self.manifest(outcome)["cooldown_seconds"], self.manifest(outcome)["cooldown"]), (0.0, {"mode": "off"}))

    def test_auto_cooldown_waits_a_share_of_each_run_within_the_bounds(self) -> None:
        waits = self.fake_cooldown()
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="auto", minimum_seconds=300.0))
        self.run_times(1200.0, 200.0, 1201.0, 10000.0, 5.0)
        outcome = self.run_job(self.cooldown_job(run_count=5))
        self.assertEqual([seconds for seconds, _runs, _saved in waits], [600.0, 300.0, 601.0, 3600.0])
        manifest = self.manifest(outcome)
        self.assertEqual((manifest["cooldown_seconds"], manifest["cooldown_source"], manifest["cooldown"]), (None, "global_config", {"mode": "auto", "ratio": 0.5, "minimum_seconds": 300.0, "maximum_seconds": 3600.0}))
        self.assertEqual([run["seconds"] for run in manifest["runs"]], [1200.0, 200.0, 1201.0, 10000.0, 5.0])
        self.assertEqual([run["cooldown_after_seconds"] for run in manifest["runs"]], [600.0, 300.0, 601.0, 3600.0, None])
        log = outcome.log.read_text(encoding="utf-8")
        self.assertIn("cooldown auto, half of each run, 5 min to 1 h (from global_config)", log)
        self.assertIn("Cooldown: waiting 10 min, half of run 1's 20 min, before run 2/5 (until 15:40:12)", log)
        self.assertIn("Cooldown: waiting 5 min (the minimum; half of run 2's 3 min 20 s is less) before run 3/5 (until 15:35:12)", log)
        self.assertIn("Cooldown: waiting 1 h (the maximum; half of run 4's 2 h 46 min 40 s is more) before run 5/5 (until 16:30:12)", log)

    def test_auto_cooldown_with_another_ratio(self) -> None:
        waits = self.fake_cooldown()
        self.run_times(1200.0, 1200.0)
        outcome = self.run_job(self.cooldown_job(run_count=2, cooldown={"mode": "auto", "ratio": 0.25}))
        self.assertEqual([seconds for seconds, _runs, _saved in waits], [300.0])
        self.assertIn("Cooldown: waiting 5 min, 25% of run 1's 20 min, before run 2/2", outcome.log.read_text(encoding="utf-8"))

    def test_auto_cooldown_without_either_key(self) -> None:
        waits = self.fake_cooldown()
        self.global_config = replace(self.global_config, cooldown=None)
        self.run_times(90.0, 0.0, 30.0)
        outcome = self.run_job(self.cooldown_job())
        # A run measured as 0 s is followed by no wait.
        self.assertEqual([seconds for seconds, _runs, _saved in waits], [45.0])
        manifest = self.manifest(outcome)
        self.assertEqual((manifest["cooldown_seconds"], manifest["cooldown_source"], manifest["cooldown"]["mode"]), (None, "default", "auto"))
        self.assertEqual([run["cooldown_after_seconds"] for run in manifest["runs"]], [45.0, None, None])

    def test_no_cooldown_after_a_failed_or_timed_out_run(self) -> None:
        waits = self.fake_cooldown()
        for result in (FakeResult(return_code=3), FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM)):
            with self.subTest(result=result):
                self.results[1] = result
                for cooldown in ({"mode": "manual", "seconds": 900}, {"mode": "auto", "minimum_seconds": 60}):
                    self.calls.clear()
                    outcome = self.run_job(self.cooldown_job(cooldown=cooldown))
                    self.assertEqual((len(self.calls), waits), (1, []))
                    self.assertIsNone(self.manifest(outcome)["runs"][0]["cooldown_after_seconds"])

    def test_signal_during_a_cooldown_stops_the_job(self) -> None:
        waits = self.fake_cooldown(interrupt_on=1, waited=412.34)
        outcome = self.run_job(self.cooldown_job(cooldown={"mode": "manual", "seconds": 900}))
        self.assertEqual((outcome.exit_code, outcome.completed_runs, len(self.calls), len(waits)), (130, 1, 1, 1))
        manifest = self.manifest(outcome)
        self.assertEqual(manifest["status"], "interrupted")
        self.assertEqual([(run["status"], run["cooldown_after_seconds"]) for run in manifest["runs"]], [("succeeded", 412.3)])
        log = outcome.log.read_text(encoding="utf-8")
        self.assertIn("Job stopped by SIGINT during the cooldown before run 2/3 (waited 412.3 s of 900 s)", log)
        self.assertNotIn("Cooldown finished", log)

    def test_real_cooldown_waits_and_is_ended_by_a_signal(self) -> None:
        self.assertLess(self.service._wait_for_cooldown(0.05), 0.5)
        self.assertGreaterEqual(self.service._wait_for_cooldown(0.05), 0.05)
        service = JobService(runner_factory=self.create_runner, find_executable=lambda executable: executable, frame_extractor=self.extract, require_ffmpeg=lambda: "ffmpeg")
        open_fds = set(os.listdir("/dev/fd"))
        previous_fd = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(previous_fd)
        handlers = install_signal_handlers(service._handle_signal)
        timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
        try:
            timer.start()
            started = time.monotonic()
            waited = service._wait_for_cooldown(5)
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
            restore_signal_handlers(handlers)
        self.assertEqual(service._interrupt, signal.SIGINT)
        self.assertLess(elapsed, 1)
        self.assertAlmostEqual(waited, elapsed, delta=0.05)
        # The previous wake-up fd is back, and the pipe is closed.
        self.assertEqual(signal.set_wakeup_fd(previous_fd), previous_fd)
        self.assertEqual(set(os.listdir("/dev/fd")), open_fds)

    def test_signal_after_a_full_cooldown_stops_before_the_next_run(self) -> None:
        def full_wait_then_signal(seconds: float) -> float:
            self.service._interrupt = signal.SIGTERM
            return seconds

        self.cooldown = full_wait_then_signal
        outcome = self.run_job(self.cooldown_job(cooldown={"mode": "manual", "seconds": 900}))
        self.assertEqual((outcome.exit_code, len(self.calls)), (143, 1))
        self.assertEqual(self.manifest(outcome)["runs"][0]["cooldown_after_seconds"], 900.0)
        log = outcome.log.read_text(encoding="utf-8")
        self.assertIn("Job stopped by SIGTERM before run 2/3", log)
        self.assertNotIn("during the cooldown", log)

    def test_cooldown_end_time_is_local_like_the_other_timestamps(self) -> None:
        utc_now = datetime(2026, 9, 24, 6, 30, 12, tzinfo=timezone.utc)
        self.service._clock = lambda: utc_now
        outcome = self.run_job(self.cooldown_job(run_count=2, cooldown={"mode": "manual", "seconds": 90}))
        expected = (utc_now.astimezone() + timedelta(seconds=90)).strftime("%H:%M:%S")
        self.assertIn(f"Cooldown: waiting 90 s before run 2/2 (until {expected})", outcome.log.read_text(encoding="utf-8"))

    # Events and cancellation.

    def observed(self, job: JobDefinition) -> tuple:
        events: list = []
        outcome = self.service.run(job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append)
        return outcome, events

    def start_in_thread(self, job: JobDefinition) -> tuple[threading.Thread, dict]:
        """Run the job on a worker thread, as the TUI does, collecting its events in self.events."""
        self.events: list = []
        result: dict = {}

        def work() -> None:
            try:
                result["outcome"] = self.service.run(job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=self.events.append)
            except BaseException as error:
                result["error"] = error

        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, result

    def wait_for_event(self, kind: type) -> None:
        deadline = time.monotonic() + 5
        while not any(isinstance(event, kind) for event in list(self.events)):
            self.assertLess(time.monotonic(), deadline, f"no {kind.__name__} event")
            time.sleep(0.01)

    def test_events_for_a_three_run_job_with_a_cooldown(self) -> None:
        self.fake_cooldown()
        job = self.cooldown_job(cooldown={"mode": "manual", "seconds": 900})
        outcome, events = self.observed(job)
        self.assertEqual([type(event) for event in events], [JobStarted, RunStarted, RunFinished, CooldownStarted, CooldownEnded, RunStarted, RunFinished, CooldownStarted, CooldownEnded, RunStarted, RunFinished, JobFinished])
        started, first, first_done, cooldown, cooled = events[:5]
        self.assertEqual((started.job_name, started.mode, started.total_runs, started.seed, started.seed_source), ("sunset-walk", "i2v", 3, 42, "config_file"))
        self.assertEqual((started.cooldown, started.cooldown_source, started.model, started.input), (CooldownPolicy(mode="manual", seconds=900.0), "job", "base.ckpt", str(job.input)))
        self.assertEqual((started.job_file, started.manifest, started.log, started.at), (str(job.path), str(outcome.manifest), str(outcome.log), "2026-09-24T15:30:12" + started.at[19:]))
        self.assertEqual(started.source_text, job.path.read_text(encoding="utf-8"))
        self.assertNotIn(started.source_text, repr(job))
        self.assertEqual(job, replace(job, source_text="# a comment\n"))
        arguments = self.calls[0][0]
        self.assertEqual((first.number, first.total, first.pair, first.positive, first.negative), (1, 3, "only", "text", None))
        self.assertEqual((first.input, first.output, first.last_frame), (str(job.input), arguments.output.name, arguments.output.stem + "-last-frame.png"))
        self.assertEqual(first.command, tuple(GenerationService.redact_command(arguments.command)))
        self.assertEqual((first_done.number, first_done.status, first_done.exit_code, first_done.output, first_done.last_frame), (1, "succeeded", 0, arguments.output.name, first.last_frame))
        self.assertEqual((cooldown.after_run, cooldown.seconds, cooldown.until, cooldown.mode, cooldown.ratio, cooldown.run_seconds, cooldown.bound), (1, 900.0, "15:45:12", "manual", None, first_done.seconds, None))
        self.assertEqual((cooled.waited_seconds, cooled.cut_short), (900.0, False))
        finished = events[-1]
        self.assertEqual((finished.status, finished.exit_code, finished.completed_runs, finished.total_runs, finished.signal), ("succeeded", 0, 3, 3, None))

    def test_auto_cooldown_events_say_why_the_wait_is_that_long(self) -> None:
        self.fake_cooldown()
        self.run_times(1440.0, 180.0, 60.0)
        _outcome, events = self.observed(self.cooldown_job(cooldown={"mode": "auto", "minimum_seconds": 300}))
        started = events[0]
        self.assertEqual((started.cooldown, started.cooldown_source), (CooldownPolicy(mode="auto", minimum_seconds=300.0), "job"))
        cooldowns = [event for event in events if isinstance(event, CooldownStarted)]
        self.assertEqual([(event.after_run, event.seconds, event.mode, event.ratio, event.run_seconds, event.bound) for event in cooldowns], [(1, 720.0, "auto", 0.5, 1440.0, None), (2, 300.0, "auto", 0.5, 180.0, "minimum")])

    def test_events_for_failed_and_timed_out_runs(self) -> None:
        for result, status, exit_code in ((FakeResult(return_code=3), "failed", 3), (FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM), "timed_out", 124)):
            with self.subTest(status=status):
                self.calls.clear()
                self.results[1] = result
                self.missing_output.add(1)
                _outcome, events = self.observed(self.job())
                self.assertEqual([type(event) for event in events], [JobStarted, RunStarted, RunFinished, JobFinished])
                self.assertEqual((events[2].status, events[2].exit_code, events[2].output), (status, exit_code, None))
                self.assertEqual((events[3].status, events[3].exit_code, events[3].completed_runs, events[3].signal), ("failed", exit_code, 0, None))

    def test_interrupted_run_names_the_signal(self) -> None:
        self.results[1] = FakeResult(return_code=0, termination_signal=signal.SIGINT)
        _outcome, events = self.observed(self.job())
        self.assertEqual((events[2].status, events[2].output is not None), ("interrupted", True))
        self.assertEqual((events[3].status, events[3].exit_code, events[3].signal), ("interrupted", 130, "SIGINT"))

    def test_run_output_carries_the_childs_lines_in_order(self) -> None:
        lines = ((OutputStream.STDOUT, "loading"), (OutputStream.STDERR, "step 3/8"), (OutputStream.STDOUT, "done"))
        seen_arguments: list = []

        def factory(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> TalkingRunner:
            seen_arguments.append(on_message)
            return TalkingRunner(arguments, on_message, lines)

        self.service._runner_factory = factory
        _outcome, events = self.observed(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        output = [event for event in events if isinstance(event, RunOutput)]
        self.assertEqual([(event.number, event.stream, event.text, event.progress) for event in output], [(run, stream.value, text, (3, 8) if "3/8" in text else None) for run in (1, 2) for stream, text in lines])
        # Output arrives between its run's start and finish.
        kinds = [type(event) for event in events]
        self.assertEqual(kinds[:6], [JobStarted, RunStarted, RunOutput, RunOutput, RunOutput, RunFinished])

    def test_without_an_observer_the_factory_gets_no_callback(self) -> None:
        self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertEqual(self.callbacks, [None])
        self.observed(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertTrue(callable(self.callbacks[1]))

    def test_a_runner_that_raises_still_closes_the_events(self) -> None:
        def cannot_start(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            raise ValueError("Could not start executable draw-things-cli: Permission denied")

        self.service._runner_factory = cannot_start
        events: list = []
        with self.assertRaisesRegex(ValueError, "Could not start"):
            self.service.run(self.job(), executable="draw-things-cli", shutdown_grace=2, observer=events.append)
        self.assertEqual([type(event) for event in events], [JobStarted, RunStarted, RunFinished, JobFinished])
        # The runner never wrote the file, so the event does not name it, though the manifest record still does.
        self.assertEqual((events[2].status, events[2].exit_code, events[2].output), ("failed", None, None))
        self.assertEqual((events[3].status, events[3].exit_code, events[3].completed_runs), ("failed", None, 0))

    def test_errors_before_the_job_starts_send_no_events(self) -> None:
        self.service._find_executable = lambda _executable: None
        events: list = []
        with self.assertRaisesRegex(ValueError, "Could not find"):
            self.service.run(self.job(), executable="draw-things-cli", shutdown_grace=2, observer=events.append)
        self.assertEqual(events, [])

    def test_an_observer_that_raises_does_not_change_the_outcome(self) -> None:
        def broken(_event: object) -> None:
            raise RuntimeError("display failed")

        expected = self.run_job(self.job(), write_records=False)
        recorded: list = []
        outcome = self.service.run(self.job(), executable="draw-things-cli", shutdown_grace=2, observer=combine_observers(broken, recorded.append))
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (expected.exit_code, expected.completed_runs))
        # The observer after the broken one still saw every event.
        self.assertEqual((type(recorded[0]), type(recorded[-1])), (JobStarted, JobFinished))

    def test_cancel_from_another_thread_stops_a_running_run(self) -> None:
        runners: list[BlockingRunner] = []

        def factory(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> BlockingRunner:
            runners.append(BlockingRunner(arguments))
            return runners[-1]

        self.service._runner_factory = factory
        thread, result = self.start_in_thread(self.job())
        self.wait_for_event(RunStarted)
        self.assertTrue(self.service.cancel())
        thread.join(10)
        self.assertNotIn("error", result)
        self.assertEqual((result["outcome"].exit_code, result["outcome"].completed_runs, len(runners)), (143, 0, 1))
        self.assertEqual(runners[0].shutdown_signal, signal.SIGTERM)
        self.assertEqual([type(event) for event in self.events], [JobStarted, RunStarted, RunFinished, JobFinished])
        self.assertEqual((self.events[2].status, self.events[3].status, self.events[3].signal), ("interrupted", "interrupted", "SIGTERM"))
        self.assertEqual(self.manifest(result["outcome"])["status"], "interrupted")

    def test_cancel_ends_a_real_cooldown_wait_at_once(self) -> None:
        self.service._cooldown = self.service._wait_for_cooldown
        open_fds = set(os.listdir("/dev/fd"))
        thread, result = self.start_in_thread(self.cooldown_job(cooldown={"mode": "manual", "seconds": 60}))
        self.wait_for_event(CooldownStarted)
        started = time.monotonic()
        self.assertTrue(self.service.cancel(signal.SIGINT))
        thread.join(10)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((result["outcome"].exit_code, result["outcome"].completed_runs, len(self.calls)), (130, 1, 1))
        ended = next(event for event in self.events if isinstance(event, CooldownEnded))
        self.assertTrue(ended.cut_short)
        self.assertLess(ended.waited_seconds, 3)
        self.assertEqual((self.events[-1].status, self.events[-1].signal), ("interrupted", "SIGINT"))
        self.assertIn("Job stopped by SIGINT during the cooldown before run 2/3", result["outcome"].log.read_text(encoding="utf-8"))
        # The wake-up pipe is closed with the job.
        self.assertEqual(set(os.listdir("/dev/fd")), open_fds)

    def test_cancel_with_no_job_running_does_nothing(self) -> None:
        self.assertFalse(self.service.cancel())
        outcome = self.run_job(self.job())
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 5))
        self.assertFalse(self.service.cancel())

    def test_cancel_between_runner_creation_and_run_still_stops_that_run(self) -> None:
        runners: list[FakeRunner] = []

        def factory(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            # The cancel lands before the service has stored the runner, so it can only reach it through the flag.
            self.assertTrue(self.service.cancel())
            runners.append(FakeRunner(arguments, FakeResult(), write_output=True))
            return runners[-1]

        self.service._runner_factory = factory
        outcome = self.run_job(self.job())
        self.assertEqual(runners[0].shutdown_signal, signal.SIGTERM)
        self.assertEqual(len(runners), 1)
        # The fake run itself succeeds, so the job stops at the flag before run 2.
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (143, 1))

    def test_a_second_concurrent_run_is_refused(self) -> None:
        refusals: list[Exception] = []

        def factory(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float, on_message: object = None, on_start: object = None) -> FakeRunner:
            try:
                self.service.run(self.job(), executable="draw-things-cli", shutdown_grace=2)
            except RuntimeError as error:
                refusals.append(error)
            return self.create_runner(arguments, timeout, grace, on_message, on_start)

        self.service._runner_factory = factory
        outcome = self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(len(refusals), 1)
        # The refused call left the running job's state alone, and the service is free again afterwards.
        self.assertEqual(self.run_job(self.job()).exit_code, 0)
