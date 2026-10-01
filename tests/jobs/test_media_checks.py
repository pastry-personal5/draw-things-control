"""Tests for the media checks of an i2v job: what each file holds, and the events the executor makes of them."""

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np
from loguru import logger
from PIL import Image, ImageCms, PngImagePlugin

from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.events import JobStarted, MediaChecked, RunFinished, RunStarted, event_from_dict, event_to_dict
from draw_things_control.jobs.executor import JobExecutor, ResumePoint
from draw_things_control.jobs.inputs.resize import resize_image
from draw_things_control.jobs.inputs.size import resize_plan
from draw_things_control.jobs.log_writer import JobLogWriter
from draw_things_control.jobs.media import checks
from draw_things_control.jobs.media.checks import MediaCheck, MediaChecker, VideoProbe, check_handoff, check_input, check_last_frame, check_resized_input, image_facts
from draw_things_control.jobs.media.fingerprint import MatrixFingerprint, can_measure
from draw_things_control.jobs.media.stream_color import StreamColor, resolve_video_color
from draw_things_control.jobs.media.toolkit import MediaTools
from draw_things_control.jobs.media.tools import find_ffprobe
from draw_things_control.jobs.media.video_color import read_colr, tag_video_colors
from draw_things_control.jobs.parsing import load_job
from draw_things_control.jobs.text import media_check_text
from draw_things_control.tui.text.events import event_text
from tests.fixtures import JobTestCase, job_data, job_executor, run_job_with
from tests.jobs.test_executor import FakeResult, FakeRunner, saved_log

DISPLAY_P3 = Path("/System/Library/ColorSync/Profiles/Display P3.icc")
SRGB_CHUNK = PngImagePlugin.PngInfo()
SRGB_CHUNK.add(b"sRGB", b"\x00")


def probe(**changes: Any) -> VideoProbe:
    values: dict[str, Any] = {"codec": "prores", "tag": "ap4h", "profile": "4444", "width": 64, "height": 48, "pix_fmt": "yuva444p12le", "frames": 3, "color_space": "bt709", "color_range": "tv", "color_primaries": None, "color_transfer": None}
    values.update(changes)
    return VideoProbe(**values)


