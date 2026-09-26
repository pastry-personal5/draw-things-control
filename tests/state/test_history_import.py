"""Tests for importing phase 1 manifests."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from draw_things_control.state.history_import import import_history
from draw_things_control.state.store import Store

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def manifest(days_ago: float = 1, status: str = "succeeded", runs: int = 2, **changes: object) -> dict:
    data = {
        "job_file": "/data/walk.yaml",
        "name": "walk",
        "mode": "i2v",
        "config_file": "base.json",
        "config_override": {"steps": 20},
        "seed": 42,
        "seed_source": "config_file",
        "cooldown_seconds": 0.0,
        "cooldown_source": "default",
        "started_at": stamp(days_ago),
        "log_file": "walk-job.log",
        "input_resize": None,
        "finished_at": stamp(days_ago) if status != "running" else None,
        "status": status,
        "runs": [{"pair": "walk", "positive": "walk", "negative": None, "input": "in.png", "output": f"out-{number}.mov", "last_frame": None, "command": ["draw-things-cli"], "started_at": stamp(days_ago), "seconds": 3.0, "exit_code": 0, "status": "succeeded" if status != "running" or number < runs else "running", "cooldown_after_seconds": None, "resized_input": None, "batch": 99} for number in range(1, runs + 1)],
    }
    data.update(changes)
    return data


class ImportHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.outputs = self.root / "output"
        (self.outputs / "walk").mkdir(parents=True)
        self.store = Store(self.root / "dtc.db", retention_days=14, clock=lambda: NOW)
        self.addCleanup(self.store.close)

    def write(self, name: str, data: object, folder: str = "walk") -> Path:
        path = self.outputs / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_import(self):
        return import_history(self.store, self.outputs, clock=NOW)

    def test_the_cooldown_mapping_is_copied_and_old_manifests_still_read(self) -> None:
        mapping = {"mode": "auto", "ratio": 0.5, "minimum_seconds": 300.0, "maximum_seconds": 1800.0}
        self.write("new-job.json", manifest(cooldown_seconds=None, cooldown_source="global_config", cooldown=mapping))
        self.write("old-job.json", manifest(days_ago=2, cooldown_seconds=900.0, cooldown_source="global_config"), folder="old")
        self.assertEqual(self.run_import().imported, 2)
        new, old = (self.store.get_execution(row["id"]) for row in self.store.list_executions())
        self.assertEqual((new["cooldown_seconds"], new["settings"]["cooldown"]), (None, mapping))
        self.assertEqual((old["cooldown_seconds"], old["settings"]["cooldown_seconds"]), (900.0, 900.0))
        self.assertNotIn("cooldown", old["settings"])

    def test_manifests_in_job_subdirectories_are_imported_with_their_runs(self) -> None:
        path = self.write("walk-job.json", manifest())
        report = self.run_import()
        self.assertEqual((report.imported, report.skipped, report.expired, report.unreadable), (1, 0, 0, 0))
        [row] = self.store.list_executions()
        execution = self.store.get_execution(row["id"])
        self.assertEqual((execution["job_name"], execution["status"], execution["seed"], execution["manifest_path"], execution["total_runs"]), ("walk", "succeeded", 42, str(path.resolve()), 2))
        self.assertEqual((execution["job_yaml"], execution["model"], execution["exit_code"], execution["signal"]), (None, None, None, None))
        self.assertEqual(execution["log_path"], str(path.parent / "walk-job.log"))
        self.assertEqual([(run["number"], run["output"]) for run in execution["runs"]], [(1, "out-1.mov"), (2, "out-2.mov")])

    def test_importing_twice_imports_once_and_never_modifies_a_manifest(self) -> None:
        path = self.write("walk-job.json", manifest())
        before = path.read_bytes()
        self.run_import()
        report = self.run_import()
        self.assertEqual((report.imported, report.skipped), (0, 1))
        self.assertEqual(len(self.store.list_executions()), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_a_manifest_already_recorded_live_is_not_imported_again(self) -> None:
        path = self.write("walk-job.json", manifest())
        self.store.start_execution(job_name="walk", job_file="/data/walk.yaml", mode="i2v", started_at=stamp(1), manifest_path=str(path.resolve()))
        self.assertEqual(self.run_import().skipped, 1)
        self.assertEqual(len(self.store.list_executions()), 1)

    def test_files_that_are_not_manifests_are_counted_unreadable_and_left_alone(self) -> None:
        self.write("notes.json", {"hello": "world"})
        self.write("list.json", [1, 2])
        broken = self.outputs / "walk" / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.write("bad-time.json", manifest(started_at="yesterday"))
        self.write(".hidden.json", manifest())
        self.write("walk-job.json", manifest(), folder=".trash")
        (self.outputs / "walk" / "image.png").write_bytes(b"png")
        report = self.run_import()
        self.assertEqual((report.imported, report.unreadable), (0, 4))
        self.assertEqual(self.store.list_executions(), [])
        self.assertEqual(broken.read_text(encoding="utf-8"), "{not json")

    def test_manifests_older_than_the_retention_period_are_expired_not_imported(self) -> None:
        self.write("old.json", manifest(days_ago=15))
        self.write("recent.json", manifest(days_ago=13))
        report = self.run_import()
        self.assertEqual((report.imported, report.expired), (1, 1))
        self.assertEqual(self.store.list_executions()[0]["started_at"], stamp(13))

    def test_zero_retention_imports_everything(self) -> None:
        store = Store(self.root / "forever.db", retention_days=0, clock=lambda: NOW)
        self.addCleanup(store.close)
        self.write("old.json", manifest(days_ago=400))
        self.assertEqual(import_history(store, self.outputs, clock=NOW).imported, 1)

    def test_a_manifest_left_running_by_a_crash_imports_as_interrupted(self) -> None:
        self.write("crashed.json", manifest(status="running"))
        self.run_import()
        execution = self.store.get_execution(self.store.list_executions()[0]["id"])
        self.assertEqual((execution["status"], [run["status"] for run in execution["runs"]]), ("interrupted", ["succeeded", "interrupted"]))
        self.assertIsNotNone(execution["recovered_at"])
        self.assertIsNotNone(execution["finished_at"])

    def test_a_crashed_manifest_older_than_retention_is_expired_not_restamped_and_reimported(self) -> None:
        self.write("crashed.json", manifest(status="running", days_ago=15))
        report = self.run_import()
        self.assertEqual((report.imported, report.expired), (0, 1))

    def test_the_run_number_is_the_position_not_the_batch_field(self) -> None:
        self.write("walk-job.json", manifest(runs=3))
        self.run_import()
        execution = self.store.get_execution(self.store.list_executions()[0]["id"])
        self.assertEqual([run["number"] for run in execution["runs"]], [1, 2, 3])
