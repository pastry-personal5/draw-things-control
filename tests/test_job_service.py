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

from job_fixtures import JobTestCase, job_data
from PIL import Image

import job_service
from draw_things_arguments import DrawThingsGenerateArguments
from draw_things_runner import install_signal_handlers, restore_signal_handlers
from job_definition import JobDefinition, load_job
from job_service import JobService

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


class JobServiceTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple[DrawThingsGenerateArguments, float | None, float]] = []
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

    def create_runner(self, arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float) -> FakeRunner:
        self.calls.append((arguments, timeout, grace))
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

    def test_i2v_runs_chain_last_frames_in_batch_order(self) -> None:
        job = self.job(batch_count=4, prompt_pairs=[{"name": "walk", "positive": "walk", "batches": [1, 3]}, {"name": "wave", "positive": "wave", "batches": [2, 4]}], run_timeout_seconds=60)
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
        self.assertEqual([(run["batch"], run["pair"], run["status"]) for run in manifest["runs"]], [(1, "walk", "succeeded"), (2, "wave", "succeeded"), (3, "walk", "succeeded"), (4, "wave", "succeeded")])
        self.assertIn("Run 4/4 (batch 4, pair wave)", outcome.log.read_text(encoding="utf-8"))
        self.assertEqual(manifest["log_file"], outcome.log.name)

    def test_t2v_first_run_has_no_image(self) -> None:
        self.run_job(self.job(mode="t2v", input=None, batch_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertIsNone(self.calls[0][0].image)
        self.assertEqual(self.calls[1][0].image, self.extracted[0][1])

    def test_i2i_chains_png_outputs_without_extraction(self) -> None:
        self.run_job(self.job(mode="i2i", batch_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertEqual(self.calls[0][0].output.suffix, ".png")
        self.assertEqual(self.calls[1][0].image, self.calls[0][0].output)
        self.assertEqual(self.extracted, [])

    def test_arguments_carry_overrides_and_config_json(self) -> None:
        self.run_job(self.job(batch_count=1, prompt_pairs=[{"name": "only", "positive": "text", "negative": "blur"}], config_override={"steps": 40, "guidance_scale": 5.0, "refiner_model": "job-refiner.ckpt", "refiner_start": 0.1, "shift": 3.99}))
        arguments = self.calls[0][0]
        self.assertEqual((arguments.steps, arguments.cfg, arguments.seed, arguments.negative_prompt), (40, 5.0, 42, "blur"))
        config = json.loads(arguments.config_json or "{}")
        self.assertEqual((config["refinerModel"], config["refinerStart"], config["shift"], config["model"]), ("job-refiner.ckpt", 0.1, 3.99, "base.ckpt"))
        self.assertNotIn("--config-file", arguments.command)

    def test_random_seed_is_drawn_once_and_recorded(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448}, name="noseed.json")
        outcome = self.run_job(self.job(config_file="noseed.json"))
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

    def test_run_that_cannot_start_is_marked_failed(self) -> None:
        def cannot_start(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float) -> FakeRunner:
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
        with mock.patch("job_service.install_signal_handlers", interrupted), mock.patch("job_service.remove_job_log", wraps=job_service.remove_job_log) as remove:
            with self.assertRaises(KeyboardInterrupt):
                self.run_job(self.job())
        remove.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_i2v_config_json_omits_batch_count_and_the_log_says_so(self) -> None:
        self.write_base_config({"model": "m.ckpt", "width": 832, "height": 448, "batchCount": 3}, name="batch.json")
        outcome = self.run_job(self.job(config_file="batch.json", batch_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        self.assertNotIn("batchCount", json.loads(self.calls[0][0].config_json or "{}"))
        self.assertIn("Ignoring batchCount (3) from config_file batch.json", outcome.log.read_text(encoding="utf-8"))

    def test_records_are_off_by_default(self) -> None:
        job = self.job(batch_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])
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

        def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float) -> FakeRunner:
            # Look at the image while the run is happening, since the copy is gone afterwards.
            if not self.calls:
                with Image.open(arguments.image) as image:
                    seen.append(image.size)
            return self.create_runner(arguments, timeout, grace)

        self.service._runner_factory = create_runner
        job = self.resize_job(batch_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])
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
        def cannot_start(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float) -> FakeRunner:
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
                with mock.patch("input_resize.resize_image") as resize, mock.patch("input_resize.tempfile.mkdtemp") as mkdtemp:
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

        def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, grace: float) -> FakeRunner:
            if not self.calls:
                with Image.open(arguments.image) as image:
                    seen.append((image.size, image.getexif().get(0x0112)))
            return self.create_runner(arguments, timeout, grace)

        self.service._runner_factory = create_runner
        job = self.job(input="rotated.jpg", desired_input_width=832)
        self.run_job(job)
        self.assertNotEqual(self.calls[0][0].image, job.input)
        self.assertEqual(seen, [((832, 448), None)])

    def test_jobs_without_desired_keys_build_the_same_commands(self) -> None:
        outcome = self.run_job(self.job(batch_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        arguments = self.calls[0][0]
        self.assertEqual((arguments.width, arguments.height), (None, None))
        self.assertNotIn("--width", arguments.command)
        self.assertEqual(json.loads(arguments.config_json or "{}")["width"], 832)
        self.assertIsNone(self.manifest(outcome)["input_resize"])

    def test_resize_failure_leaves_no_output_directory_or_manifest(self) -> None:
        job = self.resize_job()
        with mock.patch("input_resize.resize_image", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "Could not resize input"):
                self.run_job(job)
        self.assertFalse(job.output_directory.exists())
        self.assertEqual(self.calls, [])

    def test_preview_shows_a_placeholder_and_writes_nothing(self) -> None:
        job = self.resize_job()
        with mock.patch("input_resize.resize_image") as resize:
            preview = self.service.preview(job, executable="draw-things-cli")
        resize.assert_not_called()
        command = preview.command_previews[0]
        self.assertIn("--image '<photo.jpg resized to 832x448>'", command)
        self.assertIn("--width 832 --height 448", command)
        self.assertIn("--width 832 --height 448", preview.command_previews[1])
        self.assertFalse(job.output_directory.exists())

    # Cooldown between runs.

    def cooldown_job(self, **changes: object) -> JobDefinition:
        return self.job(**{"batch_count": 3, "prompt_pairs": [{"name": "only", "positive": "text"}], **changes})

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

    def test_cooldown_waits_between_runs_but_not_after_the_last(self) -> None:
        waits = self.fake_cooldown()
        for mode, changes in (("i2v", {}), ("i2i", {}), ("t2v", {"input": None})):
            with self.subTest(mode=mode):
                waits.clear()
                self.calls.clear()
                shutil.rmtree(self.output_directory, ignore_errors=True)
                outcome = self.run_job(self.cooldown_job(mode=mode, cooldown_seconds=900, **changes))
                self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 3))
                # Each wait comes after one run and before the next, and starts with the manifest showing 0.
                self.assertEqual(waits, [(900.0, 1, [0.0]), (900.0, 2, [900.0, 0.0])])
                manifest = self.manifest(outcome)
                self.assertEqual((manifest["cooldown_seconds"], manifest["cooldown_source"]), (900.0, "job"))
                self.assertEqual([run["cooldown_after_seconds"] for run in manifest["runs"]], [900.0, 900.0, None])
                log = outcome.log.read_text(encoding="utf-8")
                self.assertIn("cooldown 900 s (from job)", log)
                self.assertIn("Cooldown: waiting 900 s before run 2/3 (until 15:45:12)", log)
                self.assertIn("Cooldown finished; starting run 3/3", log)

    def test_global_cooldown_applies_and_a_job_can_turn_it_off(self) -> None:
        waits = self.fake_cooldown()
        self.global_config = replace(self.global_config, cooldown_seconds=60.0)
        outcome = self.run_job(self.cooldown_job())
        self.assertEqual([seconds for seconds, _runs, _saved in waits], [60.0, 60.0])
        self.assertEqual(self.manifest(outcome)["cooldown_source"], "global_config")
        waits.clear()
        outcome = self.run_job(self.cooldown_job(cooldown_seconds=0))
        self.assertEqual(waits, [])
        self.assertIn("no cooldown (from job)", outcome.log.read_text(encoding="utf-8"))

    def test_no_cooldown_without_either_key(self) -> None:
        waits = self.fake_cooldown()
        outcome = self.run_job(self.cooldown_job())
        self.assertEqual(waits, [])
        manifest = self.manifest(outcome)
        self.assertEqual((manifest["cooldown_seconds"], manifest["cooldown_source"]), (0.0, "default"))
        self.assertNotIn("Cooldown", outcome.log.read_text(encoding="utf-8"))

    def test_no_cooldown_after_a_failed_or_timed_out_run(self) -> None:
        waits = self.fake_cooldown()
        for result in (FakeResult(return_code=3), FakeResult(return_code=-15, timed_out=True, termination_signal=signal.SIGTERM)):
            with self.subTest(result=result):
                self.calls.clear()
                self.results[1] = result
                outcome = self.run_job(self.cooldown_job(cooldown_seconds=900))
                self.assertEqual((len(self.calls), waits), (1, []))
                self.assertIsNone(self.manifest(outcome)["runs"][0]["cooldown_after_seconds"])

    def test_signal_during_a_cooldown_stops_the_job(self) -> None:
        waits = self.fake_cooldown(interrupt_on=1, waited=412.34)
        outcome = self.run_job(self.cooldown_job(cooldown_seconds=900))
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
        outcome = self.run_job(self.cooldown_job(cooldown_seconds=900))
        self.assertEqual((outcome.exit_code, len(self.calls)), (143, 1))
        self.assertEqual(self.manifest(outcome)["runs"][0]["cooldown_after_seconds"], 900.0)
        log = outcome.log.read_text(encoding="utf-8")
        self.assertIn("Job stopped by SIGTERM before run 2/3", log)
        self.assertNotIn("during the cooldown", log)

    def test_cooldown_end_time_is_local_like_the_other_timestamps(self) -> None:
        utc_now = datetime(2026, 9, 24, 6, 30, 12, tzinfo=timezone.utc)
        self.service._clock = lambda: utc_now
        outcome = self.run_job(self.cooldown_job(batch_count=2, cooldown_seconds=90))
        expected = (utc_now.astimezone() + timedelta(seconds=90)).strftime("%H:%M:%S")
        self.assertIn(f"Cooldown: waiting 90 s before run 2/2 (until {expected})", outcome.log.read_text(encoding="utf-8"))
