"""The knob files as the forecast sees them: per-key fallbacks, hand-edit pins, damaged files, and the commands' operations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from framecontrol.core.backend_ids import DRAW_THINGS_BACKEND, H3_BACKEND
from framecontrol.core.errors import FramecontrolError
from framecontrol.core.paths import ProjectPaths
from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.knobs import KNOBS_FACTORY, KNOBS_PINNED, Tunables
from framecontrol.services.knob_updater import KnobUpdateEngine
from framecontrol.services.timing_knob_files import KnobFiles, dump_yaml
from framecontrol.services.timing_knobs import KnobReader, check, export_defaults, load_knobs, reset, rollback, show
from tests.timing.builders import dt_run, dt_workload, h3_run, h3_workload

H3_FILE, DT_FILE, TUNING = "h3-metal-cli.yaml", "draw-things-cli.yaml", "tuning.yaml"
H3_STEP = "pipelines/r2av/step/seconds"


class KnobFilesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.files = KnobFiles(self.paths.timing_data)
        self.engine = KnobUpdateEngine(self.files, clock=lambda: "2026-10-18T12:00:00+09:00")

    def first_update(self, runs: list | None = None) -> None:
        self.engine.update(runs or [])

    def read(self, name: str) -> dict[str, Any]:
        return yaml.safe_load((self.files.directory / name).read_text(encoding="utf-8"))

    def write(self, name: str, document: Any) -> None:
        (self.files.directory / name).write_text(document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def edit_h3(self, change: Any) -> None:
        document = self.read(H3_FILE)
        change(document)
        self.write(H3_FILE, document)

    def runs(self, count: int, *, backend: str = H3_BACKEND, first: int = 1) -> list:
        make = (lambda e: h3_run(e, 1, step=130.0, workload=h3_workload(pixels=832 * 448, frames=362, steps=30, references=3))) if backend == H3_BACKEND else (lambda e: dt_run(e, 1, step=12.0, workload=dt_workload(pixels=294_912, frames=33, steps=25)))
        return [make(execution) for execution in range(first, first + count)]


class AbsentAndFactoryTests(KnobFilesTestCase):
    def test_no_directory_is_the_factory_with_nothing_to_report(self) -> None:
        loaded = load_knobs(self.files)
        self.assertEqual((loaded.tunables, loaded.issues, loaded.suspended), (factory_tunables(), (), frozenset()))
        self.assertEqual(loaded.calibration.backends, factory_calibration().backends)
        self.assertFalse(any(state.present for state in loaded.files.values()))

    def test_the_first_update_creates_the_factory_files_and_they_load_as_the_factory(self) -> None:
        self.first_update()
        for name in (TUNING, DT_FILE, H3_FILE):
            self.assertTrue((self.files.directory / name).is_file(), name)
        loaded = load_knobs(self.files)
        self.assertEqual((loaded.issues, loaded.suspended, loaded.calibration.pinned), ((), frozenset(), frozenset()))
        self.assertEqual(loaded.calibration.backends[H3_BACKEND].pipelines["r2av"], factory_calibration().backends[H3_BACKEND].pipelines["r2av"])

    def test_the_files_the_machine_wrote_are_the_examples(self) -> None:
        self.first_update()
        export_defaults(self.files.directory / "examples")
        for name in (DT_FILE, H3_FILE, TUNING):
            example = self.files.directory / "examples" / name.replace(".yaml", ".example.yaml")
            self.assertEqual(example.read_text(encoding="utf-8"), (self.files.directory / name).read_text(encoding="utf-8"))


class HandEditTests(KnobFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first_update()

    def test_a_changed_value_is_pinned_and_in_force(self) -> None:
        self.edit_h3(lambda d: d["pipelines"]["r2av"]["step"].update(seconds=200.0))
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.calibration.backends[H3_BACKEND].pipelines["r2av"].step.seconds, 200.0)
        self.assertEqual(loaded.files[H3_FILE].pins, {H3_STEP: 200.0})
        self.assertIn(f"{H3_BACKEND}/{H3_STEP}", loaded.calibration.pinned)
        self.assertEqual(loaded.calibration.source_of(H3_BACKEND, "r2av"), KNOBS_PINNED)
        self.assertEqual(loaded.calibration.source_of(DRAW_THINGS_BACKEND, "i2v"), KNOBS_FACTORY)

    def test_a_comment_or_a_notes_change_pins_nothing(self) -> None:
        text = (self.files.directory / H3_FILE).read_text(encoding="utf-8")
        self.write(H3_FILE, "# my own comment\n" + text.replace("notes: ''", "notes: tuned for the long clips"))
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.files[H3_FILE].pins, {})
        self.assertEqual(loaded.calibration.backends[H3_BACKEND].notes, "tuned for the long clips")

    def test_a_newly_added_key_is_a_pin(self) -> None:
        self.edit_h3(lambda d: d["pipelines"]["r2av"]["step"]["knobs"].update({"guidance_scale": 0.2}))
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.files[H3_FILE].pins, {"pipelines/r2av/step/knobs/guidance_scale": 0.2})

    def test_an_edit_to_the_evidence_or_updated_at_is_ignored_with_a_warning(self) -> None:
        self.edit_h3(lambda d: (d["pipelines"]["r2av"]["evidence"].update(runs=999), d.update(updated_at="never")))
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.calibration.backends[H3_BACKEND].pipelines["r2av"].runs, 0)
        self.assertIsNone(loaded.calibration.backends[H3_BACKEND].updated_at)
        messages = [issue.message for issue in loaded.issues]
        self.assertTrue(any("`updated_at` is read-only" in message for message in messages))
        self.assertTrue(any("evidence" in message and "read-only" in message for message in messages))
        self.assertEqual(loaded.files[H3_FILE].pins, {})

    def test_an_invalid_value_uses_the_factorys_and_keeps_its_siblings(self) -> None:
        self.edit_h3(lambda d: d["pipelines"]["r2av"]["step"].update(seconds=-1, frame_exponent=1.9))
        loaded = load_knobs(self.files)
        step = loaded.calibration.backends[H3_BACKEND].pipelines["r2av"].step
        self.assertEqual((step.seconds, step.frame_exponent), (138.6683, 1.9))
        self.assertEqual(set(loaded.files[H3_FILE].pins), {"pipelines/r2av/step/frame_exponent"})
        self.assertTrue(any("pipelines/r2av/step/seconds" in str(issue) for issue in loaded.issues))

    def test_the_tuning_file_applies_per_key(self) -> None:
        document = self.read(TUNING)
        document["tunables"].update(pause_factor=4.5, rho="half")
        document["floors"].update(ring_size=50)
        self.write(TUNING, document)
        loaded = load_knobs(self.files)
        self.assertEqual((loaded.tunables.pause_factor, loaded.tunables.rho, loaded.tunables.ring_size), (4.5, 0.5, 50))
        self.assertEqual(loaded.files[TUNING].pins, {"tunables/pause_factor": 4.5, "floors/ring_size": 50})
        self.assertEqual({row.path for row in show(self.files) if row.file == TUNING and row.source == "pinned"}, {"tunables/pause_factor", "floors/ring_size"})
        self.assertEqual(check(self.files).pins[TUNING], {"tunables/pause_factor": 4.5, "floors/ring_size": 50})
        self.assertTrue(any(issue.file == TUNING and "rho" in issue.message for issue in loaded.issues))

        self.engine.update([])
        document["tunables"]["pause_factor"] = 3.0
        document["floors"]["ring_size"] = 300
        self.write(TUNING, document)
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.files[TUNING].pins, {"tunables/pause_factor": 3.0, "floors/ring_size": 300})
        self.assertTrue({"tuning/tunables/pause_factor", "tuning/floors/ring_size"} <= loaded.calibration.pinned)
        plan = FittedModel([], tunables=loaded.tunables, calibration=loaded.calibration).plan(H3_BACKEND, "r2av", h3_workload(pixels=832 * 448, frames=362, steps=30, references=3), {"model": "h3"})
        self.assertEqual(plan.knobs, "pinned")

    def test_the_reader_notices_a_change_and_not_a_touch(self) -> None:
        reader = KnobReader(self.files)
        first = reader.current()
        self.assertIs(reader.current(), first)
        self.edit_h3(lambda d: d["pipelines"]["r2av"]["step"].update(seconds=150.0))
        self.assertEqual(reader.current().calibration.backends[H3_BACKEND].pipelines["r2av"].step.seconds, 150.0)


class DamagedFileTests(KnobFilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first_update(self.runs(2))
        self.first_update(self.runs(4))

    def break_h3(self, text: str = "pipelines: [unclosed") -> None:
        self.write(H3_FILE, text)

    def test_a_malformed_file_uses_the_newest_valid_backup_and_is_left_untouched(self) -> None:
        learned = load_knobs(self.files).calibration.backends[H3_BACKEND].pipelines["r2av"]
        self.assertTrue(learned.learned)
        self.break_h3()
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.files[H3_FILE].origin, "backup")
        self.assertIn(H3_FILE, loaded.suspended)
        self.assertEqual(loaded.calibration.backends[H3_BACKEND].pipelines["r2av"].source, "provisional")
        self.assertEqual((self.files.directory / H3_FILE).read_text(encoding="utf-8"), "pipelines: [unclosed")
        self.assertTrue(any(issue.level == "error" and issue.file == H3_FILE for issue in loaded.issues))

    def test_with_no_backup_it_is_the_factory(self) -> None:
        for backup in self.files.backups_of(H3_FILE):
            backup.unlink()
        self.break_h3("- just\n- a list\n")
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.files[H3_FILE].origin, "factory")
        self.assertEqual(loaded.calibration.backends[H3_BACKEND], factory_calibration().backends[H3_BACKEND])

    def test_a_newer_schema_is_read_only(self) -> None:
        self.edit_h3(lambda d: d.update(schema=2))
        loaded = load_knobs(self.files)
        self.assertIn(H3_FILE, loaded.suspended)
        self.assertTrue(any("newer version" in issue.message for issue in loaded.issues))

    def test_an_update_does_not_write_a_suspended_file(self) -> None:
        self.break_h3()
        report = self.engine.update(self.runs(12, first=10))
        self.assertIn(H3_FILE, report.suspended)
        self.assertEqual((self.files.directory / H3_FILE).read_text(encoding="utf-8"), "pipelines: [unclosed")
        deferred = check(self.files).deferred
        self.assertTrue(any(H3_FILE in line for line in deferred))
        self.assertTrue(any("not yet processed" in line for line in deferred))

    def test_nothing_a_file_holds_makes_loading_raise(self) -> None:
        for content in ("", "null", "42", "schema: 1\npipelines: 7\n", "schema: 1\npipelines:\n  r2av:\n    step:\n      seconds: [1]\n", "\x00\x01\x02", "schema: 1\nanchor: 3\nnotes: [a]\n", "!!python/object:os.system {}"):
            with self.subTest(content=content[:20]):
                self.write(H3_FILE, content)
                self.write(TUNING, content)
                load_knobs(self.files)

    def test_a_directory_where_a_file_belongs_does_not_raise(self) -> None:
        (self.files.directory / H3_FILE).unlink()
        (self.files.directory / H3_FILE).mkdir()
        loaded = load_knobs(self.files)
        self.assertEqual(loaded.calibration.backends[H3_BACKEND].anchor, "r2av")


class OperationTests(KnobFilesTestCase):
    def test_show_names_the_source_of_every_value(self) -> None:
        self.first_update(self.runs(2))
        self.edit_h3(lambda d: d["pipelines"]["r2av"]["step"].update(frame_exponent=1.7))
        rows = {(row.file, row.path): row for row in show(self.files)}
        self.assertEqual(rows[(H3_FILE, "pipelines/r2av/step/frame_exponent")].source, "pinned")
        self.assertEqual(rows[(H3_FILE, "pipelines/r2av/step/shift")].source, "provisional")
        self.assertEqual(rows[(H3_FILE, "pipelines/r2av/step/pixel_exponent")].source, "factory")
        self.assertEqual(rows[(DT_FILE, "pipelines/i2v/step/seconds")].source, "factory")
        self.assertEqual(rows[(TUNING, "tunables/pause_factor")].source, "factory")

    def test_show_names_the_baseline_of_a_validated_value(self) -> None:
        self.first_update()
        workload = h3_workload(pixels=832 * 448, frames=362, steps=30, references=3)
        runs = [h3_run(execution, 1, step=400.0 if execution <= 32 else 130.0, workload=workload) for execution in range(1, 41)]
        report = self.engine.update(runs, force_search=True)
        self.assertTrue(report.search and report.search.startswith("promote "))
        learned = [row for row in show(self.files) if row.file == H3_FILE and row.source == "learned"]
        self.assertTrue(learned)
        self.assertTrue(all(row.approved_against == "factory" for row in learned))
        search = check(self.files).search
        self.assertIsNotNone(search)
        self.assertEqual(json.loads(search or "{}")["verdict"], "promote")

    def test_check_lists_pins_and_is_ok_when_clean(self) -> None:
        self.first_update(self.runs(1))
        self.assertTrue(check(self.files).ok)
        self.edit_h3(lambda d: d["pipelines"]["r2av"]["step"].update(seconds=170.0))
        report = check(self.files)
        self.assertEqual(report.pins[H3_FILE], {H3_STEP: 170.0})
        self.assertTrue(report.ok)

    def test_reset_moves_the_file_aside_and_the_factory_applies_and_learning_starts_over(self) -> None:
        self.first_update(self.runs(4))
        written = self.files.read_written()
        written.search = {"last_attempt_total": 99, "last_result": {"verdict": "promote"}}
        self.files.atomic_write(self.files.written_path, json.dumps(written.as_dict()))
        moved = reset(self.files, H3_BACKEND)
        self.assertEqual(moved, [H3_FILE])
        self.assertFalse((self.files.directory / H3_FILE).exists())
        self.assertEqual(load_knobs(self.files).calibration.backends[H3_BACKEND], factory_calibration().backends[H3_BACKEND])
        self.assertTrue((self.files.directory / DT_FILE).exists())
        self.assertEqual(self.files.read_written().groups[f"{H3_BACKEND}/r2av"]["count"], 0)
        self.assertEqual(self.files.read_written().search, {"last_attempt_total": 0})
        report = self.engine.update(self.runs(4))
        self.assertEqual(report.fitted[f"{H3_BACKEND}/r2av"], 4)
        with self.assertRaises(FramecontrolError):
            reset(self.files, "nonsense")

    def test_rollback_restores_the_previous_calibration_without_reading_it_as_an_edit(self) -> None:
        self.first_update(self.runs(1))
        before = self.read(H3_FILE)
        self.engine.update(self.runs(2))
        self.assertNotEqual(self.read(H3_FILE), before)
        self.assertEqual(rollback(self.files), [H3_FILE])
        self.assertEqual(self.read(H3_FILE), before)
        loaded = load_knobs(self.files)
        self.assertEqual((loaded.files[H3_FILE].pins, loaded.issues), ({}, ()))

    def test_export_defaults_writes_the_three_examples(self) -> None:
        written = export_defaults(Path(self.temporary.name) / "out")
        self.assertEqual(sorted(path.name for path in written), ["draw-things-cli.example.yaml", "h3-metal-cli.example.yaml", "tuning.example.yaml"])
        self.assertEqual(yaml.safe_load(written[0].read_text(encoding="utf-8"))["tunables"]["pause_factor"], 3.0)
        self.assertEqual(Tunables().pause_factor, 3.0)
        self.assertTrue(dump_yaml({"a": 1}).startswith("# Framecontrol timing knobs"))
