"""Report the M15 guarded learning curve and its false-accept controls.

Run from the project root::

    uv run python -m tests.timing.knob_gate [--executions 120]

This is evidence, not an accuracy gate.  It compares the live guarded updater
with the calibration frozen at each group's 32-run checkpoint, counts gate and
search outcomes, repeats the stream with no true factory shift, and exercises
the independent live-regression restore path.
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

from framecontrol.core.paths import ProjectPaths
from framecontrol.jobs.timing.factory import factory_calibration, factory_tunables
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.knobs import Calibration, PipelineCalibration
from framecontrol.services.knob_updater import KnobUpdateEngine
from framecontrol.services.timing_knob_files import KnobFiles
from framecontrol.services.timing_knobs import load_knobs
from framecontrol.services.timing_replay import CHECKPOINTS_RUN, ForecastEstimator, ReplayReport, score_execution
from tests.timing.builders import h3_run, h3_workload
from tests.timing.synthetic import synthetic_history


@dataclass(frozen=True)
class Evaluation:
    guarded_error: float | None
    frozen_error: float | None
    guarded_coverage: float | None
    frozen_coverage: float | None
    observations: int
    gates: Counter[str]
    searches: Counter[str]
    search_moves: Counter[str]
    search_promotions: tuple[dict[str, object], ...]


def _replace_groups(base: Calibration, values: dict[str, PipelineCalibration]) -> Calibration:
    backends = dict(base.backends)
    for group, calibration in values.items():
        backend, _, pipeline = group.partition("/")
        current = backends[backend]
        backends[backend] = replace(current, pipelines={**current.pipelines, pipeline: calibration})
    return replace(base, backends=backends)


def _observations(execution: HistoryExecution, model: FittedModel) -> ReplayReport:
    report = ReplayReport()
    score_execution(execution, [], model, ForecastEstimator, report, run_checkpoints=(0.0, *CHECKPOINTS_RUN))
    return report


def evaluate(executions: tuple[HistoryExecution, ...]) -> Evaluation:
    with tempfile.TemporaryDirectory() as directory:
        files = KnobFiles(ProjectPaths(Path(directory)).timing_data)
        engine = KnobUpdateEngine(files, clock=lambda: "2026-10-20T00:00:00+00:00")
        history: list[HistoryRun] = []
        frozen: dict[str, PipelineCalibration] = {}
        guarded_errors: list[float] = []
        frozen_errors: list[float] = []
        guarded_covered: list[float] = []
        frozen_covered: list[float] = []
        gates: Counter[str] = Counter()
        searches: Counter[str] = Counter()
        search_moves: Counter[str] = Counter()
        search_promotions: list[dict[str, object]] = []
        for execution in executions:
            loaded = load_knobs(files)
            counts = files.read_written().groups
            for group, record in counts.items():
                if int(record.get("checkpoint", 0)) >= 32 and group not in frozen:
                    backend, _, pipeline = group.partition("/")
                    frozen[group] = loaded.calibration.backends[backend].pipeline(pipeline)
            eligible = {run.run: f"{run.backend}/{run.pipeline}" for run in execution.runs if int(counts.get(f"{run.backend}/{run.pipeline}", {}).get("count", 0)) >= 32 and f"{run.backend}/{run.pipeline}" in frozen}
            if eligible:
                guarded = _observations(execution, FittedModel([], tunables=loaded.tunables, calibration=loaded.calibration))
                frozen_report = _observations(execution, FittedModel([], tunables=factory_tunables(), calibration=_replace_groups(factory_calibration(), frozen)))
                for destination, report in (("guarded", guarded), ("frozen", frozen_report)):
                    for item in report.observations:
                        if item.level != "run" or item.run not in eligible or item.estimate is None or item.truth <= 0:
                            continue
                        error = abs(item.estimate - item.truth) / item.truth
                        covered = 1.0 if item.low is not None and item.high is not None and item.low <= item.truth <= item.high else 0.0
                        (guarded_errors if destination == "guarded" else frozen_errors).append(error)
                        (guarded_covered if destination == "guarded" else frozen_covered).append(covered)
            history.extend(execution.runs)
            report = engine.update(history)
            gates.update(report.gates.values())
            if report.search is not None:
                searches.update([report.search.split()[0]])
                if report.search.startswith("promote "):
                    search_moves.update([report.search.removeprefix("promote ")])
                    result = files.read_written().search.get("last_result")
                    if isinstance(result, dict):
                        search_promotions.append(dict(result))
        return Evaluation(
            statistics.median(guarded_errors) if guarded_errors else None,
            statistics.median(frozen_errors) if frozen_errors else None,
            statistics.fmean(guarded_covered) if guarded_covered else None,
            statistics.fmean(frozen_covered) if frozen_covered else None,
            len(guarded_errors),
            gates,
            searches,
            search_moves,
            tuple(search_promotions),
        )


def restore_count() -> int:
    """A deliberately stale provisional model with no valid candidate restores factory."""
    workload = h3_workload(pixels=832 * 448, frames=362, steps=30, references=3)
    runs = [h3_run(execution, 1, step=400.0, tail=10.0, workload=workload) for execution in range(1, 33)]
    runs += [h3_run(execution, 1, step=138.6683, tail=190.0, workload=workload) for execution in range(33, 41)]
    with tempfile.TemporaryDirectory() as directory:
        files = KnobFiles(ProjectPaths(Path(directory)).timing_data)
        engine = KnobUpdateEngine(files, clock=lambda: "2026-10-20T00:00:00+00:00")
        history: list[HistoryRun] = []
        restored = 0
        with mock.patch("framecontrol.services.knob_updater.calibrate_validated", return_value=None):
            for run in runs:
                history.append(run)
                restored += sum(value == "restored" for value in engine.update(history).gates.values())
        return restored


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executions", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20261016)
    options = parser.parse_args()
    shifted = synthetic_history(options.seed, executions=options.executions, away=0.3)
    unchanged = synthetic_history(options.seed, executions=options.executions, away=0.0)
    for name, result in (("truth 30% from factory", evaluate(shifted.executions)), ("no true change", evaluate(unchanged.executions))):
        print(name)
        print(f"  post-32 observations: {result.observations}")
        print(f"  median remaining-time error: guarded {_percent(result.guarded_error)}, frozen provisional {_percent(result.frozen_error)}")
        print(f"  80% range coverage: guarded {_percent(result.guarded_coverage)}, frozen provisional {_percent(result.frozen_coverage)}")
        print(f"  gate outcomes: {dict(result.gates)}")
        print(f"  search outcomes: {dict(result.searches)}")
        print(f"  searched tunable moves: {dict(result.search_moves)}")
        print(f"  search promotion evidence: {result.search_promotions}")
    print(f"bad provisional restorations: {restore_count()}")


if __name__ == "__main__":
    main()
