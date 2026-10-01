"""Tests for a correcting job's runs: the anchor each run is held to, the files it writes, and a failed correction."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.jobs.color_run import CorrectionRequest, CorrectionResult
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import FirstImageDropped, JobStarted, MediaChecked, RunFinished, RunStarted, RunStatus
from draw_things_control.jobs.executor import ResumePoint
from draw_things_control.jobs.media.checks import MediaCheck
from draw_things_control.jobs.parsing import load_job
from tests.fixtures import JobTestCase, job_data, job_executor, run_job_with
from tests.jobs.test_executor import FakeResult, FakeRunner
from tests.jobs.test_media_checks import FakeChecker, write_frame

ALTERNATING = [{"name": "walk", "positive": "walk", "runs": [1, 3]}, {"name": "wave", "positive": "wave", "runs": [2, 4]}]


class FakeCorrector:
    """Writes the handoff and the copy as a correction would, and records each request; ``fail`` makes it fail as a
    missing encoder would, and ``during`` runs while it corrects (to request a stop, say)."""

    def __init__(self) -> None:
        self.requests: list[CorrectionRequest] = []
        self.fail = False
        self.during: Callable[[], Any] | None = None

    def __call__(self, request: CorrectionRequest) -> CorrectionResult:
        self.requests.append(request)
        if self.during is not None:
            self.during()
        drift = MediaCheck("color_drift", request.video.name, "a drift")
        if self.fail:
            return CorrectionResult(False, drift, (MediaCheck("color_correction", request.copy.name, "not corrected", warnings=("The correction failed.",)),))
        assert request.video.is_file() and not request.handoff.exists()
        request.handoff.write_bytes(b"corrected")
        request.copy.write_bytes(b"copy")
        return CorrectionResult(True, drift, (MediaCheck("color_correction", request.copy.name, "corrected"),))


class CorrectingJobTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.checker = FakeChecker()
        self.corrector = FakeCorrector()
        self.service = job_executor(
            runner_factory=lambda arguments, *rest: FakeRunner(arguments, FakeResult(), write_output=True),
            find_executable=lambda executable: executable,
            frame_extractor=write_frame,
            require_ffmpeg=lambda: "ffmpeg",
            checker=self.checker,
            corrector=self.corrector,
            handle_signals=False,
            cooldown=lambda seconds: seconds,
        )

    def job(self, **changes: object) -> JobDefinition:
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.params)

    def run_job(self, job: JobDefinition, **options: Any) -> list:
        events: list = []
        # Kept here too, for a job that raises before run_job returns them.
        self.events = events
        self.outcome = run_job_with(self.service, job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append, **options)
        return events

    def test_a_blend_job_holds_every_run_to_the_first_image_and_writes_its_copy_and_raw_frame(self) -> None:
        events = self.run_job(self.job(run_count=3, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "blend"}))
        started = events[0]
        assert isinstance(started, JobStarted) and started.first_image is not None
        first_image = Path(started.first_image)
        self.assertEqual([request.anchor for request in self.corrector.requests], [first_image] * 3)
        self.assertEqual([request.reanchored for request in self.corrector.requests], [False] * 3)
        runs = [event for event in events if isinstance(event, RunStarted)]
        self.assertEqual([run.anchor for run in runs], [str(first_image)] * 3)
        finished = [event for event in events if isinstance(event, RunFinished)]
        output_directory = Path(started.output_directory)
        for run in finished:
            assert run.output is not None and run.last_frame is not None
            stem = Path(run.output).stem
            self.assertEqual(run.corrected_output, f"{stem}-cc.mov")
            self.assertEqual((output_directory / run.last_frame).read_bytes(), b"corrected")
            self.assertEqual((output_directory / f"{stem}-last-frame-raw.png").read_bytes(), b"png")
        # Each run is corrected back to its own input: run 1's copy of the input, then each run's handoff.
        inputs = [request.run_input for request in self.corrector.requests]
        self.assertEqual(inputs[0].name, "first-frame-832x448.png")
        self.assertEqual(inputs[1:], [output_directory / run.last_frame for run in finished[:2] if run.last_frame is not None])
        manifest = json.loads(Path(started.manifest or "").read_text(encoding="utf-8"))
        self.assertEqual([(run["anchor"], run["corrected_output"]) for run in manifest["runs"]], [(str(first_image), run.corrected_output) for run in finished])
        # The drift comes from the correction's own pass, then its checks, after the last frame check of the handoff.
        stages = [event.stage for event in events if isinstance(event, MediaChecked) and event.run == 1]
        self.assertEqual(stages[-3:], ["last_frame", "color_drift", "color_correction"])
        self.assertNotIn("color_drift", self.checker.asked)

    def test_alternating_pairs_reanchor_at_every_change_unless_told_never(self) -> None:
        self.run_job(self.job(run_count=4, prompt_pairs=ALTERNATING, color={"anchor": "first"}))
        requests = self.corrector.requests
        self.assertEqual([request.reanchored for request in requests], [False, True, True, True])
        self.assertEqual([request.anchor for request in requests[1:]], [request.run_input for request in requests[1:]])
        self.corrector.requests.clear()
        self.run_job(self.job(run_count=4, prompt_pairs=ALTERNATING, color={"anchor": "first", "reanchor": "never"}))
        [anchor] = {request.anchor for request in self.corrector.requests}
        assert anchor is not None
        self.assertTrue(anchor.name.endswith("-first-image.png"))

    def test_previous_has_no_anchor_and_none_is_not_corrected(self) -> None:
        events = self.run_job(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "previous"}))
        self.assertEqual([request.anchor for request in self.corrector.requests], [None, None])
        self.assertEqual([run.anchor for run in events if isinstance(run, RunStarted)], [None, None])
        self.corrector.requests.clear()
        events = self.run_job(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "walk"}]))
        self.assertEqual(self.corrector.requests, [])
        [finished] = [event for event in events if isinstance(event, RunFinished)]
        self.assertIsNone(finished.corrected_output)
        self.assertIn("color_drift", self.checker.asked)

    def test_a_failed_correction_hands_off_the_uncorrected_frame_and_the_run_succeeds(self) -> None:
        self.corrector.fail = True
        events = self.run_job(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "previous"}))
        self.assertEqual(self.outcome.exit_code, 0)
        finished = [event for event in events if isinstance(event, RunFinished)]
        started = events[0]
        assert isinstance(started, JobStarted)
        for run in finished:
            assert run.output is not None and run.last_frame is not None
            self.assertEqual((Path(started.output_directory) / run.last_frame).read_bytes(), b"png")
            self.assertFalse((Path(started.output_directory) / f"{Path(run.output).stem}-last-frame-raw.png").exists())
            self.assertIsNone(run.corrected_output)
        # The next run still corrects back to its own input, the uncorrected handoff.
        self.assertEqual(self.corrector.requests[1].run_input, Path(started.output_directory) / (finished[0].last_frame or ""))
        warnings = [event for event in events if isinstance(event, MediaChecked) and event.stage == "color_correction"]
        self.assertEqual([event.verdict for event in warnings], ["warning", "warning"])

    def test_a_stop_during_the_correction_takes_effect_when_it_ends(self) -> None:
        self.corrector.during = lambda: self.service.cancel()
        events = self.run_job(self.job(run_count=3, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "previous"}))
        [finished] = [event for event in events if isinstance(event, RunFinished)]
        self.assertEqual((finished.status, finished.corrected_output is not None), ("succeeded", True))
        self.assertEqual((self.outcome.completed_runs, len(self.corrector.requests)), (1, 1))
        self.assertNotEqual(self.outcome.exit_code, 0)

    def test_a_t2v_jobs_run_1_is_not_corrected_and_run_2_is_held_to_its_last_frame(self) -> None:
        events = self.run_job(self.job(mode="t2v", input=None, run_count=2, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "first"}))
        started = events[0]
        assert isinstance(started, JobStarted) and started.first_image is not None
        [request] = self.corrector.requests
        self.assertEqual(request.anchor, Path(started.first_image))
        runs = [event for event in events if isinstance(event, RunStarted)]
        self.assertEqual([run.anchor for run in runs], [None, started.first_image])

    def test_a_t2v_first_image_that_is_not_kept_is_dropped_with_an_event(self) -> None:
        t2v = {"mode": "t2v", "input": None, "run_count": 2, "prompt_pairs": [{"name": "only", "positive": "walk"}], "color": {"anchor": "first"}}
        cases: dict[str, tuple[Callable[[], Any], str]] = {
            "run 1 fails": (lambda: mock.patch.object(self.service._launcher, "launch", return_value=(RunStatus.FAILED, 1)), "Run 1 failed, so no first image is kept."),
            "run 1 raises": (lambda: mock.patch.object(self.service._launcher, "launch", side_effect=RuntimeError("broken")), "Run 1 failed, so no first image is kept."),
            "the copy fails": (lambda: mock.patch("draw_things_control.jobs.executor.shutil.copyfile", side_effect=OSError("disk full")), "The first image could not be kept (disk full), so drift since it is left out."),
        }
        for name, (patch, reason) in cases.items():
            with self.subTest(name):
                with patch(), contextlib.suppress(RuntimeError):
                    self.run_job(self.job(**t2v))
                events = self.events
                started = events[0]
                assert isinstance(started, JobStarted) and started.first_image is not None and started.manifest is not None
                [dropped] = [event for event in events if isinstance(event, FirstImageDropped)]
                self.assertEqual(dropped.reason, reason)
                # Before run 1's RunFinished, as the kept copy is.
                self.assertLess(events.index(dropped), next(index for index, event in enumerate(events) if isinstance(event, RunFinished)))
                self.assertIsNone(json.loads(Path(started.manifest).read_text(encoding="utf-8"))["first_image"])
                self.assertFalse(Path(started.first_image).exists())

    def test_a_kept_t2v_first_image_and_an_i2v_one_drop_nothing(self) -> None:
        for changes in ({"mode": "t2v", "input": None}, {}):
            with self.subTest(changes.get("mode", "i2v")):
                events = self.run_job(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "first"}, **changes))
                self.assertFalse([event for event in events if isinstance(event, FirstImageDropped)])

    def test_a_resume_is_held_to_the_anchor_of_the_run_it_continues_after(self) -> None:
        job = self.job(run_count=4, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "blend"})
        self.output_directory.mkdir(parents=True, exist_ok=True)
        first_image, anchor, last_frame = (self.output_directory / name for name in ("old-job-first-image.png", "run-2-input.png", "run-2-last-frame.png"))
        for path in (first_image, anchor, last_frame):
            path.write_bytes(b"png")
        self.run_job(job, resume=ResumePoint(first_run=3, input=last_frame, seed=7, resumes_execution="E0001", first_image=first_image, anchor=anchor))
        self.assertEqual([(request.anchor, request.first_image) for request in self.corrector.requests], [(anchor, first_image)] * 2)

    def test_a_resume_whose_anchor_is_gone_is_held_to_the_first_image(self) -> None:
        job = self.job(run_count=4, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "blend"})
        self.output_directory.mkdir(parents=True, exist_ok=True)
        first_image, last_frame = (self.output_directory / name for name in ("old-job-first-image.png", "run-2-last-frame.png"))
        for path in (first_image, last_frame):
            path.write_bytes(b"png")
        gone = self.output_directory / "run-2-input.png"
        self.run_job(job, resume=ResumePoint(first_run=3, input=last_frame, seed=7, resumes_execution="E0001", first_image=first_image, anchor=gone))
        self.assertEqual([(request.anchor, request.first_image) for request in self.corrector.requests], [(first_image, first_image)] * 2)
