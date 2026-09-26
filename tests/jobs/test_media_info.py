"""Tests for measuring what an output file actually holds; ffprobe is replaced by a fake runner and never runs."""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from draw_things_control.jobs.frame_extraction import find_ffprobe, require_ffprobe
from draw_things_control.jobs.media_info import FFPROBE_TIMEOUT_SECONDS, MediaInfo, measure_output


def png_bytes(width: int, height: int) -> bytes:
    """A minimal PNG header: the signature and an IHDR chunk, which is all the measuring reads."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


class FakeProbe:
    """Answers ffprobe calls in order with (stdout, return code), and keeps each command."""

    def __init__(self, *answers: tuple[str, int]) -> None:
        self.answers = list(answers)
        self.commands: list[list[str]] = []
        self.options: list[dict[str, object]] = []

    def __call__(self, command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.options.append(options)
        stdout, code = self.answers.pop(0)
        return subprocess.CompletedProcess(command, code, stdout, "bad file" if code else "")


def streams(**entries: object) -> str:
    return json.dumps({"programs": [], "streams": [entries]})


class MediaInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # A name with a space and a quote, to show it is passed as one argument, never shell text.
        self.video = self.root / "walk it's.mov"
        self.video.write_bytes(b"video")

    def test_a_video_is_measured_from_its_displayed_size_and_frame_count(self) -> None:
        probe = FakeProbe((streams(width=832, height=448, nb_frames="81"), 0))
        info = measure_output(self.video, ffprobe="/opt/bin/ffprobe", run=probe)
        self.assertEqual(info, MediaInfo(832, 448, 81))
        self.assertEqual(probe.commands, [["/opt/bin/ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_frames", "-of", "json", str(self.video)]])
        self.assertEqual(probe.options[0]["timeout"], FFPROBE_TIMEOUT_SECONDS)
        self.assertNotIn("shell", probe.options[0])

    def test_frames_are_counted_from_packets_when_the_container_does_not_say(self) -> None:
        for missing in ({"nb_frames": "N/A"}, {}):
            with self.subTest(missing=missing):
                probe = FakeProbe((streams(width=832, height=448, **missing), 0), (streams(nb_read_packets="77"), 0))
                info = measure_output(self.video, ffprobe="ffprobe", run=probe)
                self.assertEqual(info, MediaInfo(832, 448, 77))
                self.assertIn("-count_packets", probe.commands[1])
                self.assertNotIn("-count_frames", probe.commands[1])

    def test_an_mp4_is_measured_as_a_video(self) -> None:
        video = self.root / "walk.MP4"
        video.write_bytes(b"video")
        self.assertEqual(measure_output(video, ffprobe="ffprobe", run=FakeProbe((streams(width=64, height=64, nb_frames=5), 0))), MediaInfo(64, 64, 5))

    def test_unknown_values_are_none_and_never_zero_or_a_boolean(self) -> None:
        probe = FakeProbe((streams(width=0, height=True, nb_frames="12"), 0))
        self.assertEqual(measure_output(self.video, ffprobe="ffprobe", run=probe), MediaInfo(None, None, 12))

    def test_a_video_that_cannot_be_measured_raises_value_error(self) -> None:
        cases = {
            "exit code": FakeProbe(("", 1)),
            "not json": FakeProbe(("garbage", 0)),
            "no stream": FakeProbe((json.dumps({"streams": []}), 0)),
        }
        for name, probe in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                measure_output(self.video, ffprobe="ffprobe", run=probe)

    def test_a_slow_or_missing_ffprobe_raises_value_error(self) -> None:
        def slow(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(command, FFPROBE_TIMEOUT_SECONDS)

        def missing(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(command[0])

        for run in (slow, missing):
            with self.subTest(run.__name__), self.assertRaises(ValueError):
                measure_output(self.video, ffprobe="ffprobe", run=run)
        with mock.patch("draw_things_control.jobs.media_info.find_ffprobe", return_value=None), self.assertRaisesRegex(ValueError, "ffprobe was not found"):
            measure_output(self.video)

    def test_a_png_is_measured_from_its_header_without_ffprobe(self) -> None:
        image = self.root / "frame.png"
        image.write_bytes(png_bytes(1024, 576) + b"rest of the image")

        def never(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("ffprobe must not run for a PNG")

        self.assertEqual(measure_output(image, run=never), MediaInfo(1024, 576, None))

    def test_a_file_that_is_not_a_png_or_is_cut_short_raises_value_error(self) -> None:
        cases = {"jpeg.png": b"\xff\xd8\xff\xe0" + bytes(40), "short.png": png_bytes(8, 8)[:20], "empty.png": b""}
        for name, content in cases.items():
            (self.root / name).write_bytes(content)
            with self.subTest(name), self.assertRaises(ValueError):
                measure_output(self.root / name)
        with self.assertRaises(ValueError):
            measure_output(self.root / "gone.png")
        (self.root / "notes.txt").write_text("text")
        with self.assertRaisesRegex(ValueError, "not a PNG, MOV, or MP4"):
            measure_output(self.root / "notes.txt")

    def test_ffprobe_is_found_beside_ffmpeg_before_path(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "ffmpeg").write_text("")
        (tools / "ffprobe").write_text("")
        with mock.patch("draw_things_control.jobs.frame_extraction.shutil.which", return_value="/usr/bin/ffprobe"):
            self.assertEqual(find_ffprobe(str(tools / "ffmpeg")), str(tools / "ffprobe"))
            self.assertEqual(find_ffprobe(str(self.root / "elsewhere" / "ffmpeg")), "/usr/bin/ffprobe")

    def test_a_missing_ffprobe_is_explained(self) -> None:
        with mock.patch("draw_things_control.jobs.frame_extraction.shutil.which", return_value=None), self.assertRaises(ValueError) as caught:
            require_ffprobe()
        self.assertEqual(str(caught.exception), "Could not find 'ffprobe' beside ffmpeg or on PATH; video jobs need it to measure their outputs (it comes with ffmpeg, for example: brew install ffmpeg)")


if __name__ == "__main__":
    unittest.main()
