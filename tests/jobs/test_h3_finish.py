"""H3 keeps a verified audiovisual output when an optional handoff fails."""

from __future__ import annotations

import json
import signal
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image

from framecontrol.jobs.backend import BackendRegistry, RunContext
from framecontrol.jobs.backends.h3 import H3Backend, H3ColorState, H3JobData, H3Run, H3Runtime, _partial_path
from framecontrol.jobs.definition import ColorPolicy
from framecontrol.jobs.events import RunStatus
from framecontrol.jobs.executor import JobExecutor, JobRunOptions
from framecontrol.jobs.media.checks import MediaCheck
from framecontrol.jobs.media.h3_color import DELIVERY, H3_RGB_CONVENTION_SHA256, NATIVE, H3Declarations
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.output_naming import raw_output_path, uncorrected_output_path
from framecontrol.jobs.planning import JobPlanner
from framecontrol.jobs.records import RunRecord
from tests.fixtures import JobTestCase, job_data, load_legacy_job


class FinishingH3Backend(H3Backend):
    def check_tools(self, job: Any, executable: str, media: MediaTools, find_executable: Any) -> None:
        self._ffmpeg = "ffmpeg"
        self._ffprobe = "ffprobe"


class FakeResult:
    return_code = 0
    timed_out = False
    termination_signal = None


