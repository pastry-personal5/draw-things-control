"""A version-2 chain that mixes H3 and Draw Things runs, executed with fake children and the real adapters' finishing."""

from __future__ import annotations

import json
import signal
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from PIL import Image

from framecontrol.core.arguments import DrawThingsGenerateArguments
from framecontrol.jobs.backend import BackendRegistry
from framecontrol.jobs.backends.draw_things import DrawThingsBackend
from framecontrol.jobs.backends.h3 import H3Arguments, H3Backend, H3Runtime
from framecontrol.jobs.executor import JobOutcome
from framecontrol.jobs.media.h3_color import H3_RGB_CONVENTION_SHA256, H3Declarations
from framecontrol.jobs.media.toolkit import MediaTools
from framecontrol.jobs.parsing import load_job
from framecontrol.jobs.planning import JobPlanner
from tests.fixtures import job_executor, run_job_with
from tests.jobs.test_compile_v2 import CompileV2TestCase, v2_job

SIZE = (64, 64)


class ToollessH3Backend(H3Backend):
    """The real H3 adapter with its installation probe replaced: it only needs resolved media tool names."""

    def check_tools(self, job: Any, executable: str, media: Any, find_executable: Any) -> None:
        self._ffmpeg, self._ffprobe = "ffmpeg", "ffprobe"
        self.checked = getattr(self, "checked", 0) + 1
        self.checked_pipelines = [*getattr(self, "checked_pipelines", []), job.backend_data.runs[0].pipeline]


@dataclass(frozen=True)
class FakeResult:
    return_code: int = 0
    timed_out: bool = False
    termination_signal: signal.Signals | None = None


class FakeRunner:
    def __init__(self, arguments: Any) -> None:
        self.arguments = arguments

    def run(self) -> FakeResult:
        if self.arguments.output.suffix == ".png":
            Image.new("RGB", SIZE).save(self.arguments.output)
        else:
            self.arguments.output.write_bytes(b"child output")
        return FakeResult()

    def request_shutdown(self, received_signal: signal.Signals = signal.SIGTERM) -> None:
        return None


def small_job(**changes: Any) -> dict[str, Any]:
    """The mixed job of the compiler tests at a size every adapter accepts and the fake children can serve."""
    data = v2_job(**changes)
    data["settings_profiles"]["h3-wide"]["settings"].update(width=64, height=64)
    return data


class MixedChainTestCase(CompileV2TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_base_config({"model": "base.ckpt", "width": 64, "height": 64, "numFrames": 9})
        self.h3 = ToollessH3Backend(H3Runtime(Path("/fake/h3"), Path("/fake/model")))
        self.draw_things = DrawThingsBackend()
        self.backends = BackendRegistry((self.draw_things, self.h3))
        self.calls: list[tuple[Any, float | None]] = []

    def factory(self, arguments: Any, timeout: float | None, *_rest: Any) -> FakeRunner:
        self.calls.append((arguments, timeout))
        return FakeRunner(arguments)

    def executor(self, **options: Any) -> Any:
        return job_executor(runner_factory=self.factory, find_executable=lambda _name: "tool", frame_extractor=lambda _video, png: Image.new("RGB", SIZE).save(png), require_ffmpeg=lambda: "ffmpeg", handle_signals=False, backends=self.backends, **options)

    @contextmanager
    def h3_media(self, frame_size: tuple[int, int] = SIZE) -> Any:
        """The H3 adapter's media probes answered as an untagged, valid MP4 would, so its real finishing runs."""
        facts = {"width": 64, "height": 64, "frames": 39, "frame_rate": 24, "video_duration": 39 / 24, "audio_codec": "aac", "audio_sample_rate": 32000, "audio_channels": 2, "audio_duration": 39 / 24}
        declarations = H3Declarations(None, None, {"color_range": None, "color_space": None, "color_transfer": None, "color_primaries": None})

        def extract(_video: Path, target: Path, _ffmpeg: str) -> None:
            Image.new("RGB", frame_size).save(target)

        with ExitStack() as stack:
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._file_sha256", return_value=H3_RGB_CONVENTION_SHA256))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.inspect_h3_declarations", return_value=declarations))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.convert_h3_delivery", side_effect=ValueError("not converted")))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3.tag_video_color_codes", side_effect=ValueError("not tagged")))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._verify_h3_mp4", return_value=facts))
            stack.enter_context(mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=extract))
            yield

    def run_chain(self, data: dict[str, Any], **options: Any) -> JobOutcome:
        job = load_job(self.write_job(data), self.global_config, self.params, backends=self.backends)
        with self.h3_media():
            return run_job_with(self.executor(**options), job, executable="draw-things-cli", shutdown_grace=1, write_records=True)

    def manifest(self, outcome: JobOutcome) -> dict[str, Any]:
        assert outcome.manifest is not None
        return json.loads(outcome.manifest.read_text(encoding="utf-8"))


