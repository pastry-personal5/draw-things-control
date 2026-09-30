"""Tests for correcting a run's colors with ffmpeg: the corrected copy, the corrected handoff, and a failed correction."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from draw_things_control.jobs.color_run import ColorCorrector, CorrectionRequest
from draw_things_control.jobs.definition import ColorPolicy
from draw_things_control.jobs.media.checks import MediaChecker
from draw_things_control.jobs.media.clip_frames import probe_clip
from draw_things_control.jobs.media.color_stats import compare, measure
from draw_things_control.jobs.media.frames import decode_filter, extract_last_frame
from draw_things_control.jobs.media.stream_color import StreamColor
from tests.jobs.test_media_correction import drifted, scene

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
BT709 = StreamColor("bt709", "tv")
PRORES = ("-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuv444p10le")
H264 = ("-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p")
FRAMES = 9


def samples_of(path: Path, decode: str | None = None) -> np.ndarray:
    """A PNG's or a video's last frame as 16-bit RGB samples, decoded plainly (no half level added)."""
    command = ["ffmpeg", "-v", "error", "-sseof", "-1", "-i", str(path), "-update", "1"] if path.suffix != ".png" else ["ffmpeg", "-v", "error", "-i", str(path)]
    filters = ["-vf", decode] if decode else []
    raw = subprocess.run([*command, *filters, "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype="<u2")
    return samples[-96 * 64 * 3 :].reshape(64, 96, 3)


@unittest.skipUnless(FFMPEG and FFPROBE, "needs ffmpeg and ffprobe")
class ColorCorrectorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = scene(size=96)[:64]
        self.input = self.root / "input.png"
        Image.fromarray(np.clip(np.rint(self.base * 255), 0, 255).astype(np.uint8), "RGB").save(self.input)
        self.checker = MediaChecker(lambda: FFMPEG)

    def drifting_clip(self, name: str, codec: tuple[str, ...]) -> Path:
        """A clip that drifts from the input over its frames, written as Draw Things writes: truncated to 8 bits, BT.709."""
        frames = [drifted(self.base, lightness=0.02 * share, chroma=1 + 0.08 * share, hue_degrees=4 * share) for share in np.linspace(0, 1, FRAMES)]
        raw = b"".join(np.floor(frame * 255 + 1e-9).clip(0, 255).astype(np.uint8).tobytes() for frame in frames)
        video = self.root / name
        subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "96x64", "-r", "16", "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *codec, str(video)], input=raw, check=True)
        return video

    def request(self, video: Path, policy: ColorPolicy, anchor: Path | None = None) -> CorrectionRequest:
        return CorrectionRequest(video=video, color=BT709, run_input=self.input, handoff=video.with_name(f"{video.stem}-last-frame.png"), copy=video.with_name(f"{video.stem}-cc{video.suffix}"), policy=policy, anchor=anchor, first_image=self.input)

    def test_a_prores_run_gets_a_prores_copy_and_a_handoff_from_its_corrected_last_frame(self) -> None:
        video = self.drifting_clip("run.mov", PRORES)
        original = hashlib.sha256(video.read_bytes()).hexdigest()
        request = self.request(video, ColorPolicy(anchor="first"), anchor=self.input)
        result = ColorCorrector(lambda: FFMPEG, self.checker)(request)
        self.assertTrue(result.succeeded, result.checks)
        assert result.drift is not None
        self.assertEqual([check.stage for check in (result.drift, *result.checks)], ["color_drift", "output", "color_correction"])
        # The original is untouched; the copy is in its format, with its frame count and rate.
        self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), original)
        info = probe_clip(request.copy, str(FFPROBE))
        self.assertEqual((info.tag, info.frames, info.rate), ("ap4h", FRAMES, "16/1"))
        # Its pixels measure BT.709, and its frame header and colr box state it.
        output = result.checks[0]
        self.assertEqual((output.facts["measured_matrix"], output.facts["color_space"], output.facts["color_primaries"]), ("bt709", "bt709", "bt709"))
        self.assertEqual((output.facts["colr"]["primaries"], output.facts["colr"]["transfer"], output.facts["colr"]["matrix"]), (1, 13, 1))
        # The copy's last frame, decoded plainly, is the handoff within 1 level (its values are the handoff's).
        copy_levels = samples_of(request.copy, decode_filter(BT709)).astype(np.float64) / 256
        handoff_levels = (samples_of(request.handoff) >> 8).astype(np.float64)
        self.assertLessEqual(float(np.max(np.abs(copy_levels - handoff_levels))), 1.0)
        self.assertLess(abs(float(np.mean(copy_levels - handoff_levels))), 0.05)
        # The drift the clip added is gone from the last frame: held to the input, which is also the anchor.
        left = compare(measure(self.base), measure(handoff_levels / 255))
        self.assertLess(abs(left.lightness), 0.5)
        self.assertAlmostEqual(left.chroma, 1.0, delta=0.02)
        correction = result.checks[-1]
        self.assertIn("first toward input.png", correction.summary)
        self.assertEqual(correction.facts["frames"], FRAMES)

    def test_the_copys_frame_0_is_no_further_from_the_input_than_the_originals(self) -> None:
        video = self.drifting_clip("run.mov", PRORES)
        request = self.request(video, ColorPolicy(anchor="previous"))
        self.assertTrue(ColorCorrector(lambda: FFMPEG, self.checker)(request).succeeded)
        target = measure(self.base)

        def distance(path: Path) -> float:
            first = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", decode_filter(BT709), "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"], capture_output=True, check=True).stdout
            drift = compare(target, measure(np.frombuffer(first, dtype="<u2").reshape(64, 96, 3).astype(np.float64) / 256 / 255))
            return abs(drift.lightness) / 100 + abs(drift.chroma - 1) + abs(drift.contrast - 1)

        self.assertLessEqual(distance(request.copy), distance(video) + 0.005)

    def test_an_h264_run_gets_an_h264_copy(self) -> None:
        video = self.drifting_clip("run.mp4", H264)
        request = self.request(video, ColorPolicy(anchor="blend"), anchor=self.input)
        result = ColorCorrector(lambda: FFMPEG, self.checker)(request)
        self.assertTrue(result.succeeded, result.checks)
        info = probe_clip(request.copy, str(FFPROBE))
        self.assertEqual((info.codec, info.frames), ("h264", FRAMES))
        output = result.checks[0]
        self.assertEqual((output.facts["color_primaries"], output.facts["color_transfer"]), ("bt709", "iec61966-2-1"))

    def test_strength_0_hands_off_exactly_what_milestone_08s_extraction_does(self) -> None:
        video = self.drifting_clip("run.mov", PRORES)
        request = self.request(video, ColorPolicy(anchor="first", strength=0.0), anchor=self.input)
        self.assertTrue(ColorCorrector(lambda: FFMPEG, self.checker)(request).succeeded)
        extracted = self.root / "extracted.png"
        extract_last_frame(video, extracted, BT709)
        np.testing.assert_array_equal(samples_of(request.handoff), samples_of(extracted))

    def test_a_failed_correction_leaves_no_copy_and_no_handoff_and_warns(self) -> None:
        video = self.drifting_clip("run.mov", PRORES)
        request = self.request(video, ColorPolicy(anchor="previous"))
        with mock.patch("draw_things_control.jobs.color_run.available_encoders", return_value=set()):
            result = ColorCorrector(lambda: FFMPEG, self.checker)(request)
        self.assertFalse(result.succeeded)
        self.assertFalse(request.copy.exists())
        self.assertFalse(request.handoff.exists())
        [failure] = result.checks
        self.assertEqual((failure.stage, failure.verdict), ("color_correction", "warning"))
        self.assertIn("The correction failed (ffmpeg has none of prores_videotoolbox, prores_ks)", failure.warnings[0])
        # The first pass finished, so the run's drift is still measured.
        assert result.drift is not None
        self.assertEqual(result.drift.stage, "color_drift")

    def test_without_ffmpeg_the_correction_fails_before_reading_anything(self) -> None:
        video = self.drifting_clip("run.mov", PRORES)
        result = ColorCorrector(lambda: None, self.checker)(self.request(video, ColorPolicy(anchor="previous")))
        self.assertFalse(result.succeeded)
        self.assertIsNone(result.drift)


if __name__ == "__main__":
    unittest.main()
