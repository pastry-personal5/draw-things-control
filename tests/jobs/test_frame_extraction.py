"""Tests for last-frame extraction and its color tags."""

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from draw_things_control.jobs import frame_extraction
from draw_things_control.jobs.frame_extraction import extract_last_frame, srgb_filter


def png_chunks(path: Path) -> list[str]:
    data = path.read_bytes()
    names, offset = [], 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        names.append(data[offset + 4 : offset + 8].decode())
        offset += 12 + length
    return names


def raw_pixels(path: Path) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba64le", "-"], capture_output=True, check=True).stdout


class ExtractionCommandTests(unittest.TestCase):
    def extract(self, probe_output: str) -> list[str]:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_options: object):
            calls.append(command)
            return mock.Mock(returncode=0, stdout=probe_output, stderr="")

        with mock.patch.object(frame_extraction, "require_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(frame_extraction.shutil, "which", return_value="/usr/bin/ffprobe"), mock.patch.object(frame_extraction.subprocess, "run", side_effect=fake_run), tempfile.TemporaryDirectory() as directory:
            png = Path(directory) / "frame.png"
            png.write_bytes(b"png")
            extract_last_frame(Path("clip.mov"), png)
        return calls[-1]

    def test_the_command_converts_then_labels_the_frame_and_never_overwrites(self) -> None:
        command = self.extract("smpte170m\n")
        filter_chain = command[command.index("-vf") + 1]
        self.assertEqual(filter_chain, srgb_filter(True))
        self.assertIn("-n", command)
        # The conversion to RGB comes before the label, or the label would change the pixels.
        self.assertLess(filter_chain.index("format="), filter_chain.index("setparams="))

    def test_a_video_with_no_matrix_tag_is_decoded_as_bt709_limited_range(self) -> None:
        for probe_output in ("unknown\n", "unspecified\n", "\n"):
            with self.subTest(probe_output):
                filter_chain = self.extract(probe_output)[self.extract(probe_output).index("-vf") + 1]
                self.assertTrue(filter_chain.startswith("scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format="))
        self.assertNotIn("scale=", self.extract("bt709\n")[self.extract("bt709\n").index("-vf") + 1])

    def test_a_failed_extraction_is_reported(self) -> None:
        with mock.patch.object(frame_extraction, "require_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(frame_extraction.shutil, "which", return_value=None), mock.patch.object(frame_extraction.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="boom", stdout="")):
            with self.assertRaisesRegex(ValueError, "boom"):
                extract_last_frame(Path("clip.mov"), Path("/nonexistent/frame.png"))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class ExtractionWithFfmpegTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def make_video(self, pixel_format: str, codec: str, *, tagged: bool) -> Path:
        video = self.root / f"{pixel_format}-{'tagged' if tagged else 'untagged'}.mov"
        color = ["-colorspace", "smpte170m", "-color_range", "tv"] if tagged else []
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5:duration=1", "-c:v", codec, "-pix_fmt", pixel_format, *color, str(video)], check=True)
        return video

    def reference(self, video: Path, name: str, decode: str) -> Path:
        """The last frame with only ``decode`` (a scale filter, or nothing) and the format list: the pixels the label must not change."""
        path = self.root / name
        formats = "format=pix_fmts=rgb24|rgba|rgb48be|rgba64be"
        subprocess.run(["ffmpeg", "-v", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-q:v", "1", "-vf", f"{decode},{formats}" if decode else formats, str(path)], check=True)
        return path

    def test_the_png_is_labeled_srgb_at_the_depth_of_the_source_without_changing_any_pixel(self) -> None:
        for pixel_format, codec, depth in (("yuva444p12le", "prores_ks", 16), ("yuv420p", "libx264", 8)):
            with self.subTest(pixel_format):
                video = self.make_video(pixel_format, codec, tagged=True)
                tagged = self.root / f"{pixel_format}-labeled.png"
                extract_last_frame(video, tagged)
                plain = self.reference(video, f"{pixel_format}-plain.png", "")
                self.assertTrue({"sRGB", "cHRM", "gAMA"} <= set(png_chunks(tagged)))
                self.assertFalse({"sRGB", "cHRM", "gAMA", "iCCP"} & set(png_chunks(plain)))
                self.assertEqual(tagged.read_bytes()[24], depth)
                self.assertEqual(plain.read_bytes()[24], depth)
                # The video's own matrix (BT.601 here) is honored, and the label changes no pixel.
                self.assertEqual(raw_pixels(tagged), raw_pixels(plain))

    def test_an_untagged_video_is_decoded_as_bt709_limited_range_not_ffmpegs_bt601_default(self) -> None:
        video = self.make_video("yuv420p", "libx264", tagged=False)
        extracted = self.root / "untagged-labeled.png"
        extract_last_frame(video, extracted)
        expected = self.reference(video, "untagged-709.png", "scale=in_color_matrix=bt709:in_range=tv:out_range=pc")
        default = self.reference(video, "untagged-default.png", "")
        self.assertEqual(raw_pixels(extracted), raw_pixels(expected))
        self.assertNotEqual(raw_pixels(extracted), raw_pixels(default))
        self.assertIn("sRGB", png_chunks(extracted))
