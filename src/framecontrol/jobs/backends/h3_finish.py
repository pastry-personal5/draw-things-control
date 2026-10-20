"""The staged H3 MP4 finishing workflow owned by the H3 backend."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framecontrol.core.exit_codes import exit_code_for_signal
from framecontrol.core.process.spawn import run as run_process
from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.events import RunStatus
from framecontrol.jobs.inputs.size import decode_image
from framecontrol.jobs.media.checks import MediaCheck
from framecontrol.jobs.media.h3_color import DELIVERY, DELIVERY_TO_SRGB_FILTER, H3_RGB_CONVENTION_SHA256, NATIVE, NATIVE_TO_SRGB_FILTER, H3ColorCancelled, convert_h3_delivery, declarations_match, inspect_h3_declarations, measure_h3_drift, packet_facts
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.media.video_color import read_colr, tag_video_color_codes
from framecontrol.jobs.output_naming import raw_output_path, uncorrected_output_path
from framecontrol.jobs.timing.timeline import NO_TIMER, StageTimer

if TYPE_CHECKING:
    from framecontrol.jobs.planning import PlannedRun
    from framecontrol.jobs.records import RunRecord

H3_FPS = 24
H3_COLOR_POLICY_VERSION = 1


@dataclass(frozen=True)
class H3OutputSpec:
    width: int
    height: int
    effective_frames: int


@dataclass(frozen=True)
class H3FinishColorState:
    run_input: Path | None
    segment_anchor: Path | None
    reanchor_reason: str | None


@dataclass(frozen=True)
class H3FinishRequest:
    executable: Path
    ffmpeg: str | None
    ffprobe: str | None
    output_spec: H3OutputSpec
    pipeline: str
    reference_pair: str | None
    references: tuple[dict[str, Any], ...]
    job: JobDefinition
    run: PlannedRun
    record: RunRecord
    stop_requested: Callable[[], signal.Signals | None]
    color_state: H3FinishColorState
    media: MediaTools
    report: Callable[[int, MediaCheck], None] | None
    timer: StageTimer = NO_TIMER


@dataclass(frozen=True)
class StagedH3Artifacts:
    """Every path the finishing workflow owns until promotion succeeds."""

    partial: Path
    handoff: Path | None
    delivery: Path
    native: Path
    raw: Path
    corrected: Path
    raw_output: Path | None
    uncorrected_output: Path | None

    @classmethod
    def for_request(cls, request: H3FinishRequest) -> StagedH3Artifacts:
        run = request.run
        raw_output = raw_output_path(run.output) if request.job.color.corrects and request.job.color.strength > 0 and request.pipeline == "i2av" else None
        return cls(
            partial=_partial_path(run.output),
            handoff=run.last_frame.with_name(f".{run.last_frame.name}.partial.png") if run.last_frame is not None else None,
            delivery=run.output.with_name(f".{run.output.name}.delivery.partial.mp4"),
            native=run.output.with_name(f".{run.output.name}.native.partial.mp4"),
            raw=run.output.with_name(f".{run.output.name}.raw.partial.mp4"),
            corrected=run.output.with_name(f".{run.output.name}.corrected.partial.mp4"),
            raw_output=raw_output,
            uncorrected_output=uncorrected_output_path(run.output) if raw_output is not None else None,
        )

    def cleanup_staged(self) -> None:
        for path in (self.partial, self.delivery, self.native, self.raw, self.corrected, self.handoff):
            if path is not None:
                path.unlink(missing_ok=True)

    def cleanup_promoted(self, run: PlannedRun) -> None:
        for path in (run.output, self.raw_output, self.uncorrected_output, run.last_frame):
            if path is not None:
                path.unlink(missing_ok=True)


@dataclass
class _FinishState:
    facts: dict[str, Any]
    color_facts: dict[str, Any]
    warnings: list[str]
    delivery: Path
    main_output: Path
    known_color: bool = False
    drift_warning: str | None = None


def finish_h3(request: H3FinishRequest) -> tuple[RunStatus, int] | None:
    """Verify, color-finish, promote, record, and report one H3 child MP4."""
    artifacts = StagedH3Artifacts.for_request(request)
    try:
        initial = _verify_source(request, artifacts)
        if isinstance(initial, tuple):
            return initial
        state = initial
        _interpret_source_color(request, artifacts, state)
        _copy_raw_child(request, artifacts, state)
        _select_delivery(request, artifacts, state)
        _measure_color_drift(request, artifacts, state)
        _apply_optional_correction(request, artifacts, state)
        _extract_handoff(request, artifacts, state)
        if (stop := request.stop_requested()) is not None:
            return RunStatus.INTERRUPTED, exit_code_for_signal(stop)
        _promote(request, artifacts, state)
        _record_and_report(request, artifacts, state)
        return None
    except H3ColorCancelled as cancelled:
        return RunStatus.INTERRUPTED, exit_code_for_signal(cancelled.received_signal)
    except (OSError, ValueError, subprocess.SubprocessError):
        artifacts.cleanup_promoted(request.run)
        return RunStatus.FAILED, 1
    finally:
        artifacts.cleanup_staged()


def _verify_source(request: H3FinishRequest, artifacts: StagedH3Artifacts) -> _FinishState | tuple[RunStatus, int]:
    try:
        if request.ffmpeg is None or request.ffprobe is None:
            raise ValueError("H3 media tools were not resolved")
        if (stop := request.stop_requested()) is not None:
            return RunStatus.INTERRUPTED, exit_code_for_signal(stop)
        with request.timer.stage("verify"):
            facts = _verify_h3_mp4(artifacts.partial, request.output_spec, request.ffprobe)
    except (OSError, ValueError, subprocess.SubprocessError):
        return RunStatus.FAILED, 1
    color_facts: dict[str, Any] = {
        "policy_version": H3_COLOR_POLICY_VERSION,
        "policy": request.job.color.as_dict(),
        "source_executable_sha256": None,
        "source_declarations": None,
        "source_interpretation": "unavailable",
        "delivery": "original",
        "segment_anchor": request.color_state.segment_anchor.name if request.color_state.segment_anchor is not None else None,
        "reanchor_reason": request.color_state.reanchor_reason,
    }
    return _FinishState(facts, color_facts, [], artifacts.partial, artifacts.partial)


def _interpret_source_color(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    assert request.ffmpeg is not None and request.ffprobe is not None
    try:
        with request.timer.stage("verify"):
            fingerprint = _file_sha256(request.executable)
            state.color_facts["source_executable_sha256"] = fingerprint
            declarations = inspect_h3_declarations(artifacts.partial, request.ffmpeg, request.ffprobe)
        state.color_facts["source_declarations"] = declarations.facts()
        declared = ", ".join(part for part in (f"container {declarations.container.text()}" if declarations.container else f"container {declarations.unsupported_container_kind}" if declarations.unsupported_container_kind else "", f"H.264 {declarations.bitstream.text()}" if declarations.bitstream else "") if part)
        if fingerprint != H3_RGB_CONVENTION_SHA256:
            state.warnings.append("H3 color interpretation is unavailable for this executable fingerprint; original MP4 retained" + (f" with existing {declared}" if declared else " without new tags"))
        elif not declarations_match(declarations, NATIVE, require_explicit=False):
            state.warnings.append(f"H3 child has conflicting color declarations ({declared or 'stream tags'}); original MP4 and those tags retained, with no safe handoff")
        else:
            state.known_color = True
            state.color_facts["source_interpretation"] = "assumed sRGB transfer/BT.709 primaries, measured BT.601 limited matrix/range"
    except (OSError, ValueError, subprocess.SubprocessError):
        state.warnings.append("H3 color interpretation is unavailable: executable or declaration inspection failed; original MP4 retained")


def _copy_raw_child(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    if artifacts.raw_output is None:
        return
    with request.timer.stage("copy_raw"):
        shutil.copyfile(artifacts.partial, artifacts.raw)
        if _file_sha256(artifacts.raw) != _file_sha256(artifacts.partial):
            raise ValueError("H3 raw child copy did not match the child MP4")
    state.color_facts["raw_output"] = artifacts.raw_output.name


def _select_delivery(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    if not state.known_color:
        return
    assert request.ffmpeg is not None and request.ffprobe is not None
    with request.timer.stage("convert"):
        try:
            convert_h3_delivery(artifacts.partial, artifacts.delivery, request.ffmpeg, request.stop_requested)
            converted_facts = _verify_h3_mp4(artifacts.delivery, request.output_spec, request.ffprobe)
            converted_tags = inspect_h3_declarations(artifacts.delivery, request.ffmpeg, request.ffprobe)
            if not declarations_match(converted_tags, DELIVERY, require_explicit=True):
                raise ValueError("Rec.709 delivery declarations are absent or conflicting")
            if packet_facts(artifacts.partial, request.ffprobe, "a:0") != packet_facts(artifacts.delivery, request.ffprobe, "a:0"):
                raise ValueError("Rec.709 delivery changed native AAC packets or timing")
            state.delivery, state.main_output, state.facts = artifacts.delivery, artifacts.delivery, converted_facts
            state.color_facts["delivery"] = "converted_rec709"
            state.color_facts["delivery_declarations"] = converted_tags.facts()
            return
        except (OSError, ValueError, subprocess.SubprocessError):
            artifacts.delivery.unlink(missing_ok=True)
            state.warnings.append("H3 Rec.709 delivery conversion or verification failed")
        try:
            shutil.copyfile(artifacts.partial, artifacts.native)
            added_tag = tag_video_color_codes(artifacts.native, primaries=1, transfer=13, matrix=5, full_range=False)
            if not added_tag and read_colr(artifacts.native) != NATIVE:
                raise ValueError("H3 native color tag could not be added")
            native_tags = inspect_h3_declarations(artifacts.native, request.ffmpeg, request.ffprobe)
            if native_tags.container != NATIVE or not declarations_match(native_tags, NATIVE, require_explicit=False):
                raise ValueError("H3 native color tag failed verification")
            if any(packet_facts(artifacts.partial, request.ffprobe, stream) != packet_facts(artifacts.native, request.ffprobe, stream) for stream in ("v:0", "a:0")):
                raise ValueError("H3 native tagging changed media packets or timing")
            state.delivery = state.main_output = artifacts.native
            state.color_facts["delivery"] = "native_tagged"
            state.color_facts["delivery_declarations"] = native_tags.facts()
        except (OSError, ValueError, subprocess.SubprocessError):
            artifacts.native.unlink(missing_ok=True)
            state.warnings.append("H3 native color tagging or verification failed; original MP4 retained")


def _measure_color_drift(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    if state.known_color:
        assert request.ffmpeg is not None
        try:
            with request.timer.stage("drift"):
                state.color_facts["drift"] = measure_h3_drift(state.delivery, state.facts["frames"], state.facts["width"], state.facts["height"], request.ffmpeg, rec709_delivery=state.delivery == artifacts.delivery, run_input=request.color_state.run_input, segment_anchor=request.color_state.segment_anchor, stop_requested=request.stop_requested, regions=request.job.color.regions, segmenter_factory=request.media.segmenter_factory)
        except (OSError, ValueError, subprocess.SubprocessError):
            state.drift_warning = "H3 color drift measurement is unavailable: frame decode or measurement failed"
            state.color_facts["drift"] = {"unavailable": "frame decode or measurement failed"}
    else:
        state.drift_warning = "H3 color drift measurement is unavailable: source pixel interpretation is unknown"
        state.color_facts["drift"] = {"unavailable": "source pixel interpretation is unknown"}
    if state.drift_warning is not None:
        state.warnings.append(state.drift_warning)


def _apply_optional_correction(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    state.color_facts["correction"] = {"status": "not_applicable", "reason": "r2av has no first-frame input"} if request.pipeline == "r2av" else {"status": "off"}
    if request.pipeline == "i2av" and request.job.color.corrects and request.job.color.strength == 0:
        state.color_facts["correction"] = {"status": "measured_identity", "strength": 0}
    if artifacts.raw_output is None:
        return
    if not state.known_color:
        state.color_facts["correction"] = {"status": "skipped", "reason": "unknown source pixel interpretation"}
        state.warnings.append("H3 color correction was skipped: source pixel interpretation is unknown")
        return
    if request.color_state.run_input is None:
        state.color_facts["correction"] = {"status": "skipped", "reason": "prepared i2av input unavailable"}
        state.warnings.append("H3 color correction was skipped: prepared i2av input unavailable")
        return
    assert request.ffmpeg is not None and request.ffprobe is not None
    try:
        from framecontrol.jobs.media.h3_correction import correct_h3_video

        with request.timer.stage("correct"):
            correction_facts = correct_h3_video(state.delivery, artifacts.corrected, request.ffmpeg, frames=state.facts["frames"], width=state.facts["width"], height=state.facts["height"], run_input=request.color_state.run_input, segment_anchor=request.color_state.segment_anchor, pull=request.job.color.pull, strength=request.job.color.strength, rec709_delivery=state.delivery == artifacts.delivery, stop_requested=request.stop_requested, regions=request.job.color.regions, segmenter_factory=request.media.segmenter_factory)
            corrected_facts = _verify_h3_mp4(artifacts.corrected, request.output_spec, request.ffprobe)
            corrected_tags = inspect_h3_declarations(artifacts.corrected, request.ffmpeg, request.ffprobe)
            if not declarations_match(corrected_tags, DELIVERY, require_explicit=True):
                raise ValueError("corrected H3 MP4 has missing or conflicting Rec.709 declarations")
            if packet_facts(state.delivery, request.ffprobe, "a:0") != packet_facts(artifacts.corrected, request.ffprobe, "a:0"):
                raise ValueError("corrected H3 MP4 changed native AAC packets or timing")
        state.main_output, state.facts = artifacts.corrected, corrected_facts
        state.color_facts["correction"] = {"status": "succeeded", **correction_facts}
        state.color_facts["corrected_declarations"] = corrected_tags.facts()
        if correction_facts.get("region_fallback") is not None:
            state.warnings.append("H3 region correction is unavailable; verified whole-frame fallback used")
        state.color_facts["corrected_output"] = request.run.output.name
        if artifacts.uncorrected_output is not None:
            state.color_facts["uncorrected_output"] = artifacts.uncorrected_output.name
    except (OSError, ValueError, subprocess.SubprocessError):
        artifacts.corrected.unlink(missing_ok=True)
        state.color_facts["correction"] = {"status": "failed", "reason": "correction or audiovisual verification failed"}
        state.warnings.append("H3 color correction failed; verified uncorrected delivery retained")


def _extract_handoff(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    if artifacts.handoff is None or not state.known_color:
        return
    assert request.ffmpeg is not None
    try:
        with request.timer.stage("last_frame"):
            _extract_h3_frame(state.main_output, artifacts.handoff, request.ffmpeg)
            decode_image(artifacts.handoff)
    except (OSError, ValueError, subprocess.SubprocessError):
        state.warnings.append("H3 handoff is unavailable: extraction or image validation failed")
        artifacts.handoff.unlink(missing_ok=True)


def _promote(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    run, record = request.run, request.record
    with request.timer.stage("promote"):
        if artifacts.raw_output is not None:
            os.replace(artifacts.raw, artifacts.raw_output)
            record.raw_output = artifacts.raw_output.name
        if state.main_output == artifacts.corrected and artifacts.uncorrected_output is not None:
            os.replace(state.delivery, artifacts.uncorrected_output)
            record.uncorrected_output = artifacts.uncorrected_output.name
            record.corrected_output = run.output.name
        os.replace(state.main_output, run.output)
        if artifacts.handoff is not None and artifacts.handoff.is_file() and run.last_frame is not None:
            try:
                os.replace(artifacts.handoff, run.last_frame)
                record.last_frame = run.last_frame.name
            except OSError:
                state.warnings.append("H3 handoff is unavailable: promotion failed")
                artifacts.handoff.unlink(missing_ok=True)


def _record_and_report(request: H3FinishRequest, artifacts: StagedH3Artifacts, state: _FinishState) -> None:
    run, record = request.run, request.record
    corrected = record.corrected_output is not None
    state.color_facts["artifacts"] = {"main": {"basename": run.output.name, "role": "corrected" if corrected else "uncorrected"}, "raw_child": artifacts.raw_output.name if record.raw_output is not None and artifacts.raw_output is not None else None, "uncorrected_delivery": artifacts.uncorrected_output.name if record.uncorrected_output is not None and artifacts.uncorrected_output is not None else None, "handoff": record.last_frame}
    state.color_facts["handoff_source"] = "corrected_main" if corrected and record.last_frame is not None else "uncorrected_main" if record.last_frame is not None else None
    record.output_width, record.output_height, record.output_frames = state.facts["width"], state.facts["height"], state.facts["frames"]
    record.output_frame_rate, record.output_duration = state.facts["frame_rate"], state.facts["video_duration"]
    record.audio_codec, record.audio_sample_rate, record.audio_channels, record.audio_duration = state.facts["audio_codec"], state.facts["audio_sample_rate"], state.facts["audio_channels"], state.facts["audio_duration"]
    if request.report is None:
        return
    facts = state.facts
    facts["pipeline"] = request.pipeline
    facts["reference_pair"] = request.reference_pair
    facts["references"] = list(request.references)
    facts["handoff_for_run"] = run.number + 1 if record.last_frame is not None else None
    facts["color"] = state.color_facts
    role_notes = tuple(f"{label}: {name}" for label, name in (("raw child", record.raw_output), ("uncorrected delivery", record.uncorrected_output), ("main corrected output", run.output.name if corrected else None), ("next handoff", record.last_frame)) if name is not None)
    summary = "H3 corrected H.264/AAC MP4 verified" if corrected else "H3 H.264/AAC MP4 verified"
    request.report(run.number, MediaCheck("output", run.output.name, summary, warnings=tuple(state.warnings), notes=role_notes, facts=facts))
    request.report(run.number, MediaCheck("color_drift", run.output.name, "H3 within-run and segment color drift", warnings=(state.drift_warning,) if state.drift_warning is not None else (), facts={"drift": state.color_facts["drift"], "segment_anchor": state.color_facts["segment_anchor"], "reanchor_reason": state.color_facts["reanchor_reason"]}))
    if request.job.color.corrects:
        correction_status = state.color_facts["correction"]["status"]
        correction_warnings = tuple(warning for warning in state.warnings if "color correction" in warning or "region correction" in warning)
        request.report(run.number, MediaCheck("color_correction", run.output.name, f"H3 color correction {correction_status}", warnings=correction_warnings, facts={"correction": state.color_facts["correction"], "artifacts": state.color_facts["artifacts"], "handoff_source": state.color_facts["handoff_source"]}))


def _partial_path(output: Path) -> Path:
    return output.with_name(f".{output.name}.partial.mp4")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_h3_mp4(path: Path, spec: H3OutputSpec, ffprobe: str) -> dict[str, Any]:
    command = [ffprobe, "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames,sample_rate,channels,duration", "-of", "json", str(path)]
    result = run_process(command, capture_output=True, text=True, errors="replace", timeout=30, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "ffprobe failed")
    streams = json.loads(result.stdout).get("streams")
    if not isinstance(streams, list) or len(streams) != 2:
        raise ValueError("H3 output must have exactly one video and one audio stream")
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise ValueError("H3 output must have exactly one video and one audio stream")
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise ValueError("H3 output must use H.264 video and AAC audio")
    if (video.get("width"), video.get("height")) != (spec.width, spec.height):
        raise ValueError("H3 output dimensions differ from requested dimensions")
    numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/", 1)
    if float(numerator) / float(denominator) != H3_FPS:
        raise ValueError("H3 output is not 24 fps")
    frames = int(video.get("nb_read_frames", 0))
    if frames != spec.effective_frames:
        raise ValueError("H3 output frame count differs from effective frame count")
    if str(audio.get("sample_rate")) != "32000" or audio.get("channels") != 2:
        raise ValueError("H3 output audio must be 32 kHz stereo")
    video_duration, audio_duration = float(video["duration"]), float(audio["duration"])
    if not math.isfinite(video_duration) or not math.isfinite(audio_duration) or abs(video_duration - audio_duration) > 1 / H3_FPS:
        raise ValueError("H3 audio and video durations are not synchronized")
    return {"width": spec.width, "height": spec.height, "frames": frames, "frame_rate": H3_FPS, "video_duration": video_duration, "audio_codec": "aac", "audio_sample_rate": 32000, "audio_channels": 2, "audio_duration": audio_duration}


def _extract_h3_frame(video: Path, target: Path, ffmpeg: str) -> None:
    if target.exists():
        raise ValueError(f"handoff already exists: {target}")
    tag = read_colr(video)
    color_filter = DELIVERY_TO_SRGB_FILTER if tag == DELIVERY else NATIVE_TO_SRGB_FILTER
    color_filter += ",setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc"
    result = run_process([ffmpeg, "-v", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-vf", color_filter, str(target)], capture_output=True, text=True, errors="replace", timeout=30, check=False)
    if result.returncode or not target.is_file():
        raise ValueError(result.stderr.strip() or "could not extract H3 final frame")
