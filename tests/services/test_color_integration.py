"""A correcting job through the real media tools (Milestone 09): extraction, checks, and the lazily loaded correction,
with a runner that writes Draw Things-like ProRes instead of starting draw-things-cli."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.jobs.events import JobStarted, MediaChecked, RunFinished
from draw_things_control.jobs.parsing import load_job
from draw_things_control.services.toolkit import Toolkit
from tests.fixtures import JobTestCase, job_data, run_job_with
from tests.jobs.test_executor import FakeResult, FakeRunner
from tests.jobs.test_media_correction import drifted, scene

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FRAMES = 5


def decoded(video: Path) -> bytes:
    """Every frame's pixels, which the colr tag must leave as they are."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "yuv444p16le", "-"], capture_output=True, check=True).stdout


class ClipRunner(FakeRunner):
    """Writes, as Draw Things would, a clip that starts from the image it is given and drifts: 8-bit values truncated,
    BT.709 limited range, ProRes 4444. Keeps a copy of what it wrote, to compare the original with afterwards."""

    written: dict[Path, Path] = {}

    def run(self) -> FakeResult:
        assert self.arguments.output is not None and self.arguments.image is not None
        with Image.open(self.arguments.image) as image:
            start = np.asarray(image.convert("RGB").resize((96, 64)), dtype=np.float32) / 255
        frames = [drifted(start, lightness=0.02 * share, chroma=1 + 0.06 * share, hue_degrees=3 * share) for share in np.linspace(0, 1, FRAMES)]
        raw = b"".join(np.floor(frame * 255 + 1e-9).clip(0, 255).astype(np.uint8).tobytes() for frame in frames)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "96x64", "-r", "16", "-i", "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuv444p10le", str(self.arguments.output)], input=raw, check=True)
        pristine = self.arguments.output.with_name(f"pristine-{self.arguments.output.name}")
        shutil.copyfile(self.arguments.output, pristine)
        ClipRunner.written[self.arguments.output] = pristine
        return self.result


@unittest.skipUnless(FFMPEG and FFPROBE, "needs ffmpeg and ffprobe")
class CorrectingJobWithRealToolsTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.images: list[Path] = []

        def runner(arguments: DrawThingsGenerateArguments, *rest: object) -> ClipRunner:
            assert arguments.image is not None
            self.images.append(arguments.image)
            return ClipRunner(arguments, FakeResult(), write_output=True)

        self.toolkit = Toolkit(find_executable=lambda executable: executable, job_runner_factory=runner)
        picture = np.clip(np.kron(scene(size=56)[:32], np.ones((14, 15, 1))), 0, 1)[:448, :832]
        Image.fromarray(np.rint(picture * 255).astype(np.uint8), "RGB").save(self.input_directory / "scene.png")

    def test_a_blend_job_corrects_every_run_and_hands_off_its_corrected_frame(self) -> None:
        job = load_job(self.write_job(job_data(input="scene.png", run_count=2, prompt_pairs=[{"name": "only", "positive": "walk"}], color={"anchor": "blend"})), self.global_config, self.params)
        events: list = []
        outcome = run_job_with(self.toolkit.job_executor(handle_signals=False), job, executable="draw-things-cli", shutdown_grace=2, write_records=True, observer=events.append)
        self.assertEqual((outcome.exit_code, outcome.completed_runs), (0, 2))
        started = events[0]
        assert isinstance(started, JobStarted) and started.manifest is not None and started.first_image is not None
        directory = Path(started.output_directory)
        finished = [event for event in events if isinstance(event, RunFinished)]
        manifest = json.loads(Path(started.manifest).read_text(encoding="utf-8"))
        for number, run in enumerate(finished, start=1):
            assert run.output is not None and run.last_frame is not None and run.corrected_output is not None
            stem = Path(run.output).stem
            for name in (run.corrected_output, run.last_frame, f"{stem}-last-frame-raw.png"):
                self.assertTrue((directory / name).is_file(), name)
            self.assertEqual(run.corrected_output, f"{stem}-cc.mov")
            self.assertEqual((manifest["runs"][number - 1]["anchor"], manifest["runs"][number - 1]["corrected_output"]), (started.first_image, run.corrected_output))
            # Draw Things' file keeps every pixel; only Milestone 08's colr tag is added.
            self.assertEqual(decoded(directory / run.output), decoded(ClipRunner.written[directory / run.output]))
            # Each run's checks, after its video's: the handoff's, one drift check, the copy's, then the correction's.
            stages = [event.stage for event in events if isinstance(event, MediaChecked) and event.run == number]
            self.assertEqual(stages[-4:], ["last_frame", "color_drift", "output", "color_correction"])
            self.assertEqual(stages.count("color_drift"), 1)
            correction = next(event for event in events if isinstance(event, MediaChecked) and event.run == number and event.stage == "color_correction")
            self.assertEqual(correction.verdict, "ok", correction.notes)
        # Run 2 starts from run 1's corrected handoff, not its raw frame.
        first_handoff = finished[0].last_frame
        assert first_handoff is not None
        self.assertEqual(self.images[1], directory / first_handoff)
        self.assertNotEqual((directory / first_handoff).read_bytes(), (directory / f"{Path(finished[0].output or '').stem}-last-frame-raw.png").read_bytes())


if __name__ == "__main__":
    unittest.main()