def write_rgb48_png(path: Path, width: int, height: int) -> Path:
    """A black 16-bit RGB PNG with an sRGB chunk, as the extractor writes one; Pillow cannot write 16-bit RGB."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\x00" + bytes(6 * width) for _ in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 2, 0, 0, 0)) + chunk(b"sRGB", b"\x00") + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return path


class ImageCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def save(self, image: Image.Image, name: str, **options: Any) -> Path:
        path = self.root / name
        image.save(path, **options)
        return path

    def test_a_plain_srgb_input_is_ok(self) -> None:
        path = self.save(Image.new("RGB", (32, 16), (10, 20, 30)), "in.png")
        check = check_input(path)
        self.assertEqual((check.stage, check.verdict, check.notes), ("input", "ok", ()))
        self.assertEqual(check.summary, "PNG 32x16, 8-bit RGB, no color profile (read as sRGB), no alpha")

    def test_an_input_that_is_not_plain_8_bit_srgb_is_a_note_since_its_copy_is_read(self) -> None:
        path = self.save(Image.new("RGBA", (32, 16), (10, 20, 30, 128)), "alpha.png")
        check = check_input(path)
        self.assertEqual(check.verdict, "ok")
        self.assertEqual(check.notes, ("draw-things-cli reads its copy, upright 8-bit sRGB RGB, not the file itself.",))

    def test_cmyk_without_a_profile_and_gamma_chunks_only_warn(self) -> None:
        cmyk = self.save(Image.new("CMYK", (8, 8)), "cmyk.jpg")
        self.assertIn("CMYK with no ICC profile", check_input(cmyk).warnings[0])
        info = PngImagePlugin.PngInfo()
        info.add(b"gAMA", (45455).to_bytes(4, "big"))
        gamma = self.save(Image.new("RGB", (8, 8)), "gamma.png", pnginfo=info)
        self.assertIn("gAMA or cHRM only", check_input(gamma).warnings[0])

    @unittest.skipUnless(DISPLAY_P3.is_file(), "macOS Display P3 profile not available")
    def test_a_display_p3_input_is_named_and_its_copy_keeps_the_mean_color(self) -> None:
        pixels = np.random.default_rng(1).integers(0, 256, (128, 128, 3), dtype=np.uint8)
        source = self.save(Image.fromarray(pixels), "p3.jpg", icc_profile=DISPLAY_P3.read_bytes(), quality=95)
        check = check_input(source)
        self.assertIn("ICC Display P3", check.summary)
        self.assertEqual(check.notes, ("The copy converts it from Display P3 to sRGB.", "draw-things-cli reads its copy, upright 8-bit sRGB RGB, not the file itself."))
        # Down in linear light, then up in sRGB values.
        for size in ((64, 64), (256, 256)):
            plan = resize_plan("p3.jpg", (128, 128), None, *size)
            copy = self.root / f"copy-{size[0]}.png"
            resize_image(source, plan, copy)
            resized = check_resized_input(source, copy, plan)
            self.assertEqual(resized.verdict, "ok", resized.warnings)
            self.assertIn("converted from Display P3 to sRGB with gamut mapping", resized.summary)
            self.assertLessEqual(max(abs(value) for value in resized.facts["mean_color_drift_levels"]), 1.0)
            source_facts = resized.facts["source"]
            self.assertEqual((source_facts["profile"], source_facts["conversion"], source_facts["sixteen_bit"]), ("Display P3", "matrix", None))
            # Random 8-bit P3 values reach beyond sRGB, so some are brought in, and the check says how many.
            self.assertGreater(source_facts["gamut"]["beyond_knee"], 0)
            self.assertTrue(any("brought in at constant lightness and hue" in note for note in resized.notes))

    def test_a_lab_inputs_copy_says_its_lab_values_were_converted(self) -> None:
        lab_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
        source = self.save(Image.new("LAB", (64, 64), (138, 209, 198)), "in.tif", icc_profile=lab_profile)
        plan = resize_plan("in.tif", (64, 64), None, 64, 64)
        copy = self.root / "copy.png"
        resize_image(source, plan, copy)
        check = check_resized_input(source, copy, plan)
        self.assertIn("Lab converted to sRGB, Lab identity built-in not needed", check.summary)
        self.assertEqual(check.facts["source"]["conversion"], "lab")

    def test_a_copy_whose_color_moved_or_whose_size_is_wrong_warns(self) -> None:
        source = self.save(Image.new("RGB", (128, 128), (100, 120, 140)), "in.png")
        plan = resize_plan("in.png", (128, 128), None, 64, 64)
        copy = self.save(Image.new("RGB", (64, 64), (110, 120, 140)), "copy.png")
        check = check_resized_input(source, copy, plan)
        self.assertEqual(check.verdict, "warning")
        self.assertIn("Its mean color moved by +10.0, +0.0, +0.0 levels", check.warnings[0])
        # The summary says what happened, never "kept within" beside a warning.
        self.assertTrue(check.summary.endswith("; mean color moved 10.0 levels"), check.summary)
        kept = check_resized_input(source, self.save(Image.new("RGB", (64, 64), (100, 120, 140)), "kept.png"), plan)
        self.assertTrue(kept.summary.endswith("; mean color kept within 0.0 levels"), kept.summary)
        small = self.save(Image.new("RGB", (32, 32), (100, 120, 140)), "small.png")
        self.assertIn("not the planned 64x64", check_resized_input(source, small, plan).warnings[0])

    def test_the_last_frame_warns_about_alpha_a_missing_label_a_wrong_size_and_depth(self) -> None:
        stated = StreamColor("bt709", "tv")
        good = write_rgb48_png(self.root / "good.png", 64, 48)
        # 16-bit from every source, H.264's included.
        for source in (probe(), probe(pix_fmt="yuv420p")):
            check = check_last_frame(good, source, stated)
            self.assertEqual(check.verdict, "ok", check.warnings)
        self.assertEqual(check.summary, "PNG 64x48, 16-bit RGB, sRGB chunk, no alpha; decoded from bt709, limited range, as its stream states")
        plain = self.save(Image.new("RGB", (64, 48)), "plain.png", pnginfo=SRGB_CHUNK)
        self.assertEqual(check_last_frame(plain, probe(pix_fmt="yuv420p"), stated).warnings, ("It is 8-bit, but the handoff is 16-bit from every source.",))
        rgba = self.save(Image.new("RGBA", (32, 48)), "rgba.png")
        warnings = check_last_frame(rgba, probe(pix_fmt="yuv420p"), StreamColor()).warnings
        self.assertEqual(len(warnings), 4)
        self.assertIn("It has alpha", warnings[0])
        self.assertIn("not labeled sRGB", warnings[1])
        self.assertIn("It is 8-bit", warnings[2])
        self.assertIn("It is 32x48, but the video is 64x48", warnings[3])
        self.assertIn("decoded from bt709, limited range: its stream states no matrix", check_last_frame(rgba, None, StreamColor()).summary)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_a_resumes_last_frame_is_ok_at_8_or_16_bits_and_warns_for_alpha(self) -> None:
        deep = self.root / "deep.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x48", "-frames:v", "1", "-pix_fmt", "rgb48be", str(deep)], check=True)
        for path in (deep, self.save(Image.new("RGB", (64, 48)), "plain.png", pnginfo=SRGB_CHUNK)):
            with self.subTest(path.name):
                check = check_handoff(path)
                self.assertEqual((check.stage, check.verdict), ("input", "ok"), check.warnings)
                self.assertTrue(check.summary.endswith("; the chain's own last frame"), check.summary)
        # A person's own 16-bit input is a note: draw-things-cli reads its 8-bit copy.
        self.assertEqual(check_input(deep).notes, ("draw-things-cli reads its copy, upright 8-bit sRGB RGB, not the file itself.",))
        rgba = check_handoff(self.save(Image.new("RGBA", (64, 48)), "rgba.png"))
        self.assertEqual(rgba.warnings, ("It is PNG RGBA, not an RGB PNG as a last frame is.", "It has alpha, which Draw Things reads as a mask."))

    def test_a_16_bit_png_reads_its_depth_from_the_header(self) -> None:
        path = self.save(Image.fromarray(np.zeros((4, 4), dtype=np.uint16)), "deep.png")
        self.assertEqual(image_facts(path)["bit_depth"], 16)

    def test_a_file_that_cannot_be_read_is_a_warning_not_an_error(self) -> None:
        broken = self.root / "broken.png"
        broken.write_bytes(b"not an image")
        check = MediaChecker(lambda: None).last_frame(broken, None, StreamColor())
        self.assertEqual((check.summary, check.verdict), ("could not be checked", "warning"))
        self.assertTrue(check.warnings[0].startswith("Could not check it:"))

    def test_the_tools_are_found_when_each_check_runs_not_when_the_checker_is_made(self) -> None:
        found: list[str | None] = [None]
        commands: list[list[str]] = []

        def run(command: list[str], **_options: object) -> Any:
            commands.append(command)
            return subprocess.CompletedProcess(command, 1, "", "unreadable")

        checker = MediaChecker(lambda: found[0], run=run)
        with mock.patch.object(checks, "find_ffprobe", side_effect=lambda ffmpeg: None if ffmpeg is None else "/installed/ffprobe"):
            self.assertIn("ffprobe was not found", str(checker.probe(Path("clip.mov"))))
            # Installed while the server runs: the next check finds it, with no new checker.
            found[0] = "/installed/ffmpeg"
            checker.probe(Path("clip.mov"))
        self.assertEqual([command[0] for command in commands], ["/installed/ffprobe"])


class FingerprintTests(unittest.TestCase):
    def test_the_scores_name_a_matrix_only_when_one_stands_out(self) -> None:
        self.assertEqual(MatrixFingerprint({"bt470bg": 0.254, "bt709": 0.216, "bt2020nc": 0.255}).matrix, "bt709")
        self.assertIsNone(MatrixFingerprint({"bt470bg": 0.24, "bt709": 0.236, "bt2020nc": 0.25}).matrix)
        noise = MatrixFingerprint({"bt470bg": 0.250, "bt709": 0.250, "bt2020nc": 0.250})
        self.assertIsNone(noise.matrix)
        self.assertTrue(noise.looks_like_noise)

    def test_only_full_chroma_at_10_bits_or_more_is_measured(self) -> None:
        self.assertTrue(can_measure("yuva444p12le"))
        self.assertTrue(can_measure("yuv444p10le"))
        self.assertFalse(can_measure("yuv420p"))
        self.assertFalse(can_measure("yuv444p"))
        self.assertFalse(can_measure("yuv422p10le"))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class VideoCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.checker = MediaChecker(lambda: shutil.which("ffmpeg"))

    def video(self, name: str, *, codec: str = "prores", states: str | None = None) -> Path:
        """A clip from 8-bit RGB encoded with BT.709; ``states`` rewrites the matrix its ProRes frames state, and an
        ``h264`` clip states none."""
        path = self.root / name
        source = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x48:d=0.2:r=16"]
        if codec == "h264":
            subprocess.run([*source, "-vf", "format=yuv420p", "-c:v", "libx264", str(path)], check=True)
            return path
        made = path if states is None else self.root / f"made-{name}"
        subprocess.run([*source, "-vf", "format=rgb24,scale=out_color_matrix=bt709:out_range=tv", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p12le", "-colorspace", "bt709", "-color_range", "tv", str(made)], check=True)
        if states is not None:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(made), "-c", "copy", "-bsf:v", f"prores_metadata=colorspace={states}", str(path)], check=True)
        return path

    def output(self, video: Path, requested: str | None = "prores4444") -> MediaCheck:
        # As a run does it: the stream's color read before tagging, and both the tag and the decode follow it.
        written = self.checker.probe(video)
        color = resolve_video_color(video, shutil.which("ffmpeg"), find_ffprobe())
        tag_video_colors(video, color)
        check, _ = self.checker.output(video, requested, written, color)
        return check

    def test_a_truthful_bt709_prores_file_is_ok(self) -> None:
        check = self.output(self.video("ok.mov"))
        self.assertEqual(check.verdict, "ok", check.warnings)
        self.assertIn("ProRes 4444 (ap4h), 64x48", check.summary)
        self.assertIn("stream states matrix bt709, range tv", check.summary)
        self.assertIn("decoded as bt709, limited range, as its stream states", check.summary)
        self.assertIn("colr nclc 1/13/1 (BT.709/sRGB/BT.709)", check.summary)
        self.assertIn("pixels encoded bt709", check.summary)
        self.assertEqual(check.facts["measured_matrix"], "bt709")

    def test_a_header_that_misstates_the_matrix_is_overruled_by_the_pixels(self) -> None:
        video = self.video("lying.mov", states="smpte170m")
        color = resolve_video_color(video, shutil.which("ffmpeg"), find_ffprobe())
        self.assertEqual(color, StreamColor("bt709", "tv", measured=True, stated="smpte170m"))
        check = self.output(video)
        self.assertEqual(check.verdict, "ok", check.warnings)
        self.assertIn("colr nclc 1/13/1 (BT.709/sRGB/BT.709)", check.summary)
        self.assertIn("decoded as bt709, limited range, measured from its pixels (its stream states smpte170m)", check.summary)
        self.assertIn("Its frames state smpte170m, but its pixels were encoded with bt709, so it is decoded and tagged as bt709; a reader of the frame header (ffmpeg) still sees smpte170m.", check.notes)
        self.assertEqual(check.facts["decoded_matrix"], "bt709")

    def test_a_stream_that_states_no_matrix_is_described_by_the_matrix_its_pixels_measure(self) -> None:
        # ffmpeg's own conversion is BT.601 and states nothing: the pixels decide, so the BT.709 note does not apply.
        path = self.root / "unstated.mov"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=16:duration=1", "-c:v", "prores_ks", "-pix_fmt", "yuva444p12le", str(path)], check=True)
        color = resolve_video_color(path, shutil.which("ffmpeg"), find_ffprobe())
        self.assertEqual((color.matrix, color.measured, color.stated), ("bt470bg", True, None))
        self.assertIsNotNone(color.fingerprint)
        check = self.output(path)
        self.assertIn("decoded as bt470bg, limited range, measured from its pixels (its stream states no matrix)", check.summary)
        self.assertFalse(any(note.startswith("Its stream states no matrix") for note in check.notes), check.notes)

    def test_a_truthful_header_is_kept_and_an_unmeasurable_video_keeps_what_it_states(self) -> None:
        self.assertEqual(resolve_video_color(self.video("true.mov"), shutil.which("ffmpeg"), find_ffprobe()), StreamColor("bt709", "tv"))
        self.assertEqual(resolve_video_color(self.video("plain.mov", codec="h264"), shutil.which("ffmpeg"), find_ffprobe()), StreamColor(None, None))

    def test_a_box_the_writer_left_that_contradicts_the_stream_warns(self) -> None:
        video = self.video("boxed.mov", states="smpte170m")
        written = self.checker.probe(video)
        tag_video_colors(video, StreamColor("bt709", "tv"))
        check, _ = self.checker.output(video, "prores4444", written, StreamColor("smpte170m", "tv"))
        self.assertIn("The colr box states bt709, but the stream states smpte170m: a player may use either, and ffmpeg uses the stream's for ProRes.", check.warnings)

    def test_an_untagged_h264_file_is_not_measured_and_its_format_is_compared(self) -> None:
        check = self.output(self.video("plain.mov", codec="h264"), "prores4444")
        self.assertIn("--video-format prores4444 was asked for, but it is H.264 (avc1).", check.warnings)
        self.assertIn("Its stream states no matrix, so it is decoded and tagged as BT.709 limited range, Draw Things' measured encoding.", check.notes)
        self.assertIn("Its pixels are not measured: yuv420p is not 4:4:4 at 10 bits or more.", check.notes)
        self.assertEqual(self.output(self.video("plain2.mov", codec="h264"), "h264").warnings, ())

    def test_the_colr_box_is_read_back_as_the_tagger_wrote_it(self) -> None:
        video = self.video("tagged.mov")
        self.assertIsNone(read_colr(video))
        tag_video_colors(video)
        colr = read_colr(video)
        assert colr is not None
        self.assertEqual((colr.kind, colr.primaries, colr.transfer, colr.matrix, colr.full_range), ("nclc", 1, 13, 1, None))

    def test_a_video_ffprobe_cannot_read_is_a_warning(self) -> None:
        broken = self.root / "broken.mov"
        broken.write_bytes(b"not a video")
        written = self.checker.probe(broken)
        self.assertIsInstance(written, str)
        check, decoded = self.checker.output(broken, "prores4444", written, StreamColor())
        self.assertEqual((check.verdict, decoded), ("warning", None))


class FakeChecker:
    """Answers every check at once and records the order they were asked in."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        # Each color_drift call's run input, first image, and notes.
        self.drift_asked: list[tuple[Path | None, Path | None, tuple[str, ...]]] = []

    def input(self, path: Path) -> MediaCheck:
        self.asked.append(f"input {path.name}")
        return MediaCheck("input", str(path), "an image")

    def handoff(self, path: Path) -> MediaCheck:
        self.asked.append(f"handoff {path.name}")
        return MediaCheck("input", str(path), "a last frame")

    def resized_input(self, source: Path, copy: Path, plan: object) -> MediaCheck:
        self.asked.append("resized_input")
        return MediaCheck("resized_input", copy.name, "a copy")

    def probe(self, video: Path) -> VideoProbe | str:
        self.asked.append(f"probe {video.name}")
        return probe()

    def output(self, video: Path, requested: str | None, written: object, color: StreamColor) -> tuple[MediaCheck, VideoProbe | None]:
        self.asked.append(f"output {requested}")
        return MediaCheck("output", video.name, "a video", warnings=("A warning.",)), probe()

    def last_frame(self, png: Path, video: object, color: StreamColor) -> MediaCheck:
        self.asked.append("last_frame")
        return MediaCheck("last_frame", png.name, "a frame")

    def color_drift(self, video: Path, color: StreamColor, run_input: Path | None, first_image: Path | None, notes: tuple[str, ...] = ()) -> MediaCheck:
        self.asked.append("color_drift")
        self.drift_asked.append((run_input, first_image, notes))
        return MediaCheck("color_drift", video.name, "a drift", notes=notes)


