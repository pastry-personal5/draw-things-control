"""Tests for writing color tags into a generated video's container."""

import itertools
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from draw_things_control.jobs.job_definition import JobDefinition, load_job
from draw_things_control.jobs.job_service import JobService
from draw_things_control.jobs.video_color import tag_video_colors
from tests.fixtures import JobTestCase, job_data
from tests.jobs.test_job_service import NOW, FakeResult, FakeRunner


def probe(video: Path, entries: str) -> list[str]:
    output = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entries, "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout
    return output.strip().split(",")


def frames(video: Path) -> str:
    """Every decoded frame's timestamps, duration, and content hash: what tagging must not change."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "framemd5", "-"], capture_output=True, text=True, check=True).stdout


def packets(video: Path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts,dts,duration,size", "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True).stdout


def top_level(path: Path) -> dict[str, bytes]:
    data, boxes, position = path.read_bytes(), {}, 0
    while position < len(data):
        size, kind = struct.unpack(">I4s", data[position : position + 8])
        boxes[kind.decode("latin1")] = data[position + 8 : position + size]
        position += size
    return boxes


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
class TagVideoColorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def make(self, name: str, codec: str, pixel_format: str, *extra: str) -> Path:
        video = self.root / name
        # B-frames, so the timestamps have the reordering Draw Things' files have.
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=16:duration=1", "-c:v", codec, "-pix_fmt", pixel_format, *extra, str(video)], check=True)
        return video

    def check_tagged_only_by_the_new_box(self, video: Path, added: int) -> None:
        before_frames, before_packets, before_size, before_media = frames(video), packets(video), video.stat().st_size, top_level(video)["mdat"]
        self.assertTrue(tag_video_colors(video))
        self.assertEqual(video.stat().st_size, before_size + added)
        self.assertEqual(top_level(video)["mdat"], before_media)
        self.assertEqual(packets(video), before_packets)
        self.assertEqual(frames(video), before_frames)
        primaries, transfer, matrix = probe(video, "stream=color_primaries,color_transfer,color_space")[:3]
        self.assertEqual((primaries, transfer, matrix), ("bt709", "iec61966-2-1", "bt709"))
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"], check=True)
        # Tagging again does nothing.
        stamp = video.stat().st_mtime_ns
        self.assertFalse(tag_video_colors(video))
        self.assertEqual(video.stat().st_mtime_ns, stamp)

    def test_a_quicktime_h264_file_gets_an_nclc_box_and_nothing_else_changes(self) -> None:
        video = self.make("clip.mov", "libx264", "yuv420p", "-bf", "2")
        self.check_tagged_only_by_the_new_box(video, 18)

    def test_prores_and_hevc_are_tagged_too(self) -> None:
        self.check_tagged_only_by_the_new_box(self.make("prores.mov", "prores_ks", "yuva444p12le"), 18)
        self.check_tagged_only_by_the_new_box(self.make("hevc.mov", "libx265", "yuv420p", "-tag:v", "hvc1"), 18)

    def test_an_mp4_gets_an_nclx_box_with_limited_range(self) -> None:
        video = self.make("clip.mp4", "libx264", "yuv420p")
        self.check_tagged_only_by_the_new_box(video, 19)
        self.assertEqual(probe(video, "stream=color_range"), ["tv"])

    def test_chunk_offsets_are_shifted_when_the_moov_box_comes_first(self) -> None:
        video = self.make("faststart.mp4", "libx264", "yuv420p", "-movflags", "+faststart")
        self.assertEqual(list(top_level(video)).index("moov") < list(top_level(video)).index("mdat"), True)
        self.check_tagged_only_by_the_new_box(video, 19)

    def test_a_video_that_already_has_color_tags_is_left_alone(self) -> None:
        video = self.make("tagged.mov", "libx264", "yuv420p", "-colorspace", "smpte170m", "-color_primaries", "smpte170m", "-color_trc", "smpte170m", "-movflags", "+write_colr")
        before = video.read_bytes()
        self.assertFalse(tag_video_colors(video))
        self.assertEqual(video.read_bytes(), before)

    def test_a_file_this_cannot_tag_is_refused_and_left_untouched(self) -> None:
        garbage = self.root / "not-a-video.mov"
        garbage.write_bytes(b"\x00\x00\x00\x08free" + b"\x00" * 20)
        image = self.root / "frame.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 30)
        fragmented = self.make("fragmented.mp4", "libx264", "yuv420p", "-movflags", "frag_keyframe+empty_moov")
        for path in (garbage, image, fragmented):
            with self.subTest(path.name):
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    tag_video_colors(path)
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual([entry.name for entry in self.root.iterdir() if entry.name.startswith(".")], [])

    def test_the_file_keeps_its_permissions(self) -> None:
        video = self.make("clip.mov", "libx264", "yuv420p")
        os.chmod(video, 0o640)
        tag_video_colors(video)
        self.assertEqual(video.stat().st_mode & 0o777, 0o640)


class JobTaggingTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.order: list[str] = []
        self.tagger = mock.Mock(side_effect=lambda video: self.order.append("tag") or True)
        numbers = itertools.count(1000)
        self.service = JobService(
            runner_factory=lambda arguments, timeout, grace, on_message=None, on_start=None: FakeRunner(arguments, FakeResult(), write_output=True),
            find_executable=lambda executable: executable,
            frame_extractor=lambda video, png: (self.order.append("extract"), png.write_bytes(b"png")),
            require_ffmpeg=lambda: "ffmpeg",
            clock=lambda: NOW,
            random_number=lambda: next(numbers),
            handle_signals=False,
            cooldown=lambda seconds: seconds,
            video_tagger=self.tagger,
        )

    def job(self, **changes: object) -> JobDefinition:
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.dt_config)

    def test_each_video_is_tagged_before_its_last_frame_is_extracted(self) -> None:
        outcome = self.service.run(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]), executable="draw-things-cli", shutdown_grace=2)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(self.order, ["tag", "extract", "tag", "extract"])
        self.assertEqual([call.args[0].suffix for call in self.tagger.call_args_list], [".mov", ".mov"])

    def test_an_image_job_is_not_tagged(self) -> None:
        self.service.run(self.job(mode="i2i", run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]), executable="draw-things-cli", shutdown_grace=2)
        self.tagger.assert_not_called()

    def test_a_video_that_cannot_be_tagged_does_not_fail_the_run(self) -> None:
        self.tagger.side_effect = ValueError("fragmented MP4")
        with mock.patch("draw_things_control.jobs.job_service.logger") as log:
            outcome = self.service.run(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]), executable="draw-things-cli", shutdown_grace=2)
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 2))
        self.assertTrue(any("Could not write color tags" in str(call.args[0]) for call in log.warning.call_args_list))

    def test_a_malformed_box_that_raises_struct_error_does_not_fail_the_run(self) -> None:
        self.tagger.side_effect = struct.error("unpack requires a buffer of 4 bytes")
        outcome = self.service.run(self.job(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]), executable="draw-things-cli", shutdown_grace=2)
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 2))

    def test_a_service_without_a_tagger_behaves_as_before(self) -> None:
        self.service._video_tagger = None
        self.assertEqual(self.service.run(self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]), executable="draw-things-cli", shutdown_grace=2).exit_code, 0)