class MixedExecutionTests(MixedChainTestCase):
    def test_each_run_is_launched_by_its_own_adapter_with_its_own_settings(self) -> None:
        outcome = self.run_chain(small_job())
        self.assertEqual((outcome.exit_code, outcome.completed_runs, outcome.total_runs), (0, 4, 4))
        kinds = [type(arguments).__name__ for arguments, _timeout in self.calls]
        self.assertEqual(kinds, ["H3Arguments", "H3Arguments", "DrawThingsGenerateArguments", "H3Arguments"])
        self.assertEqual([timeout for _arguments, timeout in self.calls], [3600, 3600, 1800, 3600])
        frames = [arguments.command[arguments.command.index("--frames") + 1] for arguments, _timeout in self.calls if isinstance(arguments, H3Arguments)]
        self.assertEqual(frames, ["124", "90", "192"])
        draw_things = self.calls[2][0]
        assert isinstance(draw_things, DrawThingsGenerateArguments)
        self.assertEqual((draw_things.frames, draw_things.seed, draw_things.prompt, draw_things.negative_prompt), (81, 42, "keep going", "blurry"))

    def test_a_handoff_between_runs_of_one_profile_is_passed_as_it_is(self) -> None:
        manifest = self.manifest(self.run_chain(small_job()))
        run1, run2 = self.calls[0][0], self.calls[1][0]
        assert isinstance(run1, H3Arguments) and isinstance(run2, H3Arguments) and run2.first_frame is not None
        self.assertIsNone(run1.first_frame)
        self.assertEqual(len(run1.references), 2)
        self.assertEqual(run2.first_frame.name, manifest["runs"][0]["last_frame"])
        self.assertEqual(run2.first_frame.parent, self.output_directory / "story")
        self.assertIsNone(manifest["runs"][1]["resized_input"])

    def test_a_handoff_across_a_backend_boundary_is_prepared_by_the_consuming_profile(self) -> None:
        manifest = self.manifest(self.run_chain(small_job()))
        run3, run4 = self.calls[2][0], self.calls[3][0]
        assert isinstance(run3, DrawThingsGenerateArguments) and isinstance(run4, H3Arguments) and run3.image is not None and run4.first_frame is not None
        runs = manifest["runs"]
        # The model reads the copy; the record names the producer's own file and the copy's path, which is gone after the run.
        self.assertEqual((runs[2]["input"], runs[2]["resized_input"]), (str(self.output_directory / "story" / runs[1]["last_frame"]), str(run3.image)))
        self.assertEqual((runs[3]["input"], runs[3]["resized_input"]), (str(self.output_directory / "story" / runs[2]["last_frame"]), str(run4.first_frame)))
        self.assertTrue(all(path.parent != self.output_directory / "story" and not path.exists() for path in (run3.image, run4.first_frame)))
        self.assertEqual([(run.get("input_run"), run.get("input_role")) for run in runs], [(None, None), (1, "handoff"), (2, "handoff"), (3, "handoff")])

    def test_a_handoff_the_consuming_profile_cannot_take_stops_the_job_before_its_run_launches(self) -> None:
        job = load_job(self.write_job(small_job()), self.global_config, self.params, backends=self.backends)
        with self.h3_media(frame_size=(32, 32)):
            outcome = run_job_with(self.executor(), job, executable="draw-things-cli", shutdown_grace=1, write_records=True)
        manifest = self.manifest(outcome)
        self.assertEqual((outcome.exit_code, outcome.completed_runs, manifest["status"]), (1, 2, "failed"))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(manifest["runs"]), 2)

    def test_an_image_run_hands_its_output_to_a_video_run_in_another_profile(self) -> None:
        data = small_job()
        data["input"] = "first-frame.png"
        self.write_image("first-frame.png", SIZE)
        data["prompt_pairs"]["still"] = {"positive": "a still"}
        data["settings_profiles"]["still-image"] = {"backend": "draw-things", "settings": {"config_file": "base.yaml"}}
        data["defaults"].append({"backend": "draw-things", "pipeline": "i2i", "prompt_pair": "still", "settings_profile": "still-image", "knobs": {"timeout_seconds": 60}})
        data["runs"] = [{"backend": "draw-things", "pipeline": "i2i"}, {"backend": "draw-things", "pipeline": "i2v"}]
        data.pop("reference_pairs")
        del data["settings_profiles"]["h3-wide"]
        data["defaults"] = data["defaults"][2:]
        data["prompt_pairs"].pop("establish")
        data["prompt_pairs"].pop("go")
        manifest = self.manifest(self.run_chain(data))
        image, video = self.calls[0][0], self.calls[1][0]
        assert isinstance(image, DrawThingsGenerateArguments) and isinstance(video, DrawThingsGenerateArguments)
        self.assertEqual((image.output.suffix, video.output.suffix), (".png", ".mov"))  # type: ignore[union-attr]  # both outputs are set
        self.assertEqual([(run["pipeline"], run.get("input_run"), run["input_role"]) for run in manifest["runs"]], [("i2i", None, "initial"), ("i2v", 1, "output")])
        # Different profiles, so the video run reads a prepared copy of the still the first run wrote.
        assert video.image is not None
        self.assertEqual(manifest["runs"][1]["input"], str(self.output_directory / "story" / manifest["runs"][0]["output"]))
        self.assertEqual(manifest["runs"][1]["resized_input"], str(video.image))

    def test_the_manifest_records_each_runs_backend_pipeline_profile_and_knobs(self) -> None:
        manifest = self.manifest(self.run_chain(small_job()))
        self.assertEqual((manifest["backend"], manifest["mode"], manifest["total_runs"]), ("mixed", "mixed", 4))
        runs = manifest["runs"]
        self.assertEqual([(run["backend"], run["pipeline"], run["settings_profile"], run["pair"]) for run in runs], [("h3", "r2av", "h3-wide", "establish"), ("h3", "i2av", "h3-wide", "go"), ("draw-things", "i2v", "wan", "paint"), ("h3", "i2av", "h3-wide", "go")])
        self.assertEqual(runs[1]["knobs"]["seconds"], 4)
        self.assertEqual(runs[3]["knob_sources"]["seconds"], "run")
        self.assertEqual(runs[2]["knobs"]["frames"], 81)
        self.assertEqual([run["status"] for run in runs], ["succeeded"] * 4)
        self.assertEqual(runs[0]["input"], None)

    def test_a_run_with_no_seed_uses_the_one_execution_seed_and_records_its_value(self) -> None:
        # The base configuration's own seed is not a version-2 default: omission means the execution seed.
        self.write_base_config({"model": "base.ckpt", "width": 64, "height": 64, "numFrames": 9, "seed": 123})
        data = small_job()
        for block in data["defaults"]:
            block["knobs"].pop("seed")
        outcome = self.run_chain(data, random_seed=lambda: 777)
        seeds = [arguments.settings.seed if isinstance(arguments, H3Arguments) else arguments.seed for arguments, _timeout in self.calls]
        self.assertEqual(seeds, [777, 777, 777, 777])
        draw_things = self.calls[2][0]
        assert isinstance(draw_things, DrawThingsGenerateArguments)
        self.assertNotIn('"seed"', draw_things.config_json or "")
        manifest = self.manifest(outcome)
        self.assertEqual((manifest["seed"], manifest["seed_source"]), (777, "random"))
        self.assertEqual([(run["knobs"]["seed"], run["knob_sources"]["seed"]) for run in manifest["runs"]], [(777, "execution_seed")] * 4)

    def test_an_explicit_seed_beats_the_execution_seed_for_that_run_only(self) -> None:
        data = small_job()
        for block in data["defaults"]:
            block["knobs"].pop("seed")
        data["runs"][2]["knobs"] = {"seed": 5}
        self.run_chain(data, random_seed=lambda: 777)
        seeds = [arguments.settings.seed if isinstance(arguments, H3Arguments) else arguments.seed for arguments, _timeout in self.calls]
        self.assertEqual(seeds, [777, 777, 5, 777])
        draw_things = self.calls[2][0]
        assert isinstance(draw_things, DrawThingsGenerateArguments)
        self.assertEqual(draw_things.seed, 5)

    def test_a_profile_shared_by_two_pipelines_is_checked_for_each(self) -> None:
        # The adapter checks the checkpoint its pipeline needs, so r2av and i2av on one profile are two checks.
        self.run_chain(small_job())
        self.assertEqual(sorted(self.h3.checked_pipelines), ["i2av", "r2av"])
        self.assertEqual(self.h3.checked, 2)

    def test_only_the_backends_a_remaining_run_selects_are_checked(self) -> None:
        data = small_job()
        data["runs"] = [{"backend": "draw-things", "pipeline": "i2v"}, {"backend": "draw-things", "pipeline": "i2v"}]
        data["input"] = "first-frame.png"
        data["defaults"] = [data["defaults"][2]]
        data.pop("reference_pairs")
        del data["settings_profiles"]["h3-wide"]
        data["prompt_pairs"] = {"paint": data["prompt_pairs"]["paint"]}
        self.write_image("first-frame.png", SIZE)
        self.run_chain(data)
        self.assertEqual(getattr(self.h3, "checked", 0), 0)

    def test_the_planner_previews_every_run_without_running_anything(self) -> None:
        job = load_job(self.write_job(small_job()), self.global_config, self.params, backends=self.backends)
        planner = JobPlanner(lambda _name: "tool", MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), backends=self.backends)
        preview = planner.preview(job, executable="draw-things-control", seed=9)
        self.assertEqual([run.number for run in preview.runs], [1, 2, 3, 4])
        self.assertEqual([run.seed for run in preview.runs], [42, 42, 42, 42])
        self.assertEqual({run.output.suffix for run in preview.runs}, {".mp4", ".mov"})
        self.assertEqual(len({run.output for run in preview.runs}), 4)
        self.assertEqual(self.calls, [])

    def test_the_plan_text_names_each_runs_backend_pipeline_and_profile(self) -> None:
        from framecontrol.jobs.text import plan_header, plan_steps

        job = load_job(self.write_job(small_job()), self.global_config, self.params, backends=self.backends)
        planner = JobPlanner(lambda _name: "tool", MediaTools(require_ffmpeg=lambda: "ffmpeg", frame_extractor=lambda *_args: None), backends=self.backends)
        preview = planner.preview(job, executable="draw-things-cli", seed=9)
        self.assertTrue(plan_header(job, preview)[0].startswith("Job story (mixed, mixed): 4 runs, seed 42 (run)"))
        headings = [step.heading for step in plan_steps(job, preview)]
        self.assertEqual(headings[0], "Run 1/4 (pair establish) (h3/r2av, profile h3-wide)")
        self.assertEqual(headings[2], "Run 3/4 (pair paint) (draw-things/i2v, profile wan)")


