"""Tests for compiling a version-2 job: catalogs, scoped defaults, per-run knobs, and per-run validation."""

import copy
from pathlib import Path
from typing import Any

from framecontrol.core.errors import InputError
from framecontrol.jobs.backend import BackendRegistry
from framecontrol.jobs.backends import DEFAULT_BACKENDS
from framecontrol.jobs.backends.draw_things import DrawThingsBackend, FrameRate
from framecontrol.jobs.backends.h3 import H3Backend, H3Runtime, effective_frames
from framecontrol.jobs.definition import JobDefinition
from framecontrol.jobs.parsing import load_job, load_job_text
from tests.fixtures import JobTestCase

H3_PROFILE = {"backend": "h3", "settings": {"width": 832, "height": 448, "steps": 30}}
WAN_PROFILE = {"backend": "draw-things", "settings": {"config_file": "base.yaml"}}


def v2_job(**changes: Any) -> dict[str, Any]:
    """A valid mixed version-2 job: H3 r2av, H3 i2av, Draw Things i2v, H3 i2av; keyword arguments replace top-level keys."""
    data: dict[str, Any] = {
        "version": 2,
        "name": "story",
        "prompt_pairs": {"establish": {"positive": "two people stand together"}, "go": {"positive": "they walk"}, "paint": {"positive": "keep going", "negative": "blurry"}},
        "reference_pairs": {"duo": {"references": [{"kind": "image", "path": "a.png"}, {"kind": "image", "path": "b.png"}]}},
        "settings_profiles": {"h3-wide": copy.deepcopy(H3_PROFILE), "wan": copy.deepcopy(WAN_PROFILE)},
        "defaults": [
            {"backend": "h3", "pipeline": "r2av", "prompt_pair": "establish", "settings_profile": "h3-wide", "knobs": {"seconds": 5, "seed": 42, "timeout_seconds": 3600, "reference_pair": "duo"}},
            {"backend": "h3", "pipeline": "i2av", "prompt_pair": "go", "settings_profile": "h3-wide", "knobs": {"seconds": 4, "seed": 42, "timeout_seconds": 3600}},
            {"backend": "draw-things", "pipeline": "i2v", "prompt_pair": "paint", "settings_profile": "wan", "knobs": {"frames": 81, "seed": 42, "timeout_seconds": 1800}},
        ],
        "runs": [
            {"backend": "h3", "pipeline": "r2av"},
            {"backend": "h3", "pipeline": "i2av"},
            {"backend": "draw-things", "pipeline": "i2v"},
            {"backend": "h3", "pipeline": "i2av", "knobs": {"seconds": 8}},
        ],
    }
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class CompileV2TestCase(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_image("a.png", (64, 64))
        self.write_image("b.png", (64, 64))

    def load(self, data: dict[str, Any] | None = None, **options: Any) -> JobDefinition:
        return load_job(self.write_job(data if data is not None else v2_job()), self.global_config, self.params, **options)

    def invalid(self, data: dict[str, Any], field: str, text: str = "") -> InputError:
        with self.assertRaises(InputError) as caught:
            self.load(data)
        self.assertEqual(caught.exception.field, field, str(caught.exception))
        self.assertIn(text, str(caught.exception))
        return caught.exception


class CompiledRunTests(CompileV2TestCase):
    def test_a_mixed_job_compiles_each_run_with_its_own_backend_pipeline_and_knobs(self) -> None:
        job = self.load()
        self.assertEqual((job.version, job.run_count, job.backend, job.mode_label), (2, 4, "mixed", "mixed"))
        self.assertEqual([(run.number, run.backend, run.pipeline, run.prompt_pair, run.settings_profile) for run in job.effective_runs], [(1, "h3", "r2av", "establish", "h3-wide"), (2, "h3", "i2av", "go", "h3-wide"), (3, "draw-things", "i2v", "paint", "wan"), (4, "h3", "i2av", "go", "h3-wide")])
        self.assertEqual([job.run_backend(number) for number in range(1, 5)], ["h3", "h3", "draw-things", "h3"])
        self.assertEqual([job.run_needs_visual_input(number) for number in range(1, 6)], [False, True, True, True, False])
        self.assertEqual([pair.name for pair in job.schedule()], ["establish", "go", "paint", "go"])
        self.assertIsNone(job.input)

    def test_h3_runs_take_seconds_per_run_and_derive_the_frame_counts(self) -> None:
        job = self.load()
        short, long = job.effective_runs[1].knobs, job.effective_runs[3].knobs
        self.assertEqual((short["seconds"], short["requested_frames"], short["effective_frames"]), (4, 96, effective_frames(96)))
        self.assertEqual((long["seconds"], long["requested_frames"], long["effective_frames"]), (8, 192, effective_frames(192)))
        self.assertEqual(job.effective_runs[3].knob_sources["seconds"], "run")
        self.assertEqual(job.effective_runs[1].knob_sources["seconds"], "defaults")
        self.assertEqual((job.effective_runs[1].knob_sources["requested_frames"], job.effective_runs[1].knob_sources["effective_frames"]), ("derived", "backend"))
        self.assertEqual(job.effective_runs[3].knobs["effective_seconds"], effective_frames(192) / 24)

    def test_a_run_overrides_only_the_knob_it_names(self) -> None:
        job = self.load()
        replaced = job.effective_runs[3]
        self.assertEqual((replaced.knobs["seed"], replaced.knobs["timeout_seconds"], replaced.settings_profile), (42, 3600, "h3-wide"))
        self.assertEqual((replaced.knob_sources["seed"], replaced.knob_sources["timeout_seconds"], replaced.knob_sources["seconds"]), ("defaults", "defaults", "run"))

    def test_an_r2av_default_supplies_the_reference_pair_and_duration(self) -> None:
        first = self.load().effective_runs[0]
        self.assertEqual((first.knobs["reference_pair"], first.knobs["seconds"]), ("duo", 5))
        self.assertEqual(first.knob_sources["reference_pair"], "defaults")

    def test_draw_things_runs_take_exact_frames_per_run(self) -> None:
        data = v2_job()
        data["runs"] = [{"backend": "draw-things", "pipeline": "i2v", "knobs": {"frames": 49}}, {"backend": "draw-things", "pipeline": "i2v"}]
        data["input"] = "first-frame.png"
        data["defaults"] = [data["defaults"][2]]
        for key in ("reference_pairs",):
            data.pop(key)
        del data["settings_profiles"]["h3-wide"]
        data["prompt_pairs"] = {"paint": data["prompt_pairs"]["paint"]}
        job = self.load(data)
        self.assertEqual([(run.knobs["frames"], run.knob_sources["frames"]) for run in job.effective_runs], [(49, "run"), (81, "defaults")])
        self.assertEqual([run.backend_job.config_override.frame_count for run in job.effective_runs], [49, 81])
        self.assertEqual((job.backend, job.mode_label), ("draw-things", "i2v"))

    def test_a_draw_things_run_without_a_temporal_knob_keeps_the_profile_frame_count(self) -> None:
        self.write_base_config({"model": "base.ckpt", "width": 832, "height": 448, "numFrames": 33})
        data = self._draw_things_only()
        data["defaults"][0]["knobs"].pop("frames")
        run = self.load(data).effective_runs[0]
        self.assertEqual((run.knobs["frames"], run.knob_sources["frames"]), (33, "settings_profile"))

    def test_a_profile_with_no_frame_count_leaves_it_to_the_cli_and_records_none(self) -> None:
        data = self._draw_things_only()
        data["defaults"][0]["knobs"].pop("frames")
        run = self.load(data).effective_runs[0]
        self.assertNotIn("frames", run.knobs)
        self.assertIsNone(run.backend_job.config_override.frame_count)

    def test_a_run_without_a_seed_has_none_in_its_plan_and_the_job_has_no_configured_seed(self) -> None:
        data = v2_job()
        for block in data["defaults"]:
            block["knobs"].pop("seed")
        job = self.load(data)
        self.assertTrue(all("seed" not in run.knobs for run in job.effective_runs))
        self.assertEqual(job.configured_seed(), (None, "random"))
        self.assertIsNone(job.effective_runs[2].backend_job.config_override.seed)

    def test_the_job_has_a_configured_seed_only_when_every_run_shares_one(self) -> None:
        self.assertEqual(self.load().configured_seed(), (42, "run"))
        data = v2_job()
        data["runs"][1]["knobs"] = {"seed": 7}
        self.assertEqual(self.load(data).configured_seed(), (None, "random"))

    def test_each_runs_effective_settings_are_safe_and_name_its_profile(self) -> None:
        job = self.load()
        plan = job.effective_plan()
        self.assertEqual([item["settings_profile"] for item in plan], ["h3-wide", "h3-wide", "wan", "h3-wide"])
        self.assertEqual(plan[2]["settings"]["model"], "base.ckpt")
        self.assertEqual((plan[2]["settings"]["seed"], plan[2]["settings"]["seed_source"]), (42, "defaults"))
        self.assertNotIn("h3_runs", plan[0]["settings"])
        self.assertNotIn(str(self.root), repr(plan))

    def test_the_root_job_describes_no_single_run(self) -> None:
        job = self.load()
        self.assertEqual((job.config_file, job.model, job.video_format, job.backend_data), ("", "", None, None))
        self.assertEqual(job.backend_settings["format_version"], 2)
        self.assertEqual(job.run_timeout_seconds, 3600)

    def _draw_things_only(self) -> dict[str, Any]:
        data = v2_job()
        data["runs"] = [{"backend": "draw-things", "pipeline": "i2v"}]
        data["input"] = "first-frame.png"
        data["defaults"] = [data["defaults"][2]]
        data.pop("reference_pairs")
        del data["settings_profiles"]["h3-wide"]
        data["prompt_pairs"] = {"paint": data["prompt_pairs"]["paint"]}
        return data


class DrawThingsSecondsTests(CompileV2TestCase):
    def draw_things_only(self, **knobs: Any) -> dict[str, Any]:
        data = v2_job(input="first-frame.png")
        data["runs"] = [{"backend": "draw-things", "pipeline": "i2v", "knobs": knobs}]
        data["defaults"] = [data["defaults"][2]]
        data["defaults"][0]["knobs"].pop("frames")
        data.pop("reference_pairs")
        del data["settings_profiles"]["h3-wide"]
        data["prompt_pairs"] = {"paint": data["prompt_pairs"]["paint"]}
        return data

    def test_seconds_is_refused_without_an_authoritative_frame_rate(self) -> None:
        error = self.invalid(self.draw_things_only(seconds=5), "runs[0].knobs.seconds", "set knobs.frames")
        self.assertIn("base.ckpt", str(error))

    def test_a_base_configuration_key_is_never_taken_for_a_frame_rate(self) -> None:
        self.write_base_config({"model": "base.ckpt", "width": 832, "height": 448, "fps": 16, "frameRate": 16, "framesPerSecond": 16})
        self.invalid(self.draw_things_only(seconds=5), "runs[0].knobs.seconds", "set knobs.frames")

    def test_seconds_derives_the_frame_count_from_an_injected_authoritative_rate(self) -> None:
        backends = BackendRegistry((DrawThingsBackend({"base.ckpt": FrameRate(16.0, "docs/research/draw-things-frame-rate.md")}), H3Backend(H3Runtime(self.root / "h3", self.root / "model"))))
        job = self.load(self.draw_things_only(seconds=5), backends=backends)
        run = job.effective_runs[0]
        self.assertEqual((run.knobs["seconds"], run.knobs["frames"], run.knobs["nominal_fps"]), (5, 80, 16.0))
        self.assertEqual((run.knob_sources["frames"], run.knob_sources["nominal_fps"]), ("derived", "docs/research/draw-things-frame-rate.md"))
        self.assertEqual(run.backend_job.config_override.frame_count, 80)
        self.assertEqual(run.settings["frames"], 80)

    def test_a_model_without_a_rate_still_fails_when_another_has_one(self) -> None:
        backends = BackendRegistry((DrawThingsBackend({"other.ckpt": FrameRate(24.0, "note")}), DEFAULT_BACKENDS.get("h3")))
        self.invalid_with(backends, self.draw_things_only(seconds=5), "runs[0].knobs.seconds")

    def invalid_with(self, backends: BackendRegistry, data: dict[str, Any], field: str) -> None:
        with self.assertRaises(InputError) as caught:
            self.load(data, backends=backends)
        self.assertEqual(caught.exception.field, field)

    def test_seconds_and_frames_are_one_choice_and_a_run_replaces_the_default_whichever_it_names(self) -> None:
        data = self.draw_things_only(frames=40)
        data["defaults"][0]["knobs"]["frames"] = 81
        self.assertEqual(self.load(data).effective_runs[0].knobs["frames"], 40)
        data = self.draw_things_only(frames=3, seconds=2)
        self.invalid(data, "runs[0].knobs", "both seconds and frames")
        backends = BackendRegistry((DrawThingsBackend({"base.ckpt": FrameRate(10.0, "note")}), DEFAULT_BACKENDS.get("h3")))
        data = self.draw_things_only(seconds=3)
        data["defaults"][0]["knobs"]["frames"] = 81
        run = self.load(data, backends=backends).effective_runs[0]
        self.assertEqual((run.knobs["frames"], run.knobs["seconds"]), (30, 3))


class OrderIndependenceTests(CompileV2TestCase):
    def test_the_plan_does_not_depend_on_the_order_of_keys_catalogs_or_defaults(self) -> None:
        original = v2_job()
        shuffled = {key: original[key] for key in reversed(list(original))}
        for catalog in ("prompt_pairs", "settings_profiles", "reference_pairs"):
            shuffled[catalog] = dict(reversed(list(original[catalog].items())))
        shuffled["defaults"] = list(reversed(original["defaults"]))
        first, second = self.load(original), self.load(shuffled)
        self.assertEqual(first.effective_plan(), second.effective_plan())
        self.assertEqual([run.knob_sources for run in first.effective_runs], [run.knob_sources for run in second.effective_runs])
        self.assertEqual(first.configured_seed(), second.configured_seed())

    def test_a_runs_values_never_come_from_the_run_before_it(self) -> None:
        data = v2_job()
        data["runs"][1]["knobs"] = {"seconds": 9, "seed": 7}
        job = self.load(data)
        # Run 4 has the same selectors as run 2 and takes its own defaults, not run 2's overrides or run 3's profile.
        self.assertEqual((job.effective_runs[3].knobs["seconds"], job.effective_runs[3].knobs["seed"]), (8, 42))
        self.assertEqual(job.effective_runs[3].settings_profile, "h3-wide")


class DefaultsTests(CompileV2TestCase):
    def test_duplicate_unused_and_unknown_defaults_fail_at_their_own_field(self) -> None:
        data = v2_job()
        data["defaults"].append(copy.deepcopy(data["defaults"][0]))
        self.invalid(data, "defaults[3]", "duplicates the defaults for h3/r2av")
        data = v2_job()
        data["defaults"].append({"backend": "draw-things", "pipeline": "t2v"})
        self.invalid(data, "defaults", "unused selector draw-things/t2v")
        data = v2_job()
        data["defaults"][1]["extra"] = 1
        self.invalid(data, "defaults[1].extra", "not a known key")
        data = v2_job()
        data["defaults"][1]["pipeline"] = "t2v"
        self.invalid(data, "defaults[1].pipeline", "for h3")

    def test_an_invalid_default_fails_even_when_every_run_overrides_it(self) -> None:
        data = v2_job()
        data["defaults"][1]["knobs"]["seconds"] = 99
        data["runs"][1]["knobs"] = {"seconds": 4}
        data["runs"][3]["knobs"] = {"seconds": 4}
        self.invalid(data, "defaults[1].knobs.seconds", "from 1 to 15")
        data = v2_job()
        data["defaults"][2]["knobs"]["timeout_seconds"] = -1
        data["runs"][2]["knobs"] = {"timeout_seconds": 60}
        self.invalid(data, "defaults[2].knobs.timeout_seconds", "positive")

    def test_a_default_selecting_an_undeclared_or_foreign_name_fails_at_the_default(self) -> None:
        data = v2_job()
        data["defaults"][0]["prompt_pair"] = "missing"
        data["runs"][0]["prompt_pair"] = "establish"
        self.invalid(data, "defaults[0].prompt_pair", "undeclared prompt pair")
        data = v2_job()
        data["defaults"][0]["settings_profile"] = "wan"
        data["runs"][0]["settings_profile"] = "h3-wide"
        self.invalid(data, "defaults[0].settings_profile", "draw-things profile, not a h3 one")
        data = v2_job()
        data["defaults"][0]["knobs"]["reference_pair"] = "ghost"
        data["runs"][0]["knobs"] = {"reference_pair": "duo"}
        self.invalid(data, "defaults[0].knobs.reference_pair", "undeclared reference pair")

    def test_null_unknown_and_inapplicable_knobs_are_refused(self) -> None:
        for knobs, field, text in (
            ({"seed": None}, "knobs.seed", "must not be null"),
            ({"colour": 1}, "knobs.colour", "not a known key"),
            ({"frames": 5}, "knobs.frames", "does not apply to h3/i2av"),
            ({"reference_pair": "duo"}, "knobs.reference_pair", "does not apply to h3/i2av"),
            ({"seed": 2**32}, "knobs.seed", "0 to 4294967295"),
            ({"seconds": 0}, "knobs.seconds", "positive"),
            ({"seconds": float("inf")}, "knobs.seconds", "finite"),
            ({"timeout_seconds": float("nan")}, "knobs.timeout_seconds", "finite"),
        ):
            with self.subTest(knobs=knobs):
                data = v2_job()
                data["runs"][1]["knobs"] = knobs
                self.invalid(data, f"runs[1].{field}", text)

    def test_an_i2i_run_has_no_temporal_knob(self) -> None:
        data = self._i2i_job()
        data["runs"][0]["knobs"] = {"seconds": 2}
        self.invalid(data, "runs[0].knobs.seconds", "does not apply to draw-things/i2i")
        data = self._i2i_job()
        data["runs"][0]["knobs"] = {"frames": 2}
        self.invalid(data, "runs[0].knobs.frames", "does not apply to draw-things/i2i")
        run = self.load(self._i2i_job()).effective_runs[0]
        self.assertEqual(set(run.knobs), {"timeout_seconds", "seed"})
        self.assertEqual(run.backend_job.extension, "png")

    def _i2i_job(self) -> dict[str, Any]:
        return v2_job(
            input="first-frame.png",
            prompt_pairs={"paint": {"positive": "x"}},
            reference_pairs=None,
            settings_profiles={"wan": copy.deepcopy(WAN_PROFILE)},
            defaults=[{"backend": "draw-things", "pipeline": "i2i", "prompt_pair": "paint", "settings_profile": "wan", "knobs": {"timeout_seconds": 60, "seed": 5}}],
            runs=[{"backend": "draw-things", "pipeline": "i2i"}],
        )

    def test_required_values_have_no_silent_fallback(self) -> None:
        for path, field in (
            (("runs", 1, "prompt_pair"), "runs[1].prompt_pair"),
            (("runs", 1, "settings_profile"), "runs[1].settings_profile"),
        ):
            with self.subTest(field=field):
                data = v2_job()
                data["defaults"][1].pop(path[2])
                self.invalid(data, field, "required on the run or in the matching defaults")
        for key, field in (("timeout_seconds", "runs[1].knobs.timeout_seconds"), ("seconds", "runs[1].knobs.seconds")):
            with self.subTest(field=field):
                data = v2_job()
                data["defaults"][1]["knobs"].pop(key)
                self.invalid(data, field, "required on the run or in the matching defaults")
        data = v2_job()
        data["defaults"][0]["knobs"].pop("reference_pair")
        self.invalid(data, "runs[0].knobs.reference_pair", "required")

    def test_a_default_does_not_apply_to_another_pipeline(self) -> None:
        data = v2_job()
        data["defaults"].pop(1)
        self.invalid(data, "runs[1].prompt_pair", "required on the run or in the matching defaults")

    def test_unused_catalog_entries_are_refused(self) -> None:
        for catalog, entry in (("prompt_pairs", {"spare": {"positive": "x"}}), ("settings_profiles", {"spare": copy.deepcopy(H3_PROFILE)}), ("reference_pairs", {"spare": {"references": [{"kind": "image", "path": "a.png"}]}})):
            with self.subTest(catalog=catalog):
                data = v2_job()
                data[catalog] = {**data[catalog], **entry}
                self.invalid(data, f"{catalog}.spare", "no run selects it")


class ShapeTests(CompileV2TestCase):
    def test_removed_version_1_keys_name_their_version_2_home(self) -> None:
        for key, value, text in (
            ("mode", "i2v", "pipeline"),
            ("run_count", 3, "length of 'runs'"),
            ("backend", "h3", "runs"),
            ("h3_runs", [], "runs"),
            ("backend_settings", {}, "settings profile"),
            ("config_file", "base.yaml", "settings.config_file"),
            ("config_override", {}, "settings.config_override"),
            ("color", {}, "settings.color"),
            ("run_timeout_seconds", 5, "knobs.timeout_seconds"),
            ("desired_input_width", 5, "input_resize"),
            ("desired_input_height", 5, "input_resize"),
            ("max_input_crop_percent", 5, "input_resize"),
        ):
            with self.subTest(key=key):
                self.invalid(v2_job(**{key: value}), key, text)
        self.invalid(v2_job(output={"extension": "mov"}), "output.extension", "settings profile")
        self.invalid(v2_job(output={"video_format": "h264"}), "output.video_format", "draw-things settings profile")
        data = v2_job()
        data["runs"][0]["run"] = 1
        self.invalid(data, "runs[0].run", "position in 'runs'")

    def test_the_run_list_and_its_items_are_closed(self) -> None:
        self.invalid(v2_job(runs=[]), "runs", "nonempty list")
        data = v2_job()
        data["runs"][0]["settings"] = {}
        self.invalid(data, "runs[0].settings", "not a known key")
        data = v2_job()
        data["runs"][0] = {"backend": "h3"}
        self.invalid(data, "runs[0]", "backend and pipeline")
        data = v2_job()
        data["runs"][0]["backend"] = "draw-things-cli"
        self.invalid(data, "runs[0].backend", "draw-things or h3")
        data = v2_job()
        data["runs"][0]["pipeline"] = "i2v"
        self.invalid(data, "runs[0].pipeline", "i2av, r2av for h3")

    def test_profile_settings_are_closed_per_backend_and_keep_knobs_out(self) -> None:
        for profile, key, field, text in (
            ("h3-wide", "frames", "settings_profiles.h3-wide.settings.frames", "run knob"),
            ("h3-wide", "seconds", "settings_profiles.h3-wide.settings.seconds", "run knob"),
            ("h3-wide", "seed", "settings_profiles.h3-wide.settings.seed", "run knob"),
            ("h3-wide", "config_file", "settings_profiles.h3-wide.settings.config_file", "not a known key"),
            ("wan", "steps", "settings_profiles.wan.settings.steps", "not a known key"),
            ("wan", "desired_input_width", "settings_profiles.wan.settings.desired_input_width", "not a known key"),
        ):
            with self.subTest(key=key):
                data = v2_job()
                data["settings_profiles"][profile]["settings"][key] = 5
                self.invalid(data, field, text)
        data = v2_job()
        data["settings_profiles"]["wan"]["settings"]["config_override"] = {"frame_count": 9}
        self.invalid(data, "settings_profiles.wan.settings.config_override.frame_count", "run knob")
        data = v2_job()
        data["settings_profiles"]["wan"]["settings"]["config_override"] = {"seed": 9}
        self.invalid(data, "settings_profiles.wan.settings.config_override.seed", "run knob")
        data = v2_job()
        del data["settings_profiles"]["h3-wide"]["settings"]["steps"]
        self.invalid(data, "settings_profiles.h3-wide.settings.steps", "required")

    def test_a_backend_error_names_the_version_2_field(self) -> None:
        data = v2_job()
        data["settings_profiles"]["h3-wide"]["settings"]["width"] = 833
        self.invalid(data, "settings_profiles.h3-wide.settings.width", "divisible by 32")
        data = v2_job()
        data["settings_profiles"]["wan"]["settings"]["config_override"] = {"steps": 0}
        self.invalid(data, "settings_profiles.wan.settings.config_override.steps", ">= 1")
        data = v2_job()
        data["settings_profiles"]["h3-wide"]["settings"]["color"] = {"anchor": "sideways"}
        self.invalid(data, "settings_profiles.h3-wide.settings.color.anchor", "")

    def test_a_negative_prompt_cannot_reach_h3(self) -> None:
        data = v2_job()
        data["prompt_pairs"]["go"]["negative"] = "no"
        self.invalid(data, "prompt_pairs.go.negative", "not supported by h3")

    def test_catalog_names_and_the_prompt_pair_shape_are_checked(self) -> None:
        self.invalid(v2_job(prompt_pairs={"Bad Name": {"positive": "x"}}), "prompt_pairs", "lowercase")
        data = v2_job()
        data["prompt_pairs"]["go"] = {"positive": "x", "runs": [1]}
        self.invalid(data, "prompt_pairs.go.runs", "not a known key")
        data = v2_job()
        data["prompt_pairs"]["go"] = {"positive": " "}
        self.invalid(data, "prompt_pairs.go.positive", "non-empty")

    def test_the_initial_input_is_required_or_forbidden_by_the_first_run(self) -> None:
        self.invalid(v2_job(input="first-frame.png"), "input", "forbidden when the first run uses r2av")
        data = v2_job()
        data["runs"] = data["runs"][1:]
        data.pop("reference_pairs")
        data["defaults"].pop(0)
        data["prompt_pairs"].pop("establish")
        self.invalid(data, "input", "required when the first run uses i2av")
        self.invalid({**data, "input": "missing.png"}, "input", "does not exist")
        job = self.load({**data, "input": "first-frame.png"})
        self.assertEqual(job.input, self.input_directory / "first-frame.png")
        assert job.input_copy is not None
        self.assertEqual(job.input_copy.target_size, (832, 448))
        self.assertEqual((job.run_needs_visual_input(1), job.mode_label), (True, "mixed"))

    def test_a_handoff_into_a_run_without_input_resize_must_match_its_size(self) -> None:
        data = v2_job()
        data["settings_profiles"]["h3-wide"]["settings"].update(width=768, height=1344)
        error = self.invalid(data, "runs[2].settings_profile", "generates 832x448, but run 2 hands off 768x1344")
        self.assertIn("input_resize", str(error))
        data["settings_profiles"]["wan"]["settings"]["input_resize"] = {"desired_width": 768, "desired_height": 1344}
        job = self.load(data)
        assert job.effective_runs[2].backend_job.input_resize is not None
        self.assertEqual(job.effective_runs[2].backend_job.input_resize.target_size, (768, 1344))

    def test_input_resize_keeps_the_version_1_rules(self) -> None:
        data = v2_job()
        data["settings_profiles"]["wan"]["settings"]["input_resize"] = {"desired_width": 800, "max_crop_percent": 10}
        job = self.load(data)
        assert job.effective_runs[2].backend_job.input_resize is not None
        # The version-1 rule floors a desired size to a multiple of 64 and derives the other side from the handoff's aspect.
        self.assertEqual(job.effective_runs[2].backend_job.input_resize.target_size, (768, 384))
        data["settings_profiles"]["wan"]["settings"]["input_resize"]["max_crop_percent"] = 1
        error = self.invalid(data, "settings_profiles.wan.settings.input_resize.max_crop_percent", "handoff-from-run-2")
        self.assertNotIn("max_input_crop_percent", str(error))
        self.invalid({**data, "settings_profiles": {**data["settings_profiles"], "wan": {"backend": "draw-things", "settings": {"config_file": "base.yaml", "input_resize": {"max_crop_percent": 1}}}}}, "settings_profiles.wan.settings.input_resize.max_crop_percent", "requires exactly one")
        data["settings_profiles"]["wan"]["settings"]["input_resize"] = {"desired_w": 800}
        self.invalid(data, "settings_profiles.wan.settings.input_resize.desired_w", "not a known key")


class CutoverTests(CompileV2TestCase):
    def text(self, data: dict[str, Any]) -> str:
        return self.write_legacy_job(data).read_text(encoding="utf-8")

    def test_a_version_1_file_is_refused_with_one_pointer_to_version_2(self) -> None:
        from tests.fixtures import job_data

        path = self.root / "job.yaml"
        with self.assertRaises(InputError) as caught:
            load_job_text(self.text(job_data()), path, self.global_config, self.params, allow_version_1=False)
        self.assertEqual(caught.exception.field, "version")
        self.assertIn("version 1 job files are no longer accepted", str(caught.exception))
        self.assertIn("version: 2", str(caught.exception))
        with self.assertRaises(InputError) as unversioned:
            load_job_text("name: x\n", path, self.global_config, self.params, allow_version_1=False)
        self.assertEqual((unversioned.exception.field, "must be 2" in str(unversioned.exception)), ("version", True))
        for bad in ("true", "3", "'2'"):
            with self.assertRaises(InputError):
                load_job_text(f"version: {bad}\nname: x\n", path, self.global_config, self.params, allow_version_1=False)

    def test_a_version_2_file_is_accepted_whether_or_not_version_1_is_allowed(self) -> None:
        path = self.root / "job.yaml"
        text = self.text(v2_job())
        for allowed in (True, False):
            self.assertEqual(load_job_text(text, path, self.global_config, self.params, allow_version_1=allowed).version, 2)

    def test_a_version_1_file_still_parses_when_allowed(self) -> None:
        from tests.fixtures import job_data

        job = load_job_text(self.text(job_data()), self.root / "job.yaml", self.global_config, self.params, allow_version_1=True)
        self.assertEqual((job.version, job.effective_runs, job.mode_label), (1, (), "i2v"))


class LegacyAccessorTests(CompileV2TestCase):
    def test_a_version_1_job_answers_the_per_run_questions_from_its_own_fields(self) -> None:
        from tests.fixtures import job_data

        job = load_job_text(self.write_legacy_job(job_data(run_count=3, prompt_pairs=[{"name": "a", "positive": "x"}])).read_text(encoding="utf-8"), Path("job.yaml"), self.global_config, self.params, allow_version_1=True)
        self.assertEqual([job.run_needs_visual_input(number) for number in range(0, 5)], [False, True, True, True, False])
        self.assertEqual((job.run_backend(2), job.job_for_run(2) is job, job.effective_run(1)), ("draw-things-cli", True, None))
        t2v = load_job_text(self.write_legacy_job(job_data(mode="t2v", input=None, run_count=3, prompt_pairs=[{"name": "a", "positive": "x"}])).read_text(encoding="utf-8"), Path("job.yaml"), self.global_config, self.params, allow_version_1=True)
        # A version-1 t2v job's later runs continue from the previous run's last frame.
        self.assertEqual([t2v.run_needs_visual_input(number) for number in (1, 2, 3)], [False, True, True])
        self.assertEqual([t2v.run_checks_input(number) for number in (1, 2, 3)], [False, False, False])


class TextTests(CompileV2TestCase):
    def test_the_summary_has_a_row_per_run_with_its_resolved_values(self) -> None:
        from framecontrol.jobs.text import job_summary

        rows = dict(job_summary(self.load()))
        self.assertEqual((rows["format"], rows["backend"], rows["mode"], rows["runs"]), ("version 2", "mixed", "mixed", "4 (establish, go, paint, go)"))
        self.assertEqual(rows["run 1"], "h3/r2av, pair establish, profile h3-wide, 5s requested, 120 frames requested, 124 effective, references duo, seed 42, time limit 1 h")
        self.assertEqual(rows["run 4"], "h3/i2av, pair go, profile h3-wide, 8s requested, 192 frames requested, 192 effective, seed 42, time limit 1 h")
        self.assertEqual(rows["run 3"], "draw-things/i2v, pair paint, profile wan, 81 frames, seed 42, time limit 30 min")

    def test_an_unseeded_run_says_it_uses_the_execution_seed(self) -> None:
        from framecontrol.jobs.text import job_summary

        data = v2_job()
        for block in data["defaults"]:
            block["knobs"].pop("seed")
        rows = dict(job_summary(self.load(data)))
        self.assertIn("execution seed, time limit", rows["run 2"])

    def test_the_base_configurations_own_seed_is_reported_as_ignored(self) -> None:
        from framecontrol.jobs.text import ignored_config_lines

        self.write_base_config({"model": "base.ckpt", "width": 832, "height": 448, "seed": 5})
        lines = ignored_config_lines(self.load())
        self.assertEqual(lines, ["Ignoring seed (5) from config_file base.yaml (settings profile wan): a run's seed is its knobs.seed, else the execution seed"])
        self.assertEqual(self.load().effective_runs[2].backend_job.base_config.get("seed"), None)
