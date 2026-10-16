"""The timing history as a fitted model: the store's complete runs, read once and kept until a run completes or pruning removes
one. Nothing here ever raises into a worker or a front end: a store that cannot be read gives a model that prices nothing."""

from __future__ import annotations

import threading

from loguru import logger

from framecontrol.jobs.timing.features import HistoryRun, HistoryStage
from framecontrol.jobs.timing.fit import FittedModel
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
    return HistoryRun(row.execution, row.run, row.fidelity, row.backend, row.pipeline, row.workload, row.environment, stages, row.output_frames)