class HandoffAvailabilityTests(MixedChainTestCase):
    """A run that needs the previous run's image stops the job when it is missing; an independent one carries on."""

    def failing_handoff(self, data: dict[str, Any]) -> tuple[JobOutcome, dict[str, Any]]:
        job = load_job(self.write_job(data), self.global_config, self.params, backends=self.backends)
        with self.h3_media():
            with mock.patch("framecontrol.jobs.backends.h3._extract_h3_frame", side_effect=ValueError("decode failed")):
                outcome = run_job_with(self.executor(), job, executable="draw-things-cli", shutdown_grace=1, write_records=True)
        return outcome, self.manifest(outcome)

    def h3_pair(self, second: str) -> dict[str, Any]:
        """An H3 `i2av` run followed by an `i2av` or `r2av` one, at the size the fake children serve."""
        self.write_image("first-frame.png", SIZE)
        defaults = [{"backend": "h3", "pipeline": "i2av", "prompt_pair": "go", "settings_profile": "h3-wide", "knobs": {"seconds": 4, "seed": 42, "timeout_seconds": 3600}}]
        data: dict[str, Any] = {
            "version": 2,
            "name": "story",
            "input": "first-frame.png",
            "prompt_pairs": {"go": {"positive": "they walk"}},
            "settings_profiles": {"h3-wide": {"backend": "h3", "settings": {"width": 64, "height": 64, "steps": 30}}},
            "defaults": defaults,
            "runs": [{"backend": "h3", "pipeline": "i2av"}, {"backend": "h3", "pipeline": second}],
        }
        if second == "r2av":
            data["reference_pairs"] = {"duo": {"references": [{"kind": "image", "path": "a.png"}]}}
            defaults.append({"backend": "h3", "pipeline": "r2av", "prompt_pair": "go", "settings_profile": "h3-wide", "knobs": {"seconds": 5, "seed": 42, "timeout_seconds": 3600, "reference_pair": "duo"}})
        return data

    def test_a_run_that_needs_the_missing_handoff_stops_the_job_before_it_launches(self) -> None:
        outcome, manifest = self.failing_handoff(self.h3_pair("i2av"))
        self.assertEqual((outcome.exit_code, outcome.completed_runs, manifest["status"]), (1, 1, "failed"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((manifest["runs"][0]["status"], manifest["runs"][0]["last_frame"]), ("succeeded", None))

    def test_an_independent_run_carries_on_without_the_missing_handoff(self) -> None:
        outcome, manifest = self.failing_handoff(self.h3_pair("r2av"))
        self.assertEqual((outcome.exit_code, outcome.completed_runs, manifest["status"]), (0, 2, "succeeded"))
        self.assertEqual(len(self.calls), 2)
        # r2av takes its references and no image, and records no image origin.
        self.assertEqual((manifest["runs"][1]["input"], manifest["runs"][1].get("input_run"), manifest["runs"][1].get("input_role")), (None, None, None))


class MixedRecordingTests(MixedChainTestCase):
    """What the state store, the HTTP serializers, and the TUI read back of a mixed execution."""

    def record(self, data: dict[str, Any]) -> Any:
        from framecontrol.state.recorder import ExecutionRecorder
        from framecontrol.state.store import Store

        store = Store.open(self.root / "framecontrol.db")
        self.addCleanup(store.close)
        job = load_job(self.write_job(data), self.global_config, self.params, backends=self.backends)
        with self.h3_media():
            run_job_with(self.executor(), job, executable="draw-things-cli", shutdown_grace=1, write_records=True, observer=ExecutionRecorder(store))
        [row] = store.executions.page()
        execution = store.executions.get(row.id)
        assert execution is not None
        return execution

    def test_each_run_row_keeps_its_backend_pipeline_profile_knobs_and_input_origin(self) -> None:
        execution = self.record(small_job())
        self.assertEqual((execution.backend, execution.mode, execution.total_runs, execution.status), ("mixed", "mixed", 4, "succeeded"))
        runs = execution.runs
        self.assertEqual([(run.backend, run.pipeline, run.settings_profile) for run in runs], [("h3", "r2av", "h3-wide"), ("h3", "i2av", "h3-wide"), ("draw-things", "i2v", "wan"), ("h3", "i2av", "h3-wide")])
        self.assertEqual([(run.input_run, run.input_role) for run in runs], [(None, None), (1, "handoff"), (2, "handoff"), (3, "handoff")])
        self.assertEqual(runs[3].knobs["seconds"], 8)
        self.assertEqual(runs[3].knob_sources["seconds"], "run")
        self.assertEqual(runs[2].knobs["frames"], 81)
        self.assertEqual(execution.settings.backend_settings["format_version"], 2)  # type: ignore[index]  # a version-2 execution always keeps its plan

    def test_the_http_run_summary_and_job_detail_expose_the_compiled_plan(self) -> None:
        from framecontrol.server.serializers import execution_detail, job_detail, job_summary
        from framecontrol.services.job_catalog import JobRow

        execution = self.record(small_job())
        runs = execution_detail(execution)["runs"]
        self.assertEqual(runs[1]["backend"], "h3")
        self.assertEqual((runs[1]["pipeline"], runs[1]["settings_profile"], runs[1]["input_run"], runs[1]["input_role"]), ("i2av", "h3-wide", 1, "handoff"))
        self.assertEqual(runs[1]["knobs"]["seconds"], 4)
        job = load_job(self.write_job(small_job()), self.global_config, self.params, backends=self.backends)
        detail = job_detail(job, "text")
        self.assertEqual((detail["format_version"], detail["mode"], detail["backend"]), (2, "mixed", "mixed"))
        self.assertEqual([item["pipeline"] for item in detail["runs"]], ["r2av", "i2av", "i2v", "i2av"])
        self.assertEqual({pair["name"]: pair["runs"] for pair in detail["prompt_pairs"]}, {"establish": [1], "go": [2, 4], "paint": [3]})
        summary = job_summary(JobRow(self.root / "job.yaml", job))
        self.assertEqual((summary["mode"], summary["backend"], summary["runs"]), ("mixed", "mixed", 4))

    def test_the_tui_detail_names_each_runs_generator_knobs_and_image_origin(self) -> None:
        from framecontrol.tui.text.execution import execution_detail, execution_settings, execution_text

        execution = self.record(small_job())
        text = str(execution_text(execution))
        self.assertIn("mode: mixed\n", text)
        self.assertIn("backend: mixed\n", text)
        self.assertIn("generator: h3/r2av, profile h3-wide\n", text)
        self.assertIn("generator: draw-things/i2v, profile wan\n", text)
        self.assertIn("seconds 8 (run)", text)
        self.assertIn("frames 81 (defaults)", text)
        self.assertIn("image from: run 2 handoff\n", text)
        # The Draw Things settings come from its own run's command, not an H3 run's.
        self.assertEqual(execution_settings(execution).model, "base.ckpt")
        self.assertEqual([run.frames for run in execution_detail(execution).runs], [39, 39, None, 39])


class DescribeRunTests(MixedChainTestCase):
    """A prediction is priced by the signature its run will have, so what a run is described as before it starts is exactly
    what its timing records when it does."""

    def test_the_description_of_each_run_of_a_mixed_chain_is_what_its_timing_records(self) -> None:
        from framecontrol.jobs.events import JobEvent, TimingStarted

        job = load_job(self.write_job(small_job()), self.global_config, self.params, backends=self.backends)
        executor = self.executor()
        events: list[JobEvent] = []
        with self.h3_media():
            run_job_with(executor, job, executable="draw-things-cli", shutdown_grace=1, write_records=True, observer=events.append)
        recorded = [event for event in events if isinstance(event, TimingStarted)]
        self.assertEqual([event.backend for event in recorded], ["h3", "h3", "draw-things-cli", "h3"])
        for event in recorded:
            described = executor.describe_run(job, event.number, executable="draw-things-cli")
            environment = {key: value for key, value in event.environment.items() if key != "load_start"}
            self.assertEqual((described.backend, described.pipeline, described.workload, described.environment), (event.backend, event.pipeline, event.workload, environment), f"run {event.number}")

    def test_the_description_holds_the_knobs_the_run_resolved_without_its_seed(self) -> None:
        job = load_job(self.write_job(small_job()), self.global_config, self.params, backends=self.backends)
        described = self.executor().describe_run(job, 2, executable="draw-things-cli")
        self.assertEqual(described.workload["knobs"]["seconds"], 4)
        self.assertNotIn("seed", described.workload["knobs"])