def write_frame(video: Path, png: Path) -> None:
    png.write_bytes(b"png")


class ExecutorCheckTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.checker = FakeChecker()

        def tag(video: Path) -> bool:  # the fixture drops the color
            self.checker.asked.append("tag")
            return True

        self.service = job_executor(
            runner_factory=lambda arguments, *rest: FakeRunner(arguments, FakeResult(), write_output=True),
            find_executable=lambda executable: executable,
            frame_extractor=write_frame,
            require_ffmpeg=lambda: "ffmpeg",
            video_tagger=tag,
            checker=self.checker,
            handle_signals=False,
            cooldown=lambda seconds: seconds,
        )

    def run_and_observe(self, **changes: object) -> list:
        events: list = []
        job = self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "walk"}], output={"video_format": "prores4444"}, **changes)
        self.outcome = run_job_with(self.service, job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append)
        return events

    def job(self, **changes: object) -> JobDefinition:
        return load_job(self.write_job(job_data(**changes)), self.global_config, self.params)

    def test_an_i2v_job_checks_each_file_in_order_between_its_events(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        events = self.run_and_observe(input="photo.jpg", desired_input_width=850)
        kinds = [event.stage if isinstance(event, MediaChecked) else type(event).__name__ for event in events]
        self.assertEqual(kinds, ["JobStarted", "input", "resized_input", "RunStarted", "output", "last_frame", "color_drift", "RunFinished", "JobFinished"])
        self.assertEqual(self.checker.asked, ["input photo.jpg", "resized_input", "probe " + events[3].output, "tag", "output prores4444", "last_frame", "color_drift"])
        # The first image is run 1's copy, kept beside the manifest; run 1's drift compares with its copy and with it.
        started = events[0]
        assert isinstance(started, JobStarted) and started.manifest is not None and started.first_image is not None
        first_image = Path(started.first_image)
        self.assertEqual(first_image, Path(started.manifest).with_name(Path(started.manifest).stem + "-first-image.png"))
        self.assertTrue(first_image.is_file())
        [(run_input, compared_with, notes)] = self.checker.drift_asked
        self.assertEqual((run_input.name if run_input else None, compared_with, notes), ("photo-832x448.png", first_image, ()))
        self.assertEqual(json.loads(Path(started.manifest).read_text(encoding="utf-8"))["first_image"], str(first_image))
        output = next(event for event in events if isinstance(event, MediaChecked) and event.stage == "output")
        self.assertEqual((output.run, output.verdict, output.notes), (1, "warning", ("A warning.",)))
        # The job log file gets the same lines as dtc serve's console.
        log = saved_log(self.outcome).read_text(encoding="utf-8")
        self.assertIn("Input check (run 1): ", log)
        self.assertIn("Resized input check (run 1): ", log)
        self.assertIn(f"Output check (run 1): {events[3].output}: a video: warning. A warning.", log)
        self.assertIn("Last frame check (run 1): ", log)

    def test_without_a_desired_size_the_scale_1_copy_is_checked_too(self) -> None:
        events = self.run_and_observe()
        self.assertEqual(self.checker.asked[:2], ["input first-frame.png", "resized_input"])
        self.assertIn("resized_input", [event.stage for event in events if isinstance(event, MediaChecked)])

    def test_a_resume_checks_the_last_frame_it_continues_from_as_a_handoff(self) -> None:
        # Resized or not, run 1 is not run again: its input and copy are not checked, the frame it continues from is.
        self.write_image("photo.jpg", (1920, 1080))
        job = self.job(run_count=3, input="photo.jpg", desired_input_width=850, prompt_pairs=[{"name": "only", "positive": "walk"}])
        events: list = []
        resume = ResumePoint(first_run=3, input=self.output_directory / "walk-2-last-frame.png", seed=7, resumes_execution="E0016")
        run_job_with(self.service, job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append, resume=resume)
        self.assertEqual(self.checker.asked[0], "handoff walk-2-last-frame.png")
        [check] = [event for event in events if isinstance(event, MediaChecked) and event.stage == "input"]
        self.assertEqual((check.run, check.summary), (3, "a last frame"))
        # The resumed execution kept no first image, so drift since it is left out, with a note.
        [(_input, first_image, notes)] = self.checker.drift_asked
        self.assertIsNone(first_image)
        self.assertIn("kept no first image", notes[0])

    def test_a_resume_compares_with_the_first_image_of_the_execution_it_resumes(self) -> None:
        self.write_image("photo.jpg", (1920, 1080))
        job = self.job(run_count=3, input="photo.jpg", desired_input_width=850, prompt_pairs=[{"name": "only", "positive": "walk"}])
        kept = self.output_directory / "walk-job-first-image.png"
        self.output_directory.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b"png")
        events: list = []
        resume = ResumePoint(first_run=3, input=self.output_directory / "walk-2-last-frame.png", seed=7, resumes_execution="E0016", first_image=kept)
        run_job_with(self.service, job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append, resume=resume)
        self.assertEqual(self.checker.drift_asked[0][1:], (kept, ()))
        self.assertEqual(events[0].first_image, str(kept))

    def test_without_records_the_first_image_is_kept_all_the_same(self) -> None:
        # Owner decision, 2026-10-01: first and blend need it, so it is kept with or without records.
        job = self.job(run_count=1, prompt_pairs=[{"name": "only", "positive": "walk"}])
        events: list = []
        run_job_with(self.service, job, executable="draw-things-cli", shutdown_grace=2, write_records=False, observer=events.append)
        [(_input, first_image, notes)] = self.checker.drift_asked
        self.assertIsNotNone(first_image)
        assert first_image is not None
        self.assertTrue(first_image.is_file())
        self.assertTrue(first_image.name.endswith("-job-first-image.png"))
        self.assertEqual(notes, ())
        self.assertEqual(events[0].first_image, str(first_image))
        self.assertFalse([path for path in self.output_directory.iterdir() if path.suffix in (".json", ".log")])

    def test_a_t2v_job_checks_its_video_and_last_frame_but_has_no_input(self) -> None:
        events = self.run_and_observe(mode="t2v", input=None)
        kinds = [event.stage if isinstance(event, MediaChecked) else type(event).__name__ for event in events]
        self.assertEqual(kinds, ["JobStarted", "RunStarted", "output", "last_frame", "color_drift", "RunFinished", "JobFinished"])
        self.assertEqual(self.checker.asked[1:], ["tag", "output prores4444", "last_frame", "color_drift"])
        self.assertTrue(isinstance(events[0], JobStarted) and isinstance(events[1], RunStarted) and isinstance(events[-2], RunFinished))
        # Run 1 has no input and no first image yet: it is measured within itself, and its last frame becomes the first image.
        [(run_input, first_image, notes)] = self.checker.drift_asked
        self.assertEqual((run_input, first_image), (None, None))
        self.assertIn("measured within itself", notes[0])
        started, run = events[0], events[1]
        assert isinstance(started, JobStarted) and started.first_image is not None and isinstance(run, RunStarted) and run.last_frame is not None
        self.assertEqual(Path(started.first_image).read_bytes(), (Path(started.output_directory) / run.last_frame).read_bytes())


