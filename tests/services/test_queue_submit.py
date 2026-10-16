"""Tests for submitting a job to the queue: validating it and snapshotting exactly what was validated."""

from __future__ import annotations

import json
from dataclasses import replace

from framecontrol.core.cooldown import CooldownPolicy
from framecontrol.core.errors import InputError
from framecontrol.services.queue_submit import parse_snapshot
from framecontrol.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, submit_legacy_job


class SubmitJobTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)

    def test_a_valid_job_is_stored_as_a_queued_entry(self) -> None:
        path = self.write_legacy_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        self.assertEqual((entry.label, entry.state, entry.job_path), ("Q0001", "queued", str(path.resolve())))
        self.assertEqual(entry.settings.config_file, "base.yaml")
        self.assertIn("name: sunset-walk", entry.job_text)
        self.assertIn("model: base.ckpt", entry.config_text)
        self.assertEqual((entry.backend, json.loads(entry.snapshot or "")["version"]), ("draw-things-cli", 3))

    def test_h3av_reference_is_frozen_and_verified_from_the_queue_snapshot(self) -> None:
        from framecontrol.jobs.backends.h3 import H3JobData

        self.write_image("singer.png", (160, 160))
        path = self.write_legacy_job(job_data(backend="h3-metal-cli", mode="h3av", input=None, config_file=None, run_count=1, prompt_pairs=[{"name": "scene", "positive": "A singer"}], h3_runs=[{"run": 1, "pipeline": "r2av", "reference_pair": "voice"}], reference_pairs=[{"name": "voice", "references": [{"kind": "image", "path": "singer.png"}]}], backend_settings={"width": 768, "height": 1344, "frames": 96, "seconds": 4, "steps": 30, "seed": 42}, output={"extension": "mp4"}))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        self.assertEqual(snapshot["version"], 6)
        self.assertEqual(snapshot["settings"]["backend_settings"]["color_policy"], {"version": 1, "anchor": "none"})
        asset = snapshot["reference_assets"][0]
        self.assertEqual((asset["pair"], asset["ordinal"], asset["field"], asset["basename"], len(asset["sha256"])), ("voice", 0, "path", "singer.png", 64))
        source = self.input_directory / "singer.png"
        source.unlink()
        job = parse_snapshot(entry, self.global_config, self.params)()
        self.assertIsInstance(job.backend_data, H3JobData)
        assert isinstance(job.backend_data, H3JobData)
        copied = job.backend_data.reference_pairs[0].references[0].path
        self.assertTrue(copied.is_file())
        self.assertFalse(copied.is_relative_to(self.input_directory))
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

    def test_h3_color_policy_survives_snapshot_and_tampering_is_rejected(self) -> None:
        path = self.write_legacy_job(job_data(backend="h3-metal-cli", mode="i2av", config_file=None, prompt_pairs=[{"name": "scene", "positive": "wind", "default": True}], backend_settings={"width": 768, "height": 1344, "frames": 96, "seconds": 4, "steps": 30, "seed": 42}, output={"extension": "mp4"}, color={"anchor": "first", "strength": 0.4, "regions": False}))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        self.assertEqual(snapshot["version"], 6)
        self.assertEqual(parse_snapshot(entry, self.global_config, self.params)().color.strength, 0.4)
        changed_settings = json.loads(json.dumps(snapshot["settings"]))
        changed_settings["backend_settings"]["color_policy"]["strength"] = 0.9
        changed_snapshot = {**snapshot, "settings": changed_settings}
        changed = replace(entry, settings=replace(entry.settings, backend_settings=changed_settings["backend_settings"]), snapshot=json.dumps(changed_snapshot))
        with self.assertRaises(InputError) as caught:
            parse_snapshot(changed, self.global_config, self.params)()
        self.assertEqual(caught.exception.field, "settings")

    def test_h3_snapshot_from_before_nearest_frame_alignment_keeps_its_plan(self) -> None:
        from framecontrol.jobs.backends.h3 import H3JobData

        path = self.write_legacy_job(job_data(backend="h3-metal-cli", mode="i2av", config_file=None, prompt_pairs=[{"name": "scene", "positive": "wind", "default": True}], backend_settings={"width": 768, "height": 1344, "frames": 96, "seconds": 4, "steps": 30, "seed": 42}, output={"extension": "mp4"}))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        snapshot["version"] = 5
        nearest = replace(entry, snapshot=json.dumps(snapshot))
        self.assertEqual(parse_snapshot(nearest, self.global_config, self.params)().backend_settings["effective_frames"], 90)

        legacy_settings = json.loads(json.dumps(snapshot["settings"]["backend_settings"]))
        legacy_settings["effective_frames"] = 107
        legacy_settings["effective_seconds"] = 107 / 24
        snapshot["version"] = 5
        snapshot["settings"]["backend_settings"] = legacy_settings
        previous = replace(entry, settings=replace(entry.settings, backend_settings=legacy_settings), snapshot=json.dumps(snapshot))
        job = parse_snapshot(previous, self.global_config, self.params)()
        self.assertIsInstance(job.backend_data, H3JobData)
        assert isinstance(job.backend_data, H3JobData)
        self.assertEqual((job.backend_data.settings.effective_frames, job.backend_settings["effective_frames"]), (107, 107))

        snapshot["version"] = 6
        changed = replace(previous, snapshot=json.dumps(snapshot))
        with self.assertRaises(InputError) as caught:
            parse_snapshot(changed, self.global_config, self.params)()
        self.assertEqual(caught.exception.field, "settings")

        snapshot["version"] = 5
        legacy_settings["effective_frames"] = 124
        legacy_settings["effective_seconds"] = 124 / 24
        changed = replace(previous, settings=replace(entry.settings, backend_settings=legacy_settings), snapshot=json.dumps(snapshot))
        with self.assertRaises(InputError) as caught:
            parse_snapshot(changed, self.global_config, self.params)()
        self.assertEqual(caught.exception.field, "settings")

    def test_legacy_h3_snapshot_without_color_policy_resumes_as_none(self) -> None:
        path = self.write_legacy_job(job_data(backend="h3-metal-cli", mode="i2av", config_file=None, prompt_pairs=[{"name": "scene", "positive": "wind", "default": True}], backend_settings={"width": 768, "height": 1344, "frames": 96, "seconds": 4, "steps": 30, "seed": 42}, output={"extension": "mp4"}))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        snapshot["version"] = 3
        snapshot["settings"]["backend_settings"].pop("color_policy")
        previous = replace(entry, settings=replace(entry.settings, backend_settings=snapshot["settings"]["backend_settings"]), snapshot=json.dumps(snapshot))
        self.assertEqual(parse_snapshot(previous, self.global_config, self.params)().color.anchor, "none")

    def test_version_three_snapshot_refuses_a_conflicting_backend_or_legacy_copy(self) -> None:
        path = self.write_legacy_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        for changed, field in ((replace(entry, backend="other"), "backend"), (replace(entry, job_text="changed"), "job_text")):
            with self.subTest(field=field), self.assertRaises(InputError) as caught:
                parse_snapshot(changed, self.global_config, self.params)()
            self.assertEqual(caught.exception.field, field)

    def test_version_three_snapshot_refuses_settings_that_do_not_match_its_job(self) -> None:
        path = self.write_legacy_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        summary = {**(entry.settings.backend_settings or {}), "model": "other.ckpt"}
        snapshot["settings"]["backend_settings"] = summary
        changed = replace(entry, settings=replace(entry.settings, backend_settings=summary), snapshot=json.dumps(snapshot))
        with self.assertRaises(InputError) as caught:
            parse_snapshot(changed, self.global_config, self.params)()
        self.assertEqual(caught.exception.field, "settings")

    def test_version_two_snapshot_keeps_its_older_safe_settings_shape(self) -> None:
        path = self.write_legacy_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        snapshot["version"] = 2
        snapshot["settings"]["backend_settings"] = {}
        previous = replace(entry, snapshot=json.dumps(snapshot), settings=replace(entry.settings, backend_settings={}))
        self.assertEqual(parse_snapshot(previous, self.global_config, self.params)().backend, "draw-things-cli")

    def test_an_invalid_job_stores_nothing(self) -> None:
        path = self.write_legacy_job(job_data(run_count=0, prompt_pairs=[{"name": "only", "positive": "text"}]))
        with self.assertRaises(InputError):
            submit_legacy_job(path, self.global_config, self.params, self.store)
        self.assertEqual(self.store.queue.list(), [])

    def test_a_missing_input_file_is_refused_before_anything_is_stored(self) -> None:
        path = self.write_legacy_job(job_data(input="missing.png", prompt_pairs=[{"name": "only", "positive": "text"}]))
        with self.assertRaisesRegex(InputError, "file does not exist"):
            submit_legacy_job(path, self.global_config, self.params, self.store)
        self.assertEqual(self.store.queue.list(), [])

    def test_the_snapshot_survives_editing_the_job_file_and_the_base_config(self) -> None:
        path = self.write_legacy_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        path.write_text("version: 1\nname: changed\nmode: t2v\nrun_count: 1\nprompt_pairs: []\nconfig_file: base.yaml\n", encoding="utf-8")
        self.write_base_config({"model": "changed.ckpt", "seed": 1})
        job = parse_snapshot(entry, self.global_config, self.params)()
        self.assertEqual((job.name, job.model), ("sunset-walk", "base.ckpt"))

    def test_the_snapshot_keeps_the_global_configs_input_and_output_directories(self) -> None:
        path = self.write_legacy_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        moved = replace(self.global_config, input_directory=self.root / "elsewhere-in", output_directory=self.root / "elsewhere-out")
        job = parse_snapshot(entry, moved, self.params)()
        self.assertEqual(job.output_directory, self.output_directory / "sunset-walk")

    def test_cooldown_off_snapshots_as_no_default(self) -> None:
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="manual", seconds=30))
        path = self.write_legacy_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_legacy_job(path, self.global_config, self.params, self.store)
        self.assertEqual(entry.cooldown_default, {"mode": "manual", "seconds": 30.0})
