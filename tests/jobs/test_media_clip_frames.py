"""Tests for decoding a video's frames into floating point, the numpy handoff, and reading the PNGs the tool wrote."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from draw_things_control.jobs.media.clip_frames import iter_frames, probe_clip, read_tool_png
from draw_things_control.jobs.media.frames import decode_filter, extract_last_frame, handoff_samples, write_handoff_png
from draw_things_control.jobs.media.stream_color import StreamColor

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
BT709 = StreamColor("bt709", "tv")
PRORES = ("-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuv444p10le")
H264 = ("-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p")


def png_samples(path: Path) -> np.ndarray:
    """A PNG's 16-bit RGB samples as ffmpeg reads them."""
    with Image.open(path) as image:
        width, height = image.size
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb48be", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(decoded, dtype=">u2").reshape(height, width, 3)


@unittest.skipUnless(FFMPEG and FFPROBE, "needs ffmpeg and ffprobe")
class ClipFramesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def truncated_levels(self, name: str, codec: tuple[str, ...], frames: int = 3) -> Path:
        """Flat 8x8 blocks, one per 8-bit level, as Draw Things writes its truncated values: 8-bit RGB, BT.709 limited."""
        levels = np.repeat(np.repeat(np.arange(256, dtype=np.uint8).reshape(16, 16), 8, axis=0), 8, axis=1)
        video = self.root / name
        subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "128x128", "-r", "16", "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", *codec, str(video)], input=np.stack([levels] * 3, axis=-1).tobytes() * frames, check=True)
        return video

    def test_a_truncated_clip_decodes_without_bias_and_black_stays_black(self) -> None:
        for name, codec in (("prores.mov", PRORES), ("h264.mov", H264)):
            with self.subTest(name):
                frames = list(iter_frames(self.truncated_levels(name, codec), BT709, str(FFMPEG), (128, 128)))
                self.assertEqual(len(frames), 3)
                self.assertEqual((frames[0].dtype, frames[0].shape), (np.float32, (128, 128, 3)))
                blocks = (frames[-1][..., 1] * 255).astype(np.float64).reshape(16, 8, 16, 8).mean(axis=(1, 3)).ravel()
                # A level k stands for k to k + 1: decoded, it reads k + 0.5 on average; black stays 0.
                self.assertLess(abs(float(np.mean(blocks[1:255] - (np.arange(1, 255) + 0.5)))), 0.05)
                self.assertLess(blocks[0], 0.05)
                # The sample is divided by 256, as the handoff takes it: white is 255 plus the half level, not 254.5.
                self.assertAlmostEqual(float(blocks[255]), 255.5, delta=0.05)

    def test_the_numpy_handoff_equals_the_geq_handoff_of_the_same_frame(self) -> None:
        video = self.truncated_levels("prores.mov", PRORES, frames=1)
        png = self.root / "last-frame.png"
        extract_last_frame(video, png, BT709)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", decode_filter(BT709), "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"], capture_output=True, check=True).stdout
        level = np.frombuffer(raw, dtype="<u2").reshape(128, 128, 3).astype(np.float64) / 256
        samples = handoff_samples(level + 0.5 * np.minimum(level, 1.0))
        np.testing.assert_array_equal(samples, png_samples(png))

    def test_a_written_handoff_holds_its_samples_labeled_srgb_and_reads_back_by_its_high_byte(self) -> None:
        levels = np.random.default_rng(5).random((48, 64, 3)) * 255
        samples = handoff_samples(levels)
        self.assertEqual(set(np.unique(samples & 255).tolist()), {128})
        png = self.root / "handoff.png"
        write_handoff_png(samples, png)
        np.testing.assert_array_equal(png_samples(png), samples)
        self.assertIn(b"sRGB", png.read_bytes()[:200])
        np.testing.assert_array_equal(read_tool_png(png), (samples >> 8).astype(np.float32) / 255)
        with self.assertRaisesRegex(ValueError, "Could not write handoff.png"):
            write_handoff_png(samples, png)

    def test_the_ordered_dither_holds_a_flat_areas_mean(self) -> None:
        # e + 0.5 in 8 bits: half the pixels at each neighbouring level, not all on one side.
        samples = handoff_samples(np.full((64, 64, 3), 100.5))
        self.assertAlmostEqual(float(np.mean(samples >> 8)), 100.5, delta=0.01)

    def test_an_8_bit_png_reads_as_its_values(self) -> None:
        png = self.root / "plain.png"
        Image.new("RGB", (4, 4), (10, 128, 255)).save(png)
        np.testing.assert_array_equal(read_tool_png(png)[0, 0], np.array([10, 128, 255], dtype=np.float32) / 255)

    def test_the_probe_reads_the_frame_count_rate_and_codec(self) -> None:
        info = probe_clip(self.truncated_levels("prores.mov", PRORES, frames=4), str(FFPROBE))
        self.assertEqual((info.width, info.height, info.frames, info.rate, info.codec, info.tag), (128, 128, 4, "16/1", "prores", "ap4h"))
        self.assertEqual(info.fps, 16.0)

    def test_stopping_early_stops_ffmpeg_and_a_broken_file_is_an_error(self) -> None:
        frames = iter_frames(self.truncated_levels("prores.mov", PRORES, frames=5), BT709, str(FFMPEG), (128, 128))
        next(frames)
        frames.close()
        broken = self.root / "broken.mov"
        broken.write_bytes(b"not a video")
        with self.assertRaisesRegex(ValueError, "could not decode broken.mov"):
            list(iter_frames(broken, BT709, str(FFMPEG), (128, 128)))
        with self.assertRaisesRegex(ValueError, "ran out of time"):
            list(iter_frames(self.root / "prores.mov", BT709, str(FFMPEG), (128, 128), deadline=0.0))


if __name__ == "__main__":
    unittest.main()