class StreamColorFlowTests(JobTestCase):
    def test_the_color_is_read_once_before_tagging_and_both_tagger_and_extractor_get_it(self) -> None:
        seen: list[tuple[str, StreamColor | None]] = []
        stated = StreamColor("smpte170m", "tv")

        def read(video: Path) -> StreamColor:
            seen.append(("read", None))
            return stated

        def tag(video: Path, color: StreamColor) -> bool:
            seen.append(("tag", color))
            return True

        def extract(video: Path, png: Path, color: StreamColor) -> None:
            seen.append(("extract", color))
            png.write_bytes(b"png")

        media = MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=extract, video_tagger=tag, color_reader=read)
        executor = JobExecutor(lambda arguments, *rest: FakeRunner(arguments, FakeResult(), write_output=True), lambda executable: executable, media, handle_signals=False, cooldown=lambda seconds: seconds)
        job = load_job(self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "walk"}])), self.global_config, self.params)
        run_job_with(executor, job, executable="draw-things-cli", shutdown_grace=2)
        self.assertEqual(seen, [("read", None), ("tag", stated), ("extract", stated)])


class MediaCheckedEventTests(unittest.TestCase):
    EVENT = MediaChecked(at="2026-09-30T10:00:00+09:00", run=2, stage="last_frame", file="v-last-frame.png", summary="PNG 64x48", verdict="warning", notes=("It has alpha.",), facts={"bit_depth": 16, "drift": [0.1, 0.0, -0.1]})

    def test_it_round_trips_through_the_event_stream(self) -> None:
        data = event_to_dict(self.EVENT)
        self.assertEqual(data["kind"], "media_checked")
        self.assertEqual(event_from_dict(data), self.EVENT)

    def test_every_front_end_prints_the_same_line_and_a_warning_stands_out(self) -> None:
        line = "Last frame check (run 2): v-last-frame.png: PNG 64x48: warning. It has alpha."
        self.assertEqual(media_check_text(self.EVENT), line)
        text = event_text(self.EVENT)
        assert text is not None
        self.assertEqual((text.plain, str(text.style)), (line, "yellow"))
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(f"{message.record['level'].name} {message.record['message']}"), level="INFO")
        try:
            JobLogWriter()(self.EVENT)
            JobLogWriter()(MediaChecked(at="", run=1, stage="input", file="in.png", summary="PNG", verdict="ok"))
        finally:
            logger.remove(sink)
        self.assertEqual(lines, [f"WARNING {line}", "INFO Input check (run 1): in.png: PNG: ok"])
