"""Pure helpers for the constrained M15 tunables search."""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, replace

from framecontrol.jobs.timing.calibrate import calibrate_rows, calibrate_validated
from framecontrol.jobs.timing.factory import SEARCH_GRIDS
from framecontrol.jobs.timing.features import HistoryRun
from framecontrol.jobs.timing.fit import read_row
from framecontrol.jobs.timing.gate import ReplayCase, prepare_replay, replay
from framecontrol.jobs.timing.knobs import KNOBS_LEARNED, Calibration, PipelineCalibration, Tunables

SEARCH_CONFIDENCE = 0.99
MIN_RELATIVE_TRAINING_GAIN = 0.01


@dataclass(frozen=True)
class SearchGroup:
    backend: str
    pipeline: str
    training: tuple[HistoryRun, ...]
    baseline: PipelineCalibration
    pins: Mapping[str, float]


@dataclass(frozen=True)
class SearchChoice:
    name: str
    tunables: Tunables
    calibrations: Mapping[str, PipelineCalibration]
    current_error: float
    candidate_error: float


def neighbours(tunables: Tunables, pinned: Set[str] = frozenset(), *, rho_measured: bool = False) -> tuple[tuple[str, Tunables], ...]:
    """One-grid-step candidates, excluding pins and inactive/safety dimensions."""
    result: list[tuple[str, Tunables]] = []
    for name, grid in SEARCH_GRIDS.items():
        if name in pinned or (name == "rho" and rho_measured):
            continue
        value = float(getattr(tunables, name))
        try:
            index = grid.index(value)
        except ValueError:
            continue
        for next_index in (index - 1, index + 1):
            if 0 <= next_index < len(grid):
                result.append((name, replace(tunables, **{name: grid[next_index]})))
    return tuple(result)


def _with_pipeline(calibration: Calibration, group: SearchGroup, value: PipelineCalibration) -> Calibration:
    backend = calibration.backends[group.backend]
    return replace(calibration, backends={**calibration.backends, group.backend: replace(backend, pipelines={**backend.pipelines, group.pipeline: value})})


def _cross_validated_error(tunables: Tunables, calibration: Calibration, groups: Sequence[SearchGroup], prepared: Mapping[str, Mapping[int, ReplayCase]], stamp: str, should_stop: Callable[[], bool], deadline: float) -> float:
    errors: list[float] = []
    for group in groups:
        executions = sorted({run.execution for run in group.training})
        rows = [read_row(run, tunables) for run in group.training if run.fidelity == "live"]
        for execution in executions:
            if should_stop() or time.process_time() >= deadline:
                return math.inf
            fit_rows = [row for row in rows if row.execution != execution]
            fitted = calibrate_rows(group.baseline, group.baseline, fit_rows, tunables, checkpoint=32, stamp=stamp, pins=group.pins, source=KNOBS_LEARNED)
            if fitted is None:
                continue
            held = [run for run in group.training if run.execution == execution]
            score = replay(held, tunables, _with_pipeline(calibration, group, fitted), cases=(prepared[f"{group.backend}/{group.pipeline}"][execution],))
            if not score.valid:
                return math.inf
            errors.extend(score.errors.values())
    return statistics.fmean(errors) if errors else math.inf


def select(current: Tunables, calibration: Calibration, groups: Sequence[SearchGroup], *, pinned: Set[str] = frozenset(), rho_measured: bool = False, stamp: str, should_stop: Callable[[], bool] = lambda: False, cpu_seconds: float = 10.0) -> SearchChoice | None:
    """Choose one neighbouring tunable using only leave-one-execution-out training replay.

    The returned calibrations are fitted on each complete training partition;
    callers alone may open the reserved holdout and decide whether to commit.
    """
    if not groups or cpu_seconds <= 0:
        return None
    deadline = time.process_time() + cpu_seconds
    prepared: dict[str, dict[int, ReplayCase]] = {}
    for group in groups:
        name = f"{group.backend}/{group.pipeline}"
        prepared[name] = {}
        for case in prepare_replay(group.training):
            prepared[name][case.execution.number] = case
    current_error = _cross_validated_error(current, calibration, groups, prepared, stamp, should_stop, deadline)
    best: tuple[float, str, Tunables] | None = None
    for name, candidate in neighbours(current, pinned, rho_measured=rho_measured):
        if should_stop() or time.process_time() >= deadline:
            return None
        error = _cross_validated_error(candidate, calibration, groups, prepared, stamp, should_stop, deadline)
        if math.isfinite(error) and (best is None or (error, name) < (best[0], best[1])):
            best = (error, name, candidate)
    if best is None or not math.isfinite(current_error) or best[0] >= current_error or (current_error - best[0]) / current_error < MIN_RELATIVE_TRAINING_GAIN:
        return None
    fitted: dict[str, PipelineCalibration] = {}
    for group in groups:
        if should_stop() or time.process_time() >= deadline:
            return None
        value = calibrate_validated(group.baseline, group.training, best[2], checkpoint=32, stamp=stamp, pins=group.pins, source=KNOBS_LEARNED)
        if value is None:
            return None
        fitted[f"{group.backend}/{group.pipeline}"] = value
    return SearchChoice(best[1], best[2], fitted, current_error, best[0])


__all__ = ["MIN_RELATIVE_TRAINING_GAIN", "SEARCH_CONFIDENCE", "SearchChoice", "SearchGroup", "neighbours", "select"]
