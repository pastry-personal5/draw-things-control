"""Tests for last-frame extraction and its color tags."""

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from draw_things_control.jobs.media import frames as frame_extraction
from draw_things_control.jobs.media import stream_color, tools
from draw_things_control.jobs.media.frames import extract_last_frame
from draw_things_control.jobs.media.stream_color import StreamColor, read_stream_color, resolve_video_color
from draw_things_control.jobs.media.video_color import tag_video_colors


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
    def extract(self, color: StreamColor | None, probe_output: str = "{}") -> list[str]:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_options: object):
            calls.append(command)
            return mock.Mock(returncode=0, stdout=probe_output, stderr="")

        with mock.patch.object(frame_extraction, "require_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(tools.shutil, "which", return_value="/usr/bin/ffprobe"), mock.patch.object(frame_extraction.subprocess, "run", side_effect=fake_run), mock.patch.object(stream_color.subprocess, "run", side_effect=fake_run), tempfile.TemporaryDirectory() as directory:
            png = Path(directory) / "frame.png"
            png.write_bytes(b"png")
            extract_last_frame(Path("clip.mov"), png, color)
        return calls[-1]

    def filter_chain(self, color: StreamColor | None, probe_output: str = "{}") -> str:
        command = self.extract(color, probe_output)
        return command[command.index("-vf") + 1]

    def test_the_command_decodes_with_the_color_converts_without_alpha_then_labels_and_never_overwrites(self) -> None:
        command = self.extract(StreamColor("smpte170m", "tv"))
        filter_chain = command[command.index("-vf") + 1]
        # Every source gives the 16-bit handoff, and the format is set again after geq.
        self.assertEqual(filter_chain, "scale=in_color_matrix=smpte170m:in_range=tv:out_range=pc:flags=accurate_rnd+full_chroma_int," + frame_extraction.HANDOFF + "," + frame_extraction.SRGB_LABEL)
        self.assertTrue(frame_extraction.HANDOFF.endswith(",format=pix_fmts=rgb48be"))
        self.assertIn("-n", command)
        # The conversion to RGB comes before the label, or the label would change the pixels.
        self.assertLess(filter_chain.index("format="), filter_chain.index("setparams="))

    def test_the_matrix_and_range_follow_the_stream_and_default_to_bt709_limited(self) -> None:
        self.assertTrue(self.filter_chain(StreamColor()).startswith("scale=in_color_matrix=bt709:in_range=tv:out_range=pc:"))
        self.assertTrue(self.filter_chain(StreamColor("bt709", "pc")).startswith("scale=in_color_matrix=bt709:in_range=pc:out_range=pc:"))
        self.assertTrue(self.filter_chain(StreamColor("bt2020nc", None)).startswith("scale=in_color_matrix=bt2020:in_range=tv:out_range=pc:"))
        # A matrix this does not know is left to ffmpeg, which decodes with what the frame states, still accurately.
        self.assertTrue(self.filter_chain(StreamColor("ycgco", "tv")).startswith("scale=out_range=pc:flags=accurate_rnd+full_chroma_int," + frame_extraction.HANDOFF))

    def test_without_a_color_the_stream_is_read_first(self) -> None:
        stated = json.dumps({"frames": [{"color_space": "smpte170m", "color_range": "tv"}]})
        self.assertTrue(self.filter_chain(None, stated).startswith("scale=in_color_matrix=smpte170m:in_range=tv:"))
        for unstated in ('{"frames": [{}]}', '{"frames": [{"color_space": "unknown"}]}', "not json"):
            with self.subTest(unstated):
                self.assertTrue(self.filter_chain(None, unstated).startswith("scale=in_color_matrix=bt709:in_range=tv:"))

    def test_a_failed_extraction_is_reported(self) -> None:
        with mock.patch.object(frame_extraction, "require_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(frame_extraction.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="boom", stdout="")):
            with self.assertRaisesRegex(ValueError, "boom"):
                extract_last_frame(Path("clip.mov"), Path("/nonexistent/frame.png"), StreamColor())


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
        """The last frame with only ``decode`` (a scale filter's options) and the handoff: the pixels the label must not change."""
        path = self.root / name
        filters = f"scale={decode}:flags={frame_extraction.DECODE_FLAGS},{frame_extraction.HANDOFF}"
        subprocess.run(["ffmpeg", "-v", "error", "-n", "-sseof", "-1", "-i", str(video), "-update", "1", "-q:v", "1", "-vf", filters, str(path)], check=True)
        return path

    def test_the_png_is_16_bit_rgb_without_alpha_labeled_srgb_from_every_source_without_changing_any_pixel(self) -> None:
        for pixel_format, codec in (("yuva444p12le", "prores_ks"), ("yuv420p", "libx264")):
            with self.subTest(pixel_format):
                video = self.make_video(pixel_format, codec, tagged=True)
                tagged = self.root / f"{pixel_format}-labeled.png"
                extract_last_frame(video, tagged)
                color = resolve_video_color(video, shutil.which("ffmpeg"), shutil.which("ffprobe"))
                plain = self.reference(video, f"{pixel_format}-plain.png", f"in_color_matrix={color.decode_matrix}:in_range=tv:out_range=pc")
                self.assertTrue({"sRGB", "cHRM", "gAMA"} <= set(png_chunks(tagged)))
                self.assertFalse({"sRGB", "cHRM", "gAMA", "iCCP"} & set(png_chunks(plain)))
                # Bit depth, then color type 2: 16-bit RGB, no alpha, from either source.
                self.assertEqual((tagged.read_bytes()[24], tagged.read_bytes()[25]), (16, 2))
                # The resolved matrix is the one used, and the label changes no pixel.
                self.assertEqual(raw_pixels(tagged), raw_pixels(plain))

    def test_the_resolved_matrix_is_used_even_after_a_contradicting_box(self) -> None:
        video = self.make_video("yuva444p12le", "prores_ks", tagged=True)
        color = read_stream_color(video, shutil.which("ffprobe"))
        self.assertEqual(color, StreamColor("smpte170m", "tv"))
        tag_video_colors(video, StreamColor("bt709", "tv"))
        extracted = self.root / "honored.png"
        extract_last_frame(video, extracted, color)
        as_stated = self.reference(video, "as-stated.png", "in_color_matrix=smpte170m:in_range=tv:out_range=pc")
        as_box = self.reference(video, "as-box.png", "in_color_matrix=bt709:in_range=tv:out_range=pc")
        self.assertEqual(raw_pixels(extracted), raw_pixels(as_stated))
        self.assertNotEqual(raw_pixels(extracted), raw_pixels(as_box))

    def test_an_untagged_video_is_decoded_as_bt709_limited_range_not_ffmpegs_bt601_default(self) -> None:
        video = self.make_video("yuv420p", "libx264", tagged=False)
        extracted = self.root / "untagged-labeled.png"
        extract_last_frame(video, extracted)
        expected = self.reference(video, "untagged-709.png", "in_color_matrix=bt709:in_range=tv:out_range=pc")
        default = self.reference(video, "untagged-default.png", "out_range=pc")
        self.assertEqual(raw_pixels(extracted), raw_pixels(expected))
        self.assertNotEqual(raw_pixels(extracted), raw_pixels(default))
        self.assertIn("sRGB", png_chunks(extracted))

    def truncated_levels(self, name: str, *codec: str) -> Path:
        """A clip of flat 8x8 blocks, one per 8-bit level, as Draw Things writes its truncated values: 8-bit RGB, BT.709."""
        levels = np.repeat(np.repeat(np.arange(256, dtype=np.uint8).reshape(16, 16), 8, axis=0), 8, axis=1)
        video = self.root / name
        subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "128x128", "-r", "5", "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *codec, str(video)], input=np.stack([levels] * 3, axis=-1).tobytes() * 2, check=True)
        return video

    def test_the_handoff_is_what_draw_things_cli_reads_plus_the_half_level_it_truncated(self) -> None:
        # ProRes carries each level within its own error; H.264's 8-bit YCbCr is a third of a level off per flat level.
        for name, codec, per_level in (("prores.mov", ("-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuv444p10le"), 0.1), ("h264.mov", ("-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p"), 0.35)):
            with self.subTest(name):
                png = self.root / f"{name}.png"
                extract_last_frame(self.truncated_levels(name, *codec), png)
                decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "rgb48be", "-"], capture_output=True, check=True).stdout
                samples = np.frombuffer(decoded, dtype=">u2").reshape(128, 128, 3)
                # draw-things-cli reads the high byte (>> 8): every low byte is the middle, 128.
                self.assertEqual(set(np.unique(samples & 255).tolist()), {128})
                blocks = (samples >> 8)[..., 1].astype(float).reshape(16, 8, 16, 8).mean(axis=(1, 3)).ravel()
                # Black and white stay; a level k stands for k to k + 1, so the handoff holds k + 0.5 on average, flat
                # areas included, which the ordered dither gives them (plain rounding is half a level off in each).
                self.assertEqual((blocks[0], blocks[255]), (0.0, 255.0))
                bias = blocks[1:255] - (np.arange(1, 255) + 0.5)
                self.assertLess(abs(float(np.mean(bias))), 0.1)
                self.assertLess(float(np.mean(np.abs(bias))), per_level)
