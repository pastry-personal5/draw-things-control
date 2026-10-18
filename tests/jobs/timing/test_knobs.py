"""The knob types, the factory pair, and the mappings the files hold: bounds, per-key validity, round trips, and the examples."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND
from framecontrol.core.paths import PROJECT_ROOT
from framecontrol.jobs.timing.factory import DRAW_THINGS_I8X_E0053, H3_R2AV_E0051_E0054, factory_calibration, factory_tunables
from framecontrol.jobs.timing.knob_codec import backend_from_mapping, backend_to_mapping, flatten, schema_state, tunables_from_file, tunables_to_file
from framecontrol.jobs.timing.knobs import BOUNDS, KNOBS_FACTORY, KNOBS_PINNED, SEARCHED, Tunables, tunables_from_mapping
from framecontrol.services.timing_knobs import EXAMPLE_SUFFIX, factory_texts


class TunablesTests(unittest.TestCase):
    def test_the_factory_values_are_the_milestone_table(self) -> None:
        tunables = factory_tunables()
        self.assertEqual(tunables.searched, {"pause_factor": 3.0, "size_offset_shrinkage": 3.0, "prior_weight_cap": 12.0, "half_life_runs": 60.0, "unseen_knob_inflation": 1.5, "rho": 0.5, "slope_level": 0.95, "factory_spread": 0.25})
        self.assertEqual((tunables.min_signature_runs, tunables.min_signature_executions, tunables.min_fit_runs, tunables.min_fit_executions), (3, 2, 6, 3))
        self.assertEqual((tunables.publish_interval_seconds, tunables.ring_size, tunables.backup_count), (2.0, 300, 20))

    def test_every_searched_tunable_has_bounds_that_hold_its_factory_value(self) -> None:
        for name in SEARCHED:
            low, high = BOUNDS[name]
            self.assertLessEqual(low, getattr(Tunables(), name), name)
            self.assertGreaterEqual(high, getattr(Tunables(), name), name)

    def test_an_invalid_value_is_named_and_its_siblings_stand(self) -> None:
        tunables, problems = tunables_from_mapping({"pause_factor": 99, "rho": 0.7, "half_life_runs": "soon", "slope_level": 0.9, "nonsense": 1, "min_fit_runs": 7.5})
        self.assertEqual((tunables.pause_factor, tunables.rho, tunables.half_life_runs, tunables.slope_level, tunables.min_fit_runs), (3.0, 0.7, 60.0, 0.95, 6))
        self.assertEqual(len(problems), 5)
        self.assertTrue(any(text.startswith("pause_factor") for text in problems))
        self.assertTrue(any("nonsense" in text for text in problems))

    def test_the_slope_level_is_one_the_quantile_tables_hold(self) -> None:
        self.assertEqual(tunables_from_mapping({"slope_level": 0.8})[0].slope_level, 0.8)
        self.assertEqual(tunables_from_mapping({"slope_level": 0.9})[1][0].split(":")[0], "slope_level")

    def test_booleans_and_nonfinite_numbers_are_not_numbers(self) -> None:
        tunables, problems = tunables_from_mapping({"pause_factor": True, "rho": float("nan"), "penalties": [1.0, float("inf")]})
        self.assertEqual(tunables, Tunables())
        self.assertEqual(len(problems), 3)

    def test_the_file_form_round_trips(self) -> None:
        edited = replace(Tunables(), pause_factor=4.0, penalties=(0.5, 5.0), backup_count=7)
        restored, problems = tunables_from_file(tunables_to_file(edited))
        self.assertEqual((restored, problems), (edited, []))

    def test_the_searched_tunables_and_the_floors_are_separate_sections(self) -> None:
        document = tunables_to_file(Tunables())
        self.assertEqual(set(document["tunables"]), set(SEARCHED))
        self.assertIn("ring_size", document["floors"])


class FactoryCalibrationTests(unittest.TestCase):
    def test_each_backend_has_its_measured_point_and_pipeline(self) -> None:
        calibration = factory_calibration()
        self.assertEqual(set(calibration.backends), {DRAW_THINGS_BACKEND, H3_BACKEND})
        self.assertIs(calibration.backends[DRAW_THINGS_BACKEND].points[0], DRAW_THINGS_I8X_E0053)
        self.assertIs(calibration.backends[H3_BACKEND].points[0], H3_R2AV_E0051_E0054)
        h3 = calibration.backends[H3_BACKEND].pipelines["r2av"]
        self.assertEqual((h3.step.seconds, h3.step.frames, h3.step.pixels, h3.source), (138.6683, 362, 832 * 448, KNOBS_FACTORY))

    def test_the_power_law_exponents_are_priors_not_measurements(self) -> None:
        calibration = factory_calibration()
        self.assertEqual(calibration.backends[H3_BACKEND].pipelines["r2av"].step.frame_exponent, 1.6)
        self.assertEqual(calibration.backends[DRAW_THINGS_BACKEND].pipelines["i2v"].step.frame_exponent, 1.0)
        # A still-image job borrows the anchor's figures with a gentler frame exponent.
        self.assertEqual(calibration.backends[DRAW_THINGS_BACKEND].pipeline("i2i").step.frame_exponent, 0.5)

    def test_the_step_rate_is_the_measured_step_at_the_measured_workload(self) -> None:
        step = factory_calibration().backends[DRAW_THINGS_BACKEND].pipelines["i2v"].step
        self.assertEqual(step.rate(step.pixels, step.frames), step.seconds)
        self.assertAlmostEqual(step.rate(step.pixels * 2, step.frames), step.seconds * 2)

    def test_a_pin_names_the_figure_it_touches(self) -> None:
        calibration = factory_calibration()
        pinned = replace(calibration, pinned=frozenset({f"{H3_BACKEND}/pipelines/r2av/step/seconds"}))
        self.assertEqual(pinned.source_of(H3_BACKEND, "r2av"), KNOBS_PINNED)
        self.assertEqual(pinned.source_of(H3_BACKEND, "i2av"), KNOBS_FACTORY)
        self.assertEqual(pinned.source_of(DRAW_THINGS_BACKEND, "i2v"), KNOBS_FACTORY)


class CodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = factory_calibration().backends[H3_BACKEND]

    def test_the_factory_survives_a_round_trip_through_yaml(self) -> None:
        text = yaml.safe_dump(backend_to_mapping(self.factory), sort_keys=False)
        restored, problems = backend_from_mapping(yaml.safe_load(text), self.factory)
        self.assertEqual(problems, [])
        self.assertEqual(backend_to_mapping(restored), backend_to_mapping(self.factory))

    def test_an_invalid_key_costs_only_itself(self) -> None:
        mapping = backend_to_mapping(self.factory)
        mapping["pipelines"]["r2av"]["step"]["seconds"] = -4
        mapping["pipelines"]["r2av"]["step"]["frame_exponent"] = 1.9
        mapping["pipelines"]["r2av"]["spreads"]["tau"] = "wide"
        restored, problems = backend_from_mapping(mapping, self.factory)
        step = restored.pipelines["r2av"].step
        self.assertEqual((step.seconds, step.frame_exponent), (138.6683, 1.9))
        self.assertEqual(restored.pipelines["r2av"].spreads.tau, 0.1)
        self.assertEqual(len(problems), 2)

    def test_a_pipeline_the_factory_lacks_starts_from_the_anchor(self) -> None:
        mapping = {"schema": 1, "pipelines": {"i2av": {"step": {"shift": 0.4}}}}
        restored, problems = backend_from_mapping(mapping, self.factory)
        self.assertEqual(problems, [])
        self.assertEqual(restored.pipelines["i2av"].step.shift, 0.4)
        self.assertEqual(restored.pipelines["i2av"].step.seconds, 138.6683)
        self.assertEqual(restored.pipelines["i2av"].pipeline, "i2av")

    def test_nonsense_in_a_section_is_a_problem_not_an_error(self) -> None:
        restored, problems = backend_from_mapping({"schema": 1, "pipelines": {"r2av": {"step": "slow", "stages": 3, "spreads": []}, "***": {}, "i2av": 1}}, self.factory)
        self.assertEqual(restored.pipelines["r2av"], self.factory.pipelines["r2av"])
        self.assertGreaterEqual(len(problems), 5)

    def test_flatten_lists_the_numbers_a_person_can_pin_and_not_the_read_only_blocks(self) -> None:
        flat = flatten(backend_to_mapping(self.factory))
        self.assertEqual(flat["pipelines/r2av/step/seconds"], 138.6683)
        self.assertEqual(flat["pipelines/r2av/stages/ffmpeg/seconds"], 189.977)
        self.assertNotIn("pipelines/r2av/evidence/runs", flat)
        self.assertNotIn("pipelines/r2av/source", flat)
        self.assertNotIn("updated_at", flat)
        self.assertNotIn("notes", flat)

    def test_the_schema_states(self) -> None:
        self.assertEqual([schema_state(item) for item in ({"schema": 1}, {"schema": 2}, {"schema": 0}, {"schema": "1"}, [], None)], ["ok", "newer", "invalid", "invalid", "invalid", "invalid"])


class ExampleFileTests(unittest.TestCase):
    """The checked-in examples are the factory code, exported."""

    def test_the_example_files_equal_the_factory(self) -> None:
        directory = PROJECT_ROOT / "data" / "timing-data-analysis"
        for name, text in factory_texts().items():
            self.assertTrue(name.endswith(EXAMPLE_SUFFIX))
            self.assertEqual((Path(directory) / name).read_text(encoding="utf-8"), text, f"{name} is stale: run `fc timing knobs export-defaults`")