class H3FinishingTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.backend = FinishingH3Backend(H3Runtime(Path("/fake/h3"), Path("/fake/model")))
        self.backends = BackendRegistry((self.backend,))
        self.job = load_legacy_job(self.write_legacy_job(job_data(backend="h3", mode="i2av", config_file=None, run_count=2, prompt_pairs=[{"name": "scene", "positive": "a scene"}], backend_settings={"width": 64, "height": 64, "frames": 24, "seconds": 1, "steps": 2, "seed": 42}, output={"extension": "mp4"})), self.global_config, self.params, backends=self.backends)
        self.backend.check_tools(self.job, "h3", MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), lambda _: "ffmpeg")

    @staticmethod
    def _facts() -> dict[str, Any]:
        return {"width": 64, "height": 64, "frames": 39, "frame_rate": 24, "video_duration": 39 / 24, "audio_codec": "aac", "audio_sample_rate": 32000, "audio_channels": 2, "audio_duration": 39 / 24}

    def _planned_run(self) -> Any:
        planner = JobPlanner(lambda _: "ffmpeg", MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), backends=self.backends)
        run = planner.plan_run(self.job, 1, self.job.schedule()[0], self.job.input, 42, "h3", set())
        run.output.parent.mkdir(parents=True, exist_ok=True)
        return run

    def _record(self, output: Path) -> RunRecord:
        return RunRecord(pair="scene", positive="a scene", negative=None, input=str(self.job.input), output=output.name, last_frame=None, command=[], started_at="now")

    @contextmanager
    def _known_source(self) -> Any:
        declarations = H3Declarations(None, None, {"color_range": None, "color_space": None, "color_transfer": None, "color_primaries": None})
        with ExitStack() as stack:
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._file_sha256", return_value=H3_RGB_CONVENTION_SHA256))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.inspect_h3_declarations", return_value=declarations))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.convert_h3_delivery", side_effect=ValueError("conversion failed")))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.tag_video_color_codes", side_effect=ValueError("tag failed")))
            yield

    def test_failed_handoff_keeps_verified_output_and_reports_warning(self) -> None:
        run = self._planned_run()
        _partial_path(run.output).write_bytes(b"verified child")
        record = self._record(run.output)
        checks: list[MediaCheck] = []
        with self._known_source(), mock.patch("framecontrol.jobs.backends.h3._verify_h3_mp4", return_value=self._facts()), mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=ValueError("decode failed")):
            result = self.backend.finish(self.job, run, record, lambda: None, None, MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), lambda _run, check: checks.append(check))
        self.assertIsNone(result)
        self.assertEqual(run.output.read_bytes(), b"verified child")
        self.assertIsNone(record.last_frame)
        self.assertFalse(run.last_frame.is_file() if run.last_frame is not None else False)
        self.assertEqual(checks[0].verdict, "warning")
        self.assertTrue(any("extraction or image validation failed" in warning for warning in checks[0].warnings))

    def test_cancel_during_handoff_stops_before_output_promotion(self) -> None:
        run = self._planned_run()
        _partial_path(run.output).write_bytes(b"verified child")
        record = self._record(run.output)
        calls = 0

        def stop() -> signal.Signals | None:
            nonlocal calls
            calls += 1
            return signal.SIGTERM if calls == 2 else None

        def extract(_video: Path, target: Path, _ffmpeg: str) -> None:
            Image.new("RGB", (64, 64)).save(target)

        with self._known_source(), mock.patch("framecontrol.jobs.backends.h3._verify_h3_mp4", return_value=self._facts()), mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=extract):
            result = self.backend.finish(self.job, run, record, stop, None, MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), None)
        self.assertEqual(result, (RunStatus.INTERRUPTED, 143))
        self.assertFalse(run.output.exists())
        self.assertFalse(_partial_path(run.output).exists())
        self.assertFalse(run.last_frame.is_file() if run.last_frame is not None else False)

    def test_dependent_i2av_stops_without_launching_child(self) -> None:
        seen: list[int] = []

        class Runner:
            def __init__(self, arguments: Any) -> None:
                self.arguments = arguments

            def run(self) -> FakeResult:
                seen.append(1)
                self.arguments.output.write_bytes(b"verified child")
                return FakeResult()

            def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
                pass

        executor = JobExecutor(lambda arguments, *_rest: Runner(arguments), lambda _: "ffmpeg", MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), handle_signals=False, backends=self.backends)
        with self._known_source(), mock.patch("framecontrol.jobs.backends.h3._verify_h3_mp4", return_value=self._facts()), mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=ValueError("decode failed")):
            outcome = executor.run(self.job, JobRunOptions(executable="h3", shutdown_grace=1, write_records=True))
        self.assertEqual((outcome.exit_code, outcome.completed_runs, len(seen)), (1, 1, 1))
        assert outcome.manifest is not None
        manifest = json.loads(outcome.manifest.read_text(encoding="utf-8"))
        self.assertEqual((manifest["status"], manifest["runs"][0]["status"], manifest["runs"][0]["last_frame"]), ("failed", "succeeded", None))

    def test_segment_anchor_resets_after_r2av_and_changed_prompt_pair(self) -> None:
        data = self.job.backend_data
        assert isinstance(data, H3JobData)
        mixed = replace(self.job, backend_data=replace(data, runs=(H3Run(1, "i2av"), H3Run(2, "r2av"), H3Run(3, "i2av"))))
        pair = self.job.schedule()[0]
        other = replace(pair, name="other")
        first = Path("first.png")
        stable = Path("saved-first.png")
        start = self.backend.prepare_run(mixed, RunContext(1, pair, first, stable, None, None, (), False))
        self.assertEqual(start.chain_anchor, stable)
        self.assertEqual(start.record_anchor, stable)
        assert isinstance(start.finish_state, H3ColorState)
        self.assertEqual(start.finish_state.reanchor_reason, "initial i2av input")
        boundary = self.backend.prepare_run(mixed, RunContext(2, pair, Path("prior-handoff.png"), stable, stable, pair, (), False))
        self.assertIsNone(boundary.chain_anchor)
        after = self.backend.prepare_run(mixed, RunContext(3, pair, Path("r2av-handoff.png"), stable, stable, pair, (), False))
        self.assertEqual(after.chain_anchor, Path("r2av-handoff.png"))
        assert isinstance(after.finish_state, H3ColorState)
        self.assertEqual(after.finish_state.reanchor_reason, "after r2av boundary")
        all_i2av = replace(mixed, backend_data=replace(data, runs=(H3Run(1, "i2av"), H3Run(2, "i2av"), H3Run(3, "i2av"))))
        changed = self.backend.prepare_run(all_i2av, RunContext(2, other, Path("new-input.png"), stable, stable, pair, (), False))
        self.assertEqual(changed.chain_anchor, Path("new-input.png"))
        assert isinstance(changed.finish_state, H3ColorState)
        self.assertEqual(changed.finish_state.reanchor_reason, "prompt pair changed")
        keep_first = replace(all_i2av, color=ColorPolicy(anchor="first", reanchor="never", regions=False))
        unchanged = self.backend.prepare_run(keep_first, RunContext(2, other, Path("new-input.png"), stable, stable, pair, (), False))
        self.assertEqual(unchanged.chain_anchor, stable)
        assert isinstance(unchanged.finish_state, H3ColorState)
        self.assertIsNone(unchanged.finish_state.reanchor_reason)

    def _correcting_finish(self, *, correction_fails: bool) -> tuple[Any, RunRecord, list[MediaCheck]]:
        job = replace(self.job, color=ColorPolicy(anchor="previous", strength=1, regions=False))
        run = self._planned_run()
        _partial_path(run.output).write_bytes(b"raw child")
        record = self._record(run.output)
        checks: list[MediaCheck] = []
        source = H3Declarations(None, None, {"color_range": None, "color_space": None, "color_transfer": None, "color_primaries": None})
        rec709 = H3Declarations(DELIVERY, replace(DELIVERY, kind="vui"), {"color_range": "tv", "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709"})

        def convert(_source: Path, target: Path, _ffmpeg: str, _stop: Any) -> None:
            target.write_bytes(b"uncorrected delivery")

        def correct(_source: Path, target: Path, _ffmpeg: str, **_kwargs: Any) -> dict[str, Any]:
            if correction_fails:
                raise ValueError("synthetic correction failure")
            target.write_bytes(b"corrected delivery")
            return {"version": 1, "frames": 39}

        def extract(_source: Path, target: Path, _ffmpeg: str) -> None:
            Image.new("RGB", (64, 64)).save(target)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._file_sha256", return_value=H3_RGB_CONVENTION_SHA256))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._verify_h3_mp4", return_value=self._facts()))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.inspect_h3_declarations", side_effect=[source, rec709] + ([] if correction_fails else [rec709])))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.convert_h3_delivery", side_effect=convert))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.packet_facts", return_value=[(0, 0, 1, "SHA256:equal")]))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.measure_h3_drift", return_value={"first_to_last": {"lightness": 0}}))
            stack.enter_context(mock.patch("framecontrol.jobs.media.h3_correction.correct_h3_video", side_effect=correct))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=extract))
            result = self.backend.finish(job, run, record, lambda: None, H3ColorState(self.job.input, self.job.input, "initial i2av input"), MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), lambda _run, check: checks.append(check))
        self.assertIsNone(result)
        return run, record, checks

    def test_correction_promotes_corrected_main_and_retains_raw_and_uncorrected(self) -> None:
        run, record, checks = self._correcting_finish(correction_fails=False)
        self.assertEqual(run.output.read_bytes(), b"corrected delivery")
        self.assertEqual(raw_output_path(run.output).read_bytes(), b"raw child")
        self.assertEqual(uncorrected_output_path(run.output).read_bytes(), b"uncorrected delivery")
        self.assertEqual((record.output, record.corrected_output, record.raw_output, record.uncorrected_output), (run.output.name, run.output.name, raw_output_path(run.output).name, uncorrected_output_path(run.output).name))
        self.assertEqual(checks[0].facts["color"]["correction"]["status"], "succeeded")

    def test_failed_correction_promotes_uncorrected_main_and_retains_raw(self) -> None:
        run, record, checks = self._correcting_finish(correction_fails=True)
        self.assertEqual(run.output.read_bytes(), b"uncorrected delivery")
        self.assertEqual(raw_output_path(run.output).read_bytes(), b"raw child")
        self.assertFalse(uncorrected_output_path(run.output).exists())
        self.assertIsNone(record.corrected_output)
        self.assertEqual(checks[0].facts["color"]["correction"]["status"], "failed")
        self.assertEqual(checks[0].verdict, "warning")

    def test_existing_truthful_native_tag_survives_failed_preferred_conversion(self) -> None:
        run = self._planned_run()
        _partial_path(run.output).write_bytes(b"native child")
        record = self._record(run.output)
        checks: list[MediaCheck] = []
        native = H3Declarations(NATIVE, None, {"color_range": "tv", "color_space": "bt470bg", "color_transfer": "iec61966-2-1", "color_primaries": "bt709"})

        def extract(_source: Path, target: Path, _ffmpeg: str) -> None:
            Image.new("RGB", (64, 64)).save(target)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._file_sha256", return_value=H3_RGB_CONVENTION_SHA256))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._verify_h3_mp4", return_value=self._facts()))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.inspect_h3_declarations", return_value=native))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.convert_h3_delivery", side_effect=ValueError("conversion unavailable")))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.tag_video_color_codes", return_value=False))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.read_colr", return_value=NATIVE))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.packet_facts", return_value=[(0, 0, 1, "SHA256:equal")]))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.measure_h3_drift", return_value={"first_to_last": {"lightness": 0}}))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=extract))
            result = self.backend.finish(self.job, run, record, lambda: None, H3ColorState(self.job.input, self.job.input, "initial i2av input"), MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), lambda _run, check: checks.append(check))
        self.assertIsNone(result)
        self.assertEqual(run.output.read_bytes(), b"native child")
        self.assertEqual(checks[0].facts["color"]["delivery"], "native_tagged")
        self.assertTrue(run.last_frame is not None and run.last_frame.exists())
