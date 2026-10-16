"""The timing history as a fitted model: the store's complete runs, read once and kept until a run completes or pruning removes
one. Nothing here ever raises into a worker or a front end: a store that cannot be read gives a model that prices nothing."""

from __future__ import annotations

import threading
from collections import defaultdict

from loguru import logger

from framecontrol.core.cooldown import CooldownPolicy, parse_cooldown
from framecontrol.jobs.timing.features import HistoryExecution, HistoryRun, HistoryStage
from framecontrol.jobs.timing.fit import FittedModel
from framecontrol.state.execution_rows import ExecutionRow
from framecontrol.state.store import Store
from framecontrol.state.timing import CompleteTiming


class TimingHistory:
    """The model fitted on a store's timing rows, cached by the number of complete runs and the newest of them."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._mark: tuple[int, tuple[int, int] | None] | None = None
        self._model: FittedModel | None = None

    def model(self) -> FittedModel:
        """The fitted model for the store as it is now. A fit takes up to 200 ms, so it is made again only when a run has
        completed since it was, or pruning has removed some."""
        try:
            with self._lock:
                mark = self._store.timing.completion_mark()
                if self._model is not None and mark == self._mark:
                    return self._model
                rows, skipped = self._store.timing.complete_runs()
                self._model = FittedModel([_history_run(row) for row in rows], skipped=skipped)
                self._mark = mark
                return self._model
        except Exception:
            logger.exception("The timing history could not be read; forecasts have none to rest on")
            return FittedModel([])


def _history_run(row: CompleteTiming) -> HistoryRun:
    stages = tuple(HistoryStage(stage.stage, stage.source, stage.start, stage.end, stage.done, stage.total, stage.unit, stage.marks, stage.uncertain) for stage in row.stages)
    workload = dict(row.workload)
    if row.backend == "draw-things-cli" and row.fidelity == "live":
        # Older timing rows used base-config values even when per-run CLI flags won. Reconcile only where independent
        # recorded facts establish the actual work; leave the stored row intact.
        knobs = workload.get("knobs")
        if isinstance(knobs, dict) and knobs.get("frames") == row.output_frames and workload.get("frames") != row.output_frames:
            workload["frames"] = row.output_frames
        sampling = next((stage for stage in stages if stage.stage == "sampling" and stage.source == "child" and stage.done == stage.total and stage.total is not None and stage.total > 0), None)
        if sampling is not None:
            workload["steps"] = sampling.total
    corrected = any(stage.stage == "correct" and stage.source == "framecontrol" for stage in stages)
    if row.backend in ("draw-things-cli", "h3") and corrected:
        workload["color_corrects"] = True
    elif (row.backend == "h3" and row.pipeline == "r2av") or (row.backend == "draw-things-cli" and row.pipeline == "t2v" and row.run == 1):
        # Older workload hooks marked the job's color policy, even for a run with no correction input.
        workload.pop("color_corrects", None)
    return HistoryRun(row.execution, row.run, row.fidelity, row.backend, row.pipeline, workload, row.environment, stages, row.output_frames)


def load_executions(store: Store) -> tuple[list[HistoryExecution], int]:
    """The store's complete runs grouped by execution, oldest first, each with the cooldown policy it ran under and the
    seconds it recorded waiting after each run, and the number of rows that could not be read. Nothing here raises: a store
    that cannot be read has no executions."""
    try:
        rows, skipped = store.timing.complete_runs()
        by_number: dict[int, list[CompleteTiming]] = defaultdict(list)
        for row in rows:
            by_number[row.execution].append(row)
        executions = []
        for number, runs in sorted(by_number.items()):
            stored = store.executions.by_number(number)
            recorded = {run.number: run.cooldown_after_seconds for run in stored.runs} if stored is not None else {}
            waits = tuple(recorded.get(run) or 0.0 for run in range(1, max(row.run for row in runs) + 1))
            executions.append(HistoryExecution(number, _policy_of(stored), tuple(_history_run(row) for row in runs), waits))
        return executions, skipped
    except Exception:
        logger.exception("The timing history could not be read as executions")
        return [], 0


def _policy_of(execution: ExecutionRow | None) -> CooldownPolicy:
    """The cooldown an execution ran under: its resolved mapping, else the seconds an older row kept (a manual wait), else none."""
    if execution is None:
        return CooldownPolicy("off")
    mapping = execution.settings.cooldown
    if isinstance(mapping, dict):
        try:
            return parse_cooldown(mapping, "cooldown")
        except ValueError:
            pass
    return CooldownPolicy("manual", seconds=float(execution.cooldown_seconds)) if execution.cooldown_seconds else CooldownPolicy("off")
