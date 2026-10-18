"""``fc timing knobs``: the knob files shown, checked, updated from the retained runs, rolled back, reset, and exported."""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from framecontrol.cli.app import app
from framecontrol.cli.context import CliServices
from framecontrol.jobs.timing.model import StageSpan
from framecontrol.services.toolkit import Toolkit
from framecontrol.state.executions import ExecutionSettings, NewExecution, NewRun
from framecontrol.state.store import Store, StoreMode
from tests.fixtures import JobTestCase

WORKLOAD = {"width": 832, "height": 448, "pixels": 372736, "frames": 81, "steps": 4, "mode": "i2v", "guidance_scale": 7.5}
ENVIRONMENT = {"model": "base.ckpt"}


def spans(step: float) -> list[StageSpan]:
    return [
        StageSpan("prepare", None, "framecontrol", 0.0, 1.0),
        StageSpan("sampling", "Sampling", "child", 1.0, 1.0 + 4 * step, 4, 4, "steps", tuple((1.0 + index * step, index) for index in range(5))),
        StageSpan("verify", None, "framecontrol", 1.0 + 4 * step, 3.0 + 4 * step),
    ]


class TimingKnobsCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()

    def keep(self, executions: int) -> None:
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(store.close)
        for index in range(executions):
            stamp = f"2026-10-{10 + index:02d}T10:00:00+00:00"
            execution = store.executions.start(NewExecution(job_name=f"walk-{index}", job_file="walk.yaml", mode="i2v", started_at=stamp, settings=ExecutionSettings(output_directory="/out")))
            store.executions.start_run(execution, 1, NewRun(pair="p", positive="t", started_at=stamp))
            store.executions.finish_run(execution, 1, status="succeeded", exit_code=0, seconds=40.0, output="a.mov", last_frame=None)
            store.timing.replace_run(execution, 1, backend="draw-things-cli", pipeline="i2v", workload=WORKLOAD, environment=ENVIRONMENT, fidelity="live", complete=True, spans=spans(10.0 + index))
            store.executions.finish(execution, status="succeeded", exit_code=0, signal=None, finished_at=stamp)

    def invoke(self, *arguments: str):
        return self.runner.invoke(app, ["timing", "knobs", *arguments], obj=CliServices(self.paths, Toolkit()))

    def test_with_no_files_show_lists_the_factory_and_check_says_so(self) -> None:
        shown = self.invoke("show")
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertIn("tuning.yaml  tunables/pause_factor = 3.0  [factory]", shown.stdout)
        self.assertIn("h3-metal-cli.yaml  pipelines/r2av/step/seconds = 138.6683  [factory]", shown.stdout)
        checked = self.invoke("check")
        self.assertEqual(checked.exit_code, 0, checked.output)
        self.assertIn("none (the factory knobs apply)", checked.stdout)

    def test_update_reads_the_retained_runs_with_no_server_and_creates_the_files(self) -> None:
        self.keep(3)
        result = self.invoke("update")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Added 3 runs to the ring", result.stdout)
        self.assertIn("draw-things-cli/i2v: provisional calibration at 2 runs", result.stdout)
        self.assertIn("wrote draw-things-cli.yaml", result.stdout)
        self.assertTrue((self.paths.timing_data / "tuning.yaml").is_file())
        shown = self.invoke("show", "draw-things-cli.yaml", "--source", "provisional")
        self.assertIn("pipelines/i2v/step/shift", shown.stdout)
        again = self.invoke("update")
        self.assertIn("Added 0 runs to the ring", again.stdout)

    def test_update_with_no_database_is_not_an_error(self) -> None:
        result = self.invoke("update")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Added 0 runs", result.stdout)

    def test_a_hand_edit_is_reported_as_pinned(self) -> None:
        self.invoke("update")
        path = self.paths.timing_data / "h3-metal-cli.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["pipelines"]["r2av"]["step"]["seconds"] = 150.0
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        checked = self.invoke("check")
        self.assertIn("pinned: h3-metal-cli.yaml pipelines/r2av/step/seconds = 150.0", checked.stdout)
        shown = self.invoke("show", "--source", "pinned")
        self.assertIn("pipelines/r2av/step/seconds = 150.0  [pinned]", shown.stdout)

    def test_a_damaged_file_makes_check_exit_1_and_says_what_to_do(self) -> None:
        self.invoke("update")
        (self.paths.timing_data / "h3-metal-cli.yaml").write_text("pipelines: [", encoding="utf-8")
        checked = self.invoke("check")
        self.assertEqual(checked.exit_code, 1)
        self.assertIn("error: h3-metal-cli.yaml", checked.stdout)
        self.assertIn("deferred: h3-metal-cli.yaml: automatic updates are suspended", checked.stdout)

    def test_rollback_and_reset(self) -> None:
        self.keep(2)
        self.invoke("update")
        self.assertEqual((self.paths.timing_data / "draw-things-cli.yaml").exists(), True)
        rolled = self.invoke("rollback")
        self.assertEqual(rolled.exit_code, 0, rolled.output)
        self.assertIn("Restored", rolled.stdout)
        self.assertEqual(self.invoke("rollback", "9").exit_code, 1)
        self.invoke("update")
        reset = self.invoke("reset", "draw-things-cli")
        self.assertIn("Moved draw-things-cli.yaml to the backups", reset.stdout)
        self.assertFalse((self.paths.timing_data / "draw-things-cli.yaml").exists())
        self.assertEqual(self.invoke("reset", "nonsense").exit_code, 1)
        self.assertIn("Nothing to reset", self.invoke("reset", "draw-things-cli").stdout)

    def test_export_defaults_writes_the_examples_where_asked(self) -> None:
        target = self.paths.root / "elsewhere"
        result = self.invoke("export-defaults", "--directory", str(target))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["draw-things-cli.example.yaml", "h3-metal-cli.example.yaml", "tuning.example.yaml"])
