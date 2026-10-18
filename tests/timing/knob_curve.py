"""The knobs' learning curve: a history replayed in time order through the real update schedule, from no files.

Each execution is forecast from what the updater had learned before it, with no run history behind the forecast (what a host
has once pruning has taken the rows, and the case the files exist for), and then its runs are fed to the updater. The
observations are grouped by the completed runs of the pipeline at the time (0, 1, 2, 4, 8, 16, 32, and after 32) and by how
often the exact size had run before (0, 1, 5 or more). Three variants are measured, so each addition is: the factory knobs
with no files, the calibration without size offsets and knob effects, and the full calibration. Run it for the report::

    uv run python -m tests.timing.knob_curve [--executions 600] [--away 0.3]
"""

from __future__ import annotations

import argparse
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from framecontrol.jobs.timing.calibrate import CHECKPOINTS, checkpoint_reached
from framecontrol.jobs.timing.factory import factory_calibration
from framecontrol.jobs.timing.features import HistoryExecution
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.jobs.timing.knobs import Calibration, Tunables
from framecontrol.jobs.timing.step_terms import size_key
from framecontrol.services.knob_updater import KnobUpdateEngine
from framecontrol.services.timing_knob_files import KnobFiles
from framecontrol.services.timing_knobs import load_knobs
from framecontrol.services.timing_replay import CHECKPOINTS_RUN, ForecastEstimator, Observation, ReplayReport, bootstrap_interval, cells_of, score_execution
from tests.timing.synthetic import synthetic_history

VARIANTS = ("factory", "no-offsets", "full")
SEEN_BUCKETS = ((0, "0"), (1, "1"), (5, "5+"))


def run_bucket(count: int) -> str:
    """The label of the checkpoint a pipeline's completed-run count has reached: ``0``, ``1``, ``2``, ``4``, ``8``, ``16``, ``32``, or ``32+``."""
    return "32+" if count > CHECKPOINTS[-1] else str(checkpoint_reached(count))


def seen_bucket(seen: int) -> str:
    return next(label for floor, label in reversed(SEEN_BUCKETS) if seen >= floor)


@dataclass
class Curve:
    variant: str
    by_runs: dict[str, list[Observation]]
    by_seen: dict[str, list[Observation]]
    executions: dict[str, set[int]]


def without_offsets(calibration: Calibration) -> Calibration:
    """The calibration with the size offsets and knob effects switched off, so what they add can be told."""
    backends = {}
    for name, backend in calibration.backends.items():
        backends[name] = replace(backend, pipelines={key: replace(item, step=replace(item.step, sizes={}, knobs={})) for key, item in backend.pipelines.items()})
    return replace(calibration, backends=backends)


def learning_curve(executions: Sequence[HistoryExecution], variant: str, directory: Path, *, tunables: Tunables | None = None) -> Curve:
    """Replay ``executions`` in order, learning from each after forecasting it."""
    files = KnobFiles(directory)
    engine = KnobUpdateEngine(files, clock=lambda: "2026-10-18T12:00:00+09:00")
    curve = Curve(variant, defaultdict(list), defaultdict(list), defaultdict(set))
    history = []
    sizes_seen: dict[tuple[str, str | None, str | None], int] = defaultdict(int)
    for execution in executions:
        loaded = load_knobs(files)
        calibration = factory_calibration() if variant == "factory" else without_offsets(loaded.calibration) if variant == "no-offsets" else loaded.calibration
        model = FittedModel([], tunables=tunables or loaded.tunables, calibration=calibration)
        counts = files.read_written().groups
        report = ReplayReport()
        score_execution(execution, [], model, ForecastEstimator, report, run_checkpoints=(0.0, *CHECKPOINTS_RUN))
        runs = {run.run: run for run in execution.runs}
        for item in report.observations:
            run = runs[item.run] if item.run is not None else execution.runs[0]
            bucket = run_bucket(int(counts.get(f"{run.backend}/{run.pipeline}", {}).get("count", 0)))
            seen = seen_bucket(sizes_seen[(run.backend, run.pipeline, size_key(run.workload))])
            curve.by_runs[bucket].append(item)
            curve.by_seen[seen].append(item)
            curve.executions[bucket].add(execution.number)
        for run in execution.runs:
            sizes_seen[(run.backend, run.pipeline, size_key(run.workload))] += 1
        history.extend(execution.runs)
        if variant != "factory":
            engine.update(history)
    return curve


def _interval(errors: list[float]) -> str:
    if len(errors) < 8:
        return ""
    low, high = bootstrap_interval(errors)
    return f"[{low * 100:.0f}%..{high * 100:.0f}%]"


SCOPES = {"start": "at the start of a run (0%)", "early": "in the first 10% of a run", "all": "at every checkpoint"}
LATEST = {"start": 0.0, "early": 0.10, "all": 1.0}


def curve_lines(curves: Sequence[Curve], scope: str = "all") -> list[str]:
    """The curves as a table: per completed-run bucket and per size-seen bucket, the run-level median error of the time left
    (at the start of a run, where the prior is all there is, in its first 10%, or at every checkpoint), with the 90% interval of
    the median, the 80% range's coverage, and the runs and independent executions behind it."""
    lines: list[str] = []
    for title, field_name, order in (("completed runs of the pipeline", "by_runs", ["0", "1", "2", "4", "8", "16", "32", "32+"]), ("times the exact size had run", "by_seen", [label for _, label in SEEN_BUCKETS])):
        lines += ["", f"By {title} (run-level time left, {SCOPES[scope]})", f"  {'':>8}  " + "  ".join(f"{variant:>34}" for variant in (curve.variant for curve in curves))]
        for label in order:
            cells = []
            for curve in curves:
                items = [item for item in getattr(curve, field_name).get(label, []) if item.level == "run" and (item.checkpoint is not None and item.checkpoint <= LATEST[scope])]
                cell = cells_of(items, ("level",))
                if not cell:
                    cells.append(f"{'-':>34}")
                    continue
                errors = [abs(item.estimate - item.truth) / item.truth for item in items if item.estimate is not None and item.truth > 0]
                coverage = f"{cell[0].coverage:.2f}" if cell[0].coverage is not None else "-"
                executions = len({item.execution for item in items})
                cells.append(f"{f'{cell[0].median_ape * 100:5.1f}% {_interval(errors):>13} cov {coverage} n={cell[0].runs}/{executions}':>34}")
            lines.append(f"  {label:>8}  " + "  ".join(cells))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executions", type=int, default=600)
    parser.add_argument("--away", type=float, default=0.3)
    parser.add_argument("--seed", type=int, default=20261016)
    options = parser.parse_args()
    history = synthetic_history(options.seed, executions=options.executions, away=options.away)
    for backend in ("draw-things-cli", "h3-metal-cli"):
        subset = [execution for execution in history.executions if execution.runs[0].backend == backend]
        curves = []
        for variant in VARIANTS:
            with tempfile.TemporaryDirectory() as directory:
                curves.append(learning_curve(subset, variant, Path(directory) / "timing"))
        print(f"\n{backend}: {len(subset)} executions, truth {options.away:.0%} away from the factory")
        for scope in ("start", "all"):
            print("\n".join(curve_lines(curves, scope)))


if __name__ == "__main__":
    main()
