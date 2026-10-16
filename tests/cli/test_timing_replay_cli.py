"""``fc timing replay``: the store read with no server, a stream replayed through an estimator, and the metrics printed."""

from __future__ import annotations

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


class TimingReplayCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()

    def keep(self, executions: int, *, backend: str = "draw-things-cli") -> None:
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(store.close)
        for index in range(executions):
            stamp = f"2026-10-{10 + index:02d}T10:00:00+00:00"
            execution = store.executions.start(NewExecution(job_name=f"walk-{index}", job_file="walk.yaml", mode="i2v", started_at=stamp, settings=ExecutionSettings(output_directory="/out", cooldown={"mode": "manual", "seconds": 30.0})))
            for number in (1, 2):
                store.executions.start_run(execution, number, NewRun(pair="p", positive="t", started_at=stamp))
                store.executions.finish_run(execution, number, status="succeeded", exit_code=0, seconds=40.0, output="a.mov", last_frame=None)
                store.timing.replace_run(execution, number, backend=backend, pipeline="i2v", workload=WORKLOAD, environment=ENVIRONMENT, fidelity="live", complete=True, spans=spans(10.0 + index))
            store.executions.set_run_cooldown(execution, 1, 30.0)
            store.executions.finish(execution, status="succeeded", exit_code=0, signal=None, finished_at=stamp)

    def invoke(self, *arguments: str):
        return self.runner.invoke(app, ["timing", "replay", *arguments], obj=CliServices(self.paths, Toolkit()))

    def test_it_replays_the_retained_runs_and_prints_the_metrics_of_the_forecast(self) -> None:
        self.keep(6)
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        out = result.stdout
        self.assertIn("forecast", out.splitlines()[0])
        self.assertIn("6 executions, 12 runs", out.splitlines()[0])
        for text in ("run draw-things-cli i2v", "job draw-things-cli i2v", "median", "p90", "bias", "coverage"):
            self.assertIn(text, out)
        for checkpoint in ("5%", "10%", "25%", "50%", "75%", "90%"):
            self.assertIn(checkpoint, out)
        self.assertIn("time to the first number:", out)
        self.assertIn("share moved back: 0 times in runs, 0 in jobs", out)

    def test_a_cell_with_too_few_runs_is_listed_and_not_judged(self) -> None:
        self.keep(3)
        out = self.invoke().stdout
        self.assertIn("not judged", out)
        self.assertIn("fewer than 8 runs", out)

    def test_each_estimator_by_its_name_and_an_unknown_one_refused(self) -> None:
        self.keep(4)
        for name in ("constant-pace", "last-signature", "forecast"):
            result = self.invoke("--estimator", name)
            self.assertEqual(result.exit_code, 0, f"{name}: {result.output}")
            self.assertIn(name, result.stdout.splitlines()[0])
        refused = self.invoke("--estimator", "frozen")
        self.assertEqual(refused.exit_code, 2)

    def test_a_backend_with_no_runs_says_so(self) -> None:
        self.keep(4)
        result = self.invoke("--backend", "h3-metal-cli")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No complete h3-metal-cli runs to replay", result.stdout)

    def test_with_no_database_or_no_timing_it_says_so_and_creates_nothing(self) -> None:
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No timing history to replay", result.stdout)
        self.assertFalse(self.paths.database.exists())
