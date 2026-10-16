"""Version-2 jobs in the queue: the snapshot freezes the compiled plan, resume reads only it, and limits count each run."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

from framecontrol.core.errors import InputError, LimitExceededError
from framecontrol.core.global_config import ApiLimits
from framecontrol.jobs.backends.h3 import H3JobData
from framecontrol.services.api_rules import check_job_limits
from framecontrol.services.queue_resume import ResumeRefusedError, _check_resume_input
from framecontrol.services.queue_submit import is_version_2, parse_snapshot, submit_job
from framecontrol.state.store import Store, StoreMode
from tests.fixtures import submit_legacy_job
from tests.jobs.test_compile_v2 import CompileV2TestCase, v2_job


class QueueV2TestCase(CompileV2TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)

    def submit(self, data: dict | None = None):
        return submit_job(self.write_job(data if data is not None else v2_job()), self.global_config, self.params, self.store)


class SubmitV2Tests(QueueV2TestCase):
    def test_a_mixed_job_is_stored_as_a_version_7_snapshot_with_its_plan(self) -> None:
        entry = self.submit()
        snapshot = json.loads(entry.snapshot or "")
        self.assertEqual((entry.backend, entry.total_runs, snapshot["version"], is_version_2(entry)), ("mixed", 4, 7, True))
        self.assertEqual(entry.config_file, "")
        self.assertEqual(list(json.loads(entry.config_text)), ["wan"])
        self.assertIn("model: base.ckpt", json.loads(entry.config_text)["wan"])
        self.assertEqual([item["settings_profile"] for item in snapshot["effective_plan"]], ["h3-wide", "h3-wide", "wan", "h3-wide"])
        assert entry.settings.backend_settings is not None
        self.assertEqual(entry.settings.backend_settings["format_version"], 2)

    def test_a_version_1_entry_is_not_a_version_2_entry(self) -> None:
        from tests.fixtures import job_data

        entry = submit_legacy_job(self.write_legacy_job(job_data(run_count=2, prompt_pairs=[{"name": "only", "positive": "text"}])), self.global_config, self.params, self.store)
        self.assertFalse(is_version_2(entry))
        self.assertEqual(json.loads(entry.snapshot or "")["version"], 3)

    def test_the_snapshot_freezes_the_job_the_profiles_and_the_references(self) -> None:
        entry = self.submit()
        before = parse_snapshot(entry, self.global_config, self.params)()
        # Everything the job read from outside the snapshot changes or goes away.
        self.write_base_config({"model": "other.ckpt", "width": 64, "height": 64, "numFrames": 5})
        (self.params / "base.yaml").unlink()
        (self.input_directory / "a.png").unlink()
        (self.input_directory / "b.png").unlink()
        after = parse_snapshot(entry, self.global_config, self.params)()
        self.assertEqual(before.effective_plan(), after.effective_plan())
        self.assertEqual(after.effective_runs[2].backend_job.model, "base.ckpt")
        assert isinstance(after.effective_runs[0].backend_job.backend_data, H3JobData)
        copied = after.effective_runs[0].backend_job.backend_data.reference_pairs[0].references[0].path
        self.assertTrue(copied.is_file())
        self.assertFalse(copied.is_relative_to(self.input_directory))

    def test_a_changed_plan_makes_the_entry_unreadable(self) -> None:
        entry = self.submit()
        snapshot = json.loads(entry.snapshot or "")
        snapshot["effective_plan"][1]["knobs"]["seconds"] = 9
        tampered = replace(entry, snapshot=json.dumps(snapshot))
        with self.assertRaises(InputError) as caught:
            parse_snapshot(tampered, self.global_config, self.params)()
        self.assertEqual(caught.exception.field, "runs")

    def test_a_resumed_chain_does_not_need_the_jobs_first_input(self) -> None:
        data = v2_job(input="first-frame.png")
        data["runs"] = data["runs"][1:]
        data.pop("reference_pairs")
        data["defaults"].pop(0)
        data["prompt_pairs"].pop("establish")
        entry = self.submit(data)
        (self.input_directory / "first-frame.png").unlink()
        with self.assertRaises(InputError):
            parse_snapshot(entry, self.global_config, self.params)()
        job = parse_snapshot(entry, self.global_config, self.params, first_run=2)()
        self.assertEqual(job.run_count, 3)

    def test_a_resumed_draw_things_chain_keeps_its_frozen_first_input_plan(self) -> None:
        # No width or height in the base configuration: the profile's input_resize decides the size, so it cannot be
        # recomputed from the configuration when the input image is gone.
        self.write_base_config({"model": "base.ckpt", "numFrames": 9})
        self.write_image("tall.png", (200, 400))
        data = v2_job(input="tall.png")
        data["settings_profiles"] = {"wan": {"backend": "draw-things-cli", "settings": {"config_file": "base.yaml", "input_resize": {"desired_width": 128}}}}
        data["prompt_pairs"] = {"paint": {"positive": "x"}}
        data["defaults"] = [{"backend": "draw-things-cli", "pipeline": "i2v", "prompt_pair": "paint", "settings_profile": "wan", "knobs": {"frames": 9, "timeout_seconds": 60}}]
        data["runs"] = [{"backend": "draw-things-cli", "pipeline": "i2v"}, {"backend": "draw-things-cli", "pipeline": "i2v"}]
        data.pop("reference_pairs")
        entry = self.submit(data)
        original = parse_snapshot(entry, self.global_config, self.params)()
        planned = original.effective_runs[0].backend_job.input_resize
        assert planned is not None
        self.assertEqual(planned.target_size, (128, 256))
        (self.input_directory / "tall.png").unlink()
        resumed = parse_snapshot(entry, self.global_config, self.params, first_run=2)()
        self.assertEqual(resumed.effective_plan(), original.effective_plan())
        self.assertEqual(resumed.effective_runs[0].backend_job.size, (128, 256))

    def test_a_job_with_no_reference_assets_cannot_use_an_r2av_pair(self) -> None:
        entry = self.submit()
        snapshot = json.loads(entry.snapshot or "")
        snapshot.pop("reference_assets")
        with self.assertRaises(InputError):
            parse_snapshot(replace(entry, snapshot=json.dumps(snapshot)), self.global_config, self.params)()


class ReferenceCopyTests(QueueV2TestCase):
    """Two `r2av` runs with a pair each, so a resume past the first can do without the first pair's copies."""

    def two_pair_job(self) -> dict:
        data = v2_job()
        data["reference_pairs"] = {"duo-a": {"references": [{"kind": "image", "path": "a.png"}]}, "duo-b": {"references": [{"kind": "image", "path": "b.png"}]}}
        data["defaults"] = [data["defaults"][0], data["defaults"][1]]
        data["defaults"][0]["knobs"].pop("reference_pair")
        data["runs"] = [{"backend": "h3-metal-cli", "pipeline": "r2av", "knobs": {"reference_pair": "duo-a"}}, {"backend": "h3-metal-cli", "pipeline": "i2av"}, {"backend": "h3-metal-cli", "pipeline": "r2av", "knobs": {"reference_pair": "duo-b"}}]
        del data["settings_profiles"]["wan"]
        data["prompt_pairs"].pop("paint")
        return data

    def test_the_copies_a_version_2_entry_needs_survive_retention(self) -> None:
        entry = self.submit(self.two_pair_job())
        root = self.store.path.parent / "queue-inputs"
        directories = [path for path in root.iterdir() if path.name.startswith("h3-references-")]
        self.assertEqual(len(directories), 1)
        stray = root / "h3-references-stray"
        stray.mkdir()
        for directory in (*directories, stray):
            os.utime(directory, (time.time() - 7200, time.time() - 7200))
        self.store.queue.cleanup_reference_snapshots()
        # Referenced by the entry's version-7 snapshot, the copies stay after the grace period; an unreferenced directory goes.
        self.assertTrue(directories[0].is_dir())
        self.assertFalse(stray.exists())
        job = parse_snapshot(entry, self.global_config, self.params)()
        self.assertEqual([run.knobs.get("reference_pair") for run in job.effective_runs], ["duo-a", None, "duo-b"])

    def test_a_resume_needs_only_the_pairs_its_unfinished_runs_select(self) -> None:
        entry = self.submit(self.two_pair_job())
        assets = json.loads(entry.snapshot or "")["reference_assets"]
        first = next(item for item in assets if item["pair"] == "duo-a")
        Path(first["snapshot_path"]).unlink()
        # Runs 1 and 2 are finished, so duo-a, which only run 1 selects, can be gone; the whole chain cannot.
        resumed = parse_snapshot(entry, self.global_config, self.params, first_run=3)()
        self.assertEqual(resumed.effective_plan()[2]["knobs"]["reference_pair"], "duo-b")
        with self.assertRaises(InputError):
            parse_snapshot(entry, self.global_config, self.params)()
        with self.assertRaises(InputError):
            parse_snapshot(entry, self.global_config, self.params, first_run=1)()


