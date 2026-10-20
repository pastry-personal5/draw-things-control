"""H3 declaration and Rec.709 finishing checks with a local synthetic H.264/AAC child."""

from __future__ import annotations

import shutil
import signal
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image

from framecontrol.jobs.backends.h3_finish import _extract_h3_frame
from framecontrol.jobs.media.color_stats import ColorStats, measure
from framecontrol.jobs.media.h3_color import DELIVERY, NATIVE, H3ColorCancelled, H3Declarations, convert_h3_delivery, declarations_match, inspect_h3_declarations, measure_h3_drift, packet_facts
from framecontrol.jobs.media.h3_correction import CLIPPED_PIXEL_CAP, H3Params, _params, apply_h3_params, correct_h3_video
from framecontrol.jobs.media.regions import Segmentation
from framecontrol.jobs.media.video_color import ColrTag, read_colr, read_colr_kind, tag_video_color_codes


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe required")
class H3ColorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.child = self.root / "child.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=24:duration=1.625", "-f", "lavfi", "-i", "anullsrc=r=32000:cl=stereo", "-frames:v", "39", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", str(self.child)], capture_output=True, check=True, timeout=30)

    def test_absent_native_and_explicit_delivery_declarations(self) -> None:
        source = inspect_h3_declarations(self.child, "ffmpeg", "ffprobe")
        self.assertIsNone(source.container)
        self.assertIsNone(source.bitstream)
        self.assertTrue(declarations_match(source, NATIVE, require_explicit=False))
        delivery = self.root / "delivery.mp4"
        convert_h3_delivery(self.child, delivery, "ffmpeg")
        tags = inspect_h3_declarations(delivery, "ffmpeg", "ffprobe")
        self.assertTrue(declarations_match(tags, DELIVERY, require_explicit=True))
        self.assertEqual(packet_facts(self.child, "ffprobe", "a:0"), packet_facts(delivery, "ffprobe", "a:0"))

    def test_native_tag_preserves_video_and_audio_packets(self) -> None:
        native = self.root / "native.mp4"
        shutil.copyfile(self.child, native)
        self.assertTrue(tag_video_color_codes(native, primaries=1, transfer=13, matrix=5, full_range=False))
        tags = inspect_h3_declarations(native, "ffmpeg", "ffprobe")
        self.assertEqual(tags.container, NATIVE)
        self.assertTrue(declarations_match(tags, NATIVE, require_explicit=False))
        for stream in ("v:0", "a:0"):
            self.assertEqual(packet_facts(self.child, "ffprobe", stream), packet_facts(native, "ffprobe", stream))

    def test_conflicting_child_container_is_rejected(self) -> None:
        conflicting = self.root / "conflict.mp4"
        shutil.copyfile(self.child, conflicting)
        self.assertTrue(tag_video_color_codes(conflicting, primaries=1, transfer=1, matrix=1, full_range=False))
        tags = inspect_h3_declarations(conflicting, "ffmpeg", "ffprobe")
        self.assertFalse(declarations_match(tags, NATIVE, require_explicit=False))

    def test_unsupported_child_color_box_is_not_treated_as_absent(self) -> None:
        unsupported = self.root / "unsupported.mp4"
        shutil.copyfile(self.child, unsupported)
        self.assertTrue(tag_video_color_codes(unsupported, primaries=1, transfer=13, matrix=5, full_range=False))
        data = unsupported.read_bytes()
        self.assertIn(b"nclx", data)
        unsupported.write_bytes(data.replace(b"nclx", b"prof", 1))
        self.assertIsNone(read_colr(unsupported))
        self.assertEqual(read_colr_kind(unsupported), "prof")
        declarations = H3Declarations(None, None, {"color_range": None, "color_space": None, "color_transfer": None, "color_primaries": None}, "prof")
        self.assertFalse(declarations_match(declarations, NATIVE, require_explicit=False))

    def test_conflicting_h264_vui_is_rejected_even_with_native_container_tag(self) -> None:
        rec709 = self.root / "rec709.mp4"
        convert_h3_delivery(self.child, rec709, "ffmpeg")
        container_native = self.root / "container-native.mp4"
        data = bytearray(rec709.read_bytes())
        position = data.index(b"nclx")
        data[position + 4 : position + 10] = struct.pack(">HHH", 1, 13, 5)
        container_native.write_bytes(data)
        declarations = inspect_h3_declarations(container_native, "ffmpeg", "ffprobe")
        self.assertEqual(declarations.container, NATIVE)
        self.assertEqual(declarations.bitstream, ColrTag("vui", 1, 1, 1, False))
        self.assertFalse(declarations_match(declarations, NATIVE, require_explicit=False))

    def test_handoff_is_last_frame_and_has_srgb_chunks(self) -> None:
        delivery = self.root / "delivery.mp4"
        convert_h3_delivery(self.child, delivery, "ffmpeg")
        handoff = self.root / "handoff.png"
        _extract_h3_frame(delivery, handoff, "ffmpeg")
        with Image.open(handoff) as image:
            self.assertEqual(image.size, (64, 64))
            self.assertIn("srgb", image.info)
            handoff_bytes = image.convert("RGB").tobytes()
        reference = self.root / "frame-38.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(delivery), "-vf", "select=eq(n\\,38)," + "colorspace=ispace=bt709:irange=tv:iprimaries=bt709:itrc=bt709:space=bt470bg:range=tv:primaries=bt709:trc=srgb:format=yuv420p,scale=in_color_matrix=bt470bg:in_range=tv:out_range=pc,format=rgb24,setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc", "-frames:v", "1", str(reference)], capture_output=True, check=True, timeout=30)
        with Image.open(reference) as image:
            self.assertEqual(handoff_bytes, image.convert("RGB").tobytes())

    def test_drift_reports_r2av_input_and_anchor_as_unavailable(self) -> None:
        drift = measure_h3_drift(self.child, 39, 64, 64, "ffmpeg", rec709_delivery=False, run_input=None, segment_anchor=None)
        self.assertIn("lightness", drift["first_to_last"])
        self.assertEqual(drift["input_to_first"], {"unavailable": "r2av has no first-frame input"})
        self.assertEqual(drift["anchor_to_last"], {"unavailable": "r2av has no i2av segment anchor"})

    def test_drift_cancellation_interrupts_decode(self) -> None:
        with self.assertRaises(H3ColorCancelled):
            measure_h3_drift(self.child, 39, 64, 64, "ffmpeg", rec709_delivery=False, run_input=None, segment_anchor=None, stop_requested=lambda: signal.SIGTERM)

    def test_correction_preserves_aac_and_rec709_declarations(self) -> None:
        prepared = self.root / "prepared.png"
        Image.new("RGB", (64, 64), (120, 80, 160)).save(prepared)
        corrected = self.root / "corrected.mp4"
        outcome = correct_h3_video(self.child, corrected, "ffmpeg", frames=39, width=64, height=64, run_input=prepared, segment_anchor=None, pull=0, strength=1, rec709_delivery=False, stop_requested=lambda: None)
        self.assertEqual((outcome["version"], outcome["frames"]), (1, 39))
        self.assertLessEqual(cast(float, outcome["peak_clipped_fraction"]), CLIPPED_PIXEL_CAP)
        self.assertTrue(declarations_match(inspect_h3_declarations(corrected, "ffmpeg", "ffprobe"), DELIVERY, require_explicit=True))
        self.assertEqual(packet_facts(self.child, "ffprobe", "a:0"), packet_facts(corrected, "ffprobe", "a:0"))

    def test_regions_use_person_and_background_when_segmenter_is_available(self) -> None:
        prepared = self.root / "prepared-regions.png"
        Image.new("RGB", (64, 64), (120, 80, 160)).save(prepared)

        def segment(frame: np.ndarray) -> Segmentation:
            person = np.zeros(frame.shape[:2], dtype=np.uint8)
            person[:, : frame.shape[1] // 2] = 255
            return Segmentation(person)

        corrected = self.root / "corrected-regions.mp4"
        outcome = correct_h3_video(self.child, corrected, "ffmpeg", frames=39, width=64, height=64, run_input=prepared, segment_anchor=None, pull=0, strength=1, rec709_delivery=False, stop_requested=lambda: None, regions=True, segmenter_factory=lambda: segment)
        self.assertEqual(outcome["regions"], ["people", "background"])
        self.assertIsNone(outcome["region_fallback"])
        self.assertEqual(packet_facts(self.child, "ffprobe", "a:0"), packet_facts(corrected, "ffprobe", "a:0"))
        drift = measure_h3_drift(self.child, 39, 64, 64, "ffmpeg", rec709_delivery=False, run_input=prepared, segment_anchor=None, regions=True, segmenter_factory=lambda: segment)
        self.assertIn("people", drift["regions"])
        self.assertIn("background", drift["regions"])

    def test_regions_fall_back_when_segmenter_is_missing(self) -> None:
        prepared = self.root / "prepared-no-regions.png"
        Image.new("RGB", (64, 64), (120, 80, 160)).save(prepared)
        corrected = self.root / "corrected-no-regions.mp4"
        outcome = correct_h3_video(self.child, corrected, "ffmpeg", frames=39, width=64, height=64, run_input=prepared, segment_anchor=None, pull=0, strength=1, rec709_delivery=False, stop_requested=lambda: None, regions=True, segmenter_factory=lambda: None)
        self.assertEqual(outcome["regions"], ["frame"])
        self.assertIsNotNone(outcome["region_fallback"])

    def test_correction_cancellation_leaves_no_output(self) -> None:
        prepared = self.root / "prepared.png"
        Image.new("RGB", (64, 64), (120, 80, 160)).save(prepared)
        corrected = self.root / "cancelled.mp4"
        with self.assertRaises(H3ColorCancelled):
            correct_h3_video(self.child, corrected, "ffmpeg", frames=39, width=64, height=64, run_input=prepared, segment_anchor=None, pull=0, strength=1, rec709_delivery=False, stop_requested=lambda: signal.SIGTERM)
        self.assertFalse(corrected.exists())

    def test_saturated_patches_stay_in_gamut_under_clipping_cap(self) -> None:
        swatches = np.asarray([(0, 0, 0), (1, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 1)], dtype=np.float32)
        frame = np.tile(swatches, (32, 32, 1))
        corrected, clipped, reduced = apply_h3_params(frame, measure(frame), H3Params(lightness=0.03, contrast=1.05, chroma=1.08, hue=np.deg2rad(4), cast_a=0.01))
        self.assertTrue(reduced)
        self.assertLessEqual(clipped, CLIPPED_PIXEL_CAP)
        self.assertTrue(np.isfinite(corrected).all())
        self.assertGreaterEqual(float(corrected.min()), 0)
        self.assertLessEqual(float(corrected.max()), 1)

    def test_skin_params_use_narrower_caps(self) -> None:
        current = ColorStats(100, (0.2, 0.3, 0.4), 0.1, 0.0, (0.0, 0.0))
        fitted = _params(current, (0.9, 0.5, 0.3, np.deg2rad(30), (0.1, 0.1)), 1, skin=True)
        self.assertLessEqual(abs(fitted.lightness), 0.015)
        self.assertLessEqual(fitted.chroma, 1.04)
        self.assertLessEqual(abs(np.rad2deg(fitted.hue)), 2)
        self.assertLessEqual(np.hypot(fitted.cast_a, fitted.cast_b), 0.005001)

    def test_rec709_handoff_preserves_dark_and_saturated_patch_centers(self) -> None:
        swatches = ((0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255))
        image = Image.new("RGB", (160, 64))
        for index, color in enumerate(swatches):
            for x in range(index * 32, (index + 1) * 32):
                for y in range(64):
                    image.putpixel((x, y), color)
        reference = self.root / "patches.png"
        image.save(reference)
        native = self.root / "native-patches.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-loop", "1", "-framerate", "24", "-i", str(reference), "-f", "lavfi", "-i", "anullsrc=r=32000:cl=stereo", "-frames:v", "24", "-vf", "scale=out_color_matrix=bt470bg:out_range=tv,format=yuv420p", "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-shortest", str(native)], capture_output=True, check=True, timeout=30)
        delivery = self.root / "rec709-patches.mp4"
        convert_h3_delivery(native, delivery, "ffmpeg")
        handoff = self.root / "patches-handoff.png"
        _extract_h3_frame(delivery, handoff, "ffmpeg")
        with Image.open(handoff) as result:
            for index, expected in enumerate(swatches):
                actual = cast(tuple[int, int, int], result.convert("RGB").getpixel((index * 32 + 16, 32)))
                self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected, strict=True)), 6)


if __name__ == "__main__":
    unittest.main()
