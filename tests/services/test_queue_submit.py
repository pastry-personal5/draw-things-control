"""Tests for submitting a job to the queue: validating it and snapshotting exactly what was validated."""

from __future__ import annotations

import json
from dataclasses import replace

from framechain.core.cooldown import CooldownPolicy
from framechain.core.errors import InputError
from framechain.services.queue_submit import parse_snapshot, submit_job
from framechain.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data


class SubmitJobTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)

    def test_a_valid_job_is_stored_as_a_queued_entry(self) -> None:
        path = self.write_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        self.assertEqual((entry.label, entry.state, entry.job_path), ("Q0001", "queued", str(path.resolve())))
        self.assertEqual(entry.settings.config_file, "base.yaml")
        self.assertIn("name: sunset-walk", entry.job_text)
        self.assertIn("model: base.ckpt", entry.config_text)
        self.assertEqual((entry.backend, json.loads(entry.snapshot or "")["version"]), ("draw-things-cli", 3))

    def test_version_three_snapshot_refuses_a_conflicting_backend_or_legacy_copy(self) -> None:
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        for changed, field in ((replace(entry, backend="other"), "backend"), (replace(entry, job_text="changed"), "job_text")):
            with self.subTest(field=field), self.assertRaises(InputError) as caught:
                parse_snapshot(changed, self.global_config, self.params)()
            self.assertEqual(caught.exception.field, field)

    def test_version_three_snapshot_refuses_settings_that_do_not_match_its_job(self) -> None:
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        summary = {**(entry.settings.backend_settings or {}), "model": "other.ckpt"}
        snapshot["settings"]["backend_settings"] = summary
        changed = replace(entry, settings=replace(entry.settings, backend_settings=summary), snapshot=json.dumps(snapshot))
        with self.assertRaises(InputError) as caught:
            parse_snapshot(changed, self.global_config, self.params)()
        self.assertEqual(caught.exception.field, "settings")

    def test_version_two_snapshot_keeps_its_older_safe_settings_shape(self) -> None:
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        snapshot = json.loads(entry.snapshot or "")
        snapshot["version"] = 2
        snapshot["settings"]["backend_settings"] = {}
        previous = replace(entry, snapshot=json.dumps(snapshot), settings=replace(entry.settings, backend_settings={}))
        self.assertEqual(parse_snapshot(previous, self.global_config, self.params)().backend, "draw-things-cli")

    def test_an_invalid_job_stores_nothing(self) -> None:
        path = self.write_job(job_data(run_count=0, prompt_pairs=[{"name": "only", "positive": "text"}]))
        with self.assertRaises(InputError):
            submit_job(path, self.global_config, self.params, self.store)
        self.assertEqual(self.store.queue.list(), [])

    def test_a_missing_input_file_is_refused_before_anything_is_stored(self) -> None:
        path = self.write_job(job_data(input="missing.png", prompt_pairs=[{"name": "only", "positive": "text"}]))
        with self.assertRaisesRegex(InputError, "file does not exist"):
            submit_job(path, self.global_config, self.params, self.store)
        self.assertEqual(self.store.queue.list(), [])

    def test_the_snapshot_survives_editing_the_job_file_and_the_base_config(self) -> None:
        path = self.write_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        path.write_text("version: 1\nname: changed\nmode: t2v\nrun_count: 1\nprompt_pairs: []\nconfig_file: base.yaml\n", encoding="utf-8")
        self.write_base_config({"model": "changed.ckpt", "seed": 1})
        job = parse_snapshot(entry, self.global_config, self.params)()
        self.assertEqual((job.name, job.model), ("sunset-walk", "base.ckpt"))

    def test_the_snapshot_keeps_the_global_configs_input_and_output_directories(self) -> None:
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        moved = replace(self.global_config, input_directory=self.root / "elsewhere-in", output_directory=self.root / "elsewhere-out")
        job = parse_snapshot(entry, moved, self.params)()
        self.assertEqual(job.output_directory, self.output_directory / "sunset-walk")

    def test_cooldown_off_snapshots_as_no_default(self) -> None:
        self.global_config = replace(self.global_config, cooldown=CooldownPolicy(mode="manual", seconds=30))
        path = self.write_job(job_data(run_count=1, prompt_pairs=[{"name": "only", "positive": "text"}]))
        entry = submit_job(path, self.global_config, self.params, self.store)
        self.assertEqual(entry.cooldown_default, {"mode": "manual", "seconds": 30.0})