class ResumeV2Tests(QueueV2TestCase):
    def job(self):
        return parse_snapshot(self.submit(), self.global_config, self.params)()

    def make_png(self, name: str, size: tuple[int, int]) -> Path:
        return self.write_image(name, size)

    def test_a_handoff_into_the_same_h3_profile_must_be_the_profiles_size(self) -> None:
        job = self.job()
        _check_resume_input(job, self.make_png("ok.png", (832, 448)), 2)
        with self.assertRaises(ResumeRefusedError):
            _check_resume_input(job, self.make_png("wrong.png", (64, 64)), 2)
        with self.assertRaises(ResumeRefusedError):
            _check_resume_input(job, self.input_directory / "gone.png", 2)

    def test_a_handoff_into_another_profile_is_prepared_when_the_run_starts(self) -> None:
        job = self.job()
        # Run 3 is Draw Things after H3; run 4 is H3 after Draw Things. Either way another profile prepares the image.
        for run in (3, 4):
            _check_resume_input(job, self.make_png(f"any-{run}.png", (64, 64)), run)
            with self.assertRaises(ResumeRefusedError):
                _check_resume_input(job, self.input_directory / "missing.png", run)

    def test_a_resume_point_past_the_last_run_is_refused_not_a_crash(self) -> None:
        with self.assertRaises(ResumeRefusedError) as caught:
            _check_resume_input(self.job(), self.make_png("late.png", (832, 448)), 5)
        self.assertIn("job has 4 runs", str(caught.exception))
        self.assertIsNone(self.job().effective_run(0))
        self.assertIsNone(self.job().effective_run(5))

    def test_an_independent_run_needs_no_handoff(self) -> None:
        _check_resume_input(self.job(), self.input_directory / "never-read.png", 1)


