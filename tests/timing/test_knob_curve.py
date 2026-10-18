"""The knob learning curve on a small deterministic fixture: the update schedule is replayed, the groups are filled, and the
three variants are told apart. No accuracy threshold gates this; the report (``python -m tests.timing.knob_curve``) is read."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from framecontrol.core.backend_ids import H3_BACKEND
from tests.timing.knob_curve import VARIANTS, curve_lines, learning_curve, run_bucket, seen_bucket, without_offsets
from tests.timing.synthetic import synthetic_history

SYNTHETIC = synthetic_history(20261016, executions=40, away=0.3)
H3_EXECUTIONS = [execution for execution in SYNTHETIC.executions if execution.runs[0].backend == H3_BACKEND]


class SecondModeTests(unittest.TestCase):
    def test_away_zero_is_the_first_mode_exactly(self) -> None:
        plain, zero = synthetic_history(7, executions=12), synthetic_history(7, executions=12, away=0.0)
        self.assertEqual([[(run.workload, [(stage.stage, stage.start, stage.end) for stage in run.stages]) for run in execution.runs] for execution in plain.executions], [[(run.workload, [(stage.stage, stage.start, stage.end) for stage in run.stages]) for run in execution.runs] for execution in zero.executions])

    def test_the_second_mode_moves_each_backend_the_other_way_from_the_factory(self) -> None:
        plain, away = synthetic_history(7, executions=40), synthetic_history(7, executions=40, away=0.3)

        def step(history, backend: str) -> float:
            runs = [run for run in history.runs if run.backend == backend and run.workload.get("width") == 832 and run.workload.get("height") == 448]
            sampling = [next(stage for stage in run.stages if stage.unit == "steps") for run in runs]
            return sum(stage.duration / stage.total for stage in sampling) / len(sampling)

        self.assertGreater(step(away, "draw-things-cli"), step(plain, "draw-things-cli"))
        self.assertLess(step(away, H3_BACKEND), step(plain, H3_BACKEND))


class LearningCurveTests(unittest.TestCase):
    curves: list = []

    @classmethod
    def setUpClass(cls) -> None:
        for variant in VARIANTS:
            with tempfile.TemporaryDirectory() as directory:
                cls.curves.append(learning_curve(H3_EXECUTIONS, variant, Path(directory) / "timing"))

    def test_the_curve_fills_its_buckets_as_the_pipeline_completes_runs(self) -> None:
        factory, no_offsets, full = self.curves
        self.assertEqual(set(factory.by_runs), {"0"})
        self.assertGreater(len(full.by_runs), 3)
        self.assertEqual({"0", "1", "2", "4", "8", "16", "32"} | {"32+"} >= set(full.by_runs), True)
        self.assertEqual(sum(len(items) for items in full.by_runs.values()), sum(len(items) for items in factory.by_runs.values()))
        self.assertEqual(set(no_offsets.by_runs), set(full.by_runs))

    def test_the_variants_forecast_differently_once_something_is_learned(self) -> None:
        factory, _no_offsets, full = self.curves
        first = lambda curve: sorted((item.execution, item.run or 0, item.checkpoint, item.estimate or 0.0) for items in curve.by_runs.values() for item in items)[:40]  # noqa: E731
        self.assertEqual(first(factory)[:3], first(full)[:3])
        late = lambda curve: sorted((item.execution, item.run or 0, item.checkpoint, item.estimate or 0.0) for items in curve.by_runs.values() for item in items)[-40:]  # noqa: E731
        self.assertNotEqual(late(factory), late(full))

    def test_the_table_has_the_buckets_the_milestone_names(self) -> None:
        text = "\n".join(curve_lines(self.curves))
        for label in ("completed runs of the pipeline", "times the exact size had run", "factory", "no-offsets", "full"):
            self.assertIn(label, text)
        self.assertIn("cov ", text)

    def test_the_bucket_names(self) -> None:
        self.assertEqual([run_bucket(count) for count in (0, 1, 2, 3, 4, 7, 8, 15, 16, 31, 32, 33)], ["0", "1", "2", "2", "4", "4", "8", "8", "16", "16", "32", "32+"])
        self.assertEqual([seen_bucket(count) for count in (0, 1, 4, 5, 50)], ["0", "1", "1", "5+", "5+"])

    def test_without_offsets_clears_only_those_terms(self) -> None:
        from dataclasses import replace

        from framecontrol.jobs.timing.factory import factory_calibration

        base = factory_calibration()
        backend = base.backends[H3_BACKEND]
        pipeline = backend.pipelines["r2av"]
        marked = replace(base, backends={**base.backends, H3_BACKEND: replace(backend, pipelines={"r2av": replace(pipeline, step=replace(pipeline.step, shift=0.2, sizes={"1x1": 0.1}, knobs={"a": 0.1}))})})
        stripped = without_offsets(marked).backends[H3_BACKEND].pipelines["r2av"].step
        self.assertEqual((stripped.shift, dict(stripped.sizes), dict(stripped.knobs)), (0.2, {}, {}))