class LimitsV2Tests(QueueV2TestCase):
    def test_limits_sum_each_runs_own_timeout_instead_of_multiplying_one(self) -> None:
        data = v2_job()
        data["defaults"][0]["knobs"]["timeout_seconds"] = 100
        data["defaults"][1]["knobs"]["timeout_seconds"] = 100
        data["defaults"][2]["knobs"]["timeout_seconds"] = 100
        job = parse_snapshot(self.submit(data), self.global_config, self.params)()
        finishing = sum(self.backends_finishing(job, number) for number in (1, 2, 3, 4))
        worst = 400.0 + finishing + 3 * job.cooldown.wait_after(100).seconds
        generous = ApiLimits(max_job_seconds=worst, max_job_output_bytes=10**15)
        check_job_limits(job, generous)
        with self.assertRaises(LimitExceededError) as caught:
            check_job_limits(job, ApiLimits(max_job_seconds=worst - 1, max_job_output_bytes=10**15))
        self.assertEqual(caught.exception.key, "max_job_seconds")

    @staticmethod
    def backends_finishing(job, number: int) -> float:
        from framecontrol.jobs.backends import DEFAULT_BACKENDS

        run = job.effective_runs[number - 1]
        return DEFAULT_BACKENDS.get(run.backend).finishing_seconds(run.backend_job)

    def test_a_resume_counts_only_the_runs_it_has_left(self) -> None:
        job = parse_snapshot(self.submit(), self.global_config, self.params)()
        check_job_limits(job, ApiLimits(max_job_runs=2, max_job_seconds=10**9, max_job_output_bytes=10**15), remaining_runs=2)
        with self.assertRaises(LimitExceededError):
            check_job_limits(job, ApiLimits(max_job_runs=3, max_job_seconds=10**9, max_job_output_bytes=10**15))

    def test_the_output_bound_follows_each_h3_runs_own_duration(self) -> None:
        data = v2_job()
        long = parse_snapshot(self.submit(data), self.global_config, self.params)()
        shortened = v2_job()
        shortened["runs"][3]["knobs"] = {"seconds": 1}
        short = parse_snapshot(self.submit(shortened), self.global_config, self.params)()

        def bytes_for(job) -> int:
            from framecontrol.jobs.backends import DEFAULT_BACKENDS

            return sum(DEFAULT_BACKENDS.get(run.backend).estimated_output_bytes(run.backend_job, 1) for run in job.effective_runs)

        self.assertGreater(bytes_for(long), bytes_for(short))
        limit = bytes_for(short)
        check_job_limits(short, ApiLimits(max_job_seconds=10**9, max_job_output_bytes=limit))
        with self.assertRaises(LimitExceededError):
            check_job_limits(long, ApiLimits(max_job_seconds=10**9, max_job_output_bytes=limit))
