"""The loader: the store's complete runs as a fitted model, cached until a run completes or one is pruned."""

from __future__ import annotations

import sqlite3
from unittest import mock

from framecontrol.jobs.timing.model import StageSpan
from framecontrol.services.timing_history import TimingHistory
from tests.state.test_store import StoreCase, iso

WORKLOAD = {"width": 832, "height": 448, "pixels": 372736, "frames": 81, "steps": 2, "mode": "i2v", "guidance_scale": 7.5}
ENVIRONMENT = {"model": "base.ckpt", "executable_bytes": 100, "executable_mtime": 1.0}


def spans(step: float = 50.0) -> list[StageSpan]:
    return [
        StageSpan("prepare", None, "framecontrol", 0.0, 1.0),
        StageSpan("sampling", "Sampling", "child", 1.0, 1.0 + 2 * step, 2, 2, "steps", ((1.0, 0), (1.0 + step, 1), (1.0 + 2 * step, 2))),
        StageSpan("verify", None, "framecontrol", 1.0 + 2 * step, 3.0 + 2 * step),
    ]


class TimingHistoryTests(StoreCase):
    def keep(self, name: str, days_ago: float, *, step: float = 50.0, complete: bool = True, fidelity: str = "live", workload: dict | None = None, extra: list[StageSpan] | None = None, frames: int | None = None) -> int:
        execution = self.add(name, started=iso(days_ago), finished=iso(days_ago))
        self.store.timing.replace_run(execution, 1, backend="draw-things-cli", pipeline="i2v", workload=workload if workload is not None else WORKLOAD, environment=ENVIRONMENT if fidelity == "live" else {}, fidelity=fidelity, complete=complete, spans=[*spans(step), *(extra or [])])
        if frames is not None:
            self.store.executions.finish_run(execution, 1, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None, output_frames=frames)
        return execution

    def plan(self, history: TimingHistory):
        return history.model().plan("draw-things-cli", "i2v", WORKLOAD, ENVIRONMENT)

    def test_an_empty_store_gives_a_model_that_prices_nothing(self) -> None:
        plan = self.plan(TimingHistory(self.store))
        self.assertEqual((plan.tier, plan.runs, plan.skipped), ("live_only", 0, 0))

    def test_complete_live_runs_of_a_signature_price_the_next_one(self) -> None:
        for days in (3, 2, 1):
            self.keep(f"walk-{days}", days)
        plan = self.plan(TimingHistory(self.store))
        self.assertEqual((plan.tier, plan.runs, plan.executions, plan.fidelity), ("signature", 3, 3, "live"))

    def test_a_run_whose_timing_is_not_complete_is_not_used(self) -> None:
        for days in (3, 2):
            self.keep(f"walk-{days}", days)
        self.keep("walk-1", 1, complete=False)
        self.assertEqual(self.plan(TimingHistory(self.store)).tier, "live_only")

    def test_a_row_rebuilt_from_a_log_never_forms_a_signature_with_a_live_one(self) -> None:
        for days in (3, 2, 1):
            self.keep(f"walk-{days}", days, fidelity="log", workload={"width": 832, "height": 448, "pixels": 372736, "frames": 81, "steps": 2})
        self.assertNotEqual(self.plan(TimingHistory(self.store)).tier, "signature")

    def test_a_live_row_with_no_frames_joins_the_frames_its_output_measured(self) -> None:
        no_frames = {**WORKLOAD, "frames": None}
        for days in (3, 2, 1):
            self.keep(f"walk-{days}", days, workload=no_frames, frames=81)
        history = TimingHistory(self.store)
        plan = history.model().plan("draw-things-cli", "i2v", no_frames, ENVIRONMENT)
        self.assertEqual(plan.tier, "signature")

    def test_a_phase_the_model_has_never_seen_loads_like_any_other(self) -> None:
        for days in (3, 2, 1):
            self.keep(f"walk-{days}", days, extra=[StageSpan("other", "mystery phase", "child", 3.0 + 100.0, 9.0 + 100.0)])
        plan = self.plan(TimingHistory(self.store))
        self.assertEqual(plan.tier, "signature")

    def test_a_corrupt_row_is_left_out_and_counted_in_the_basis(self) -> None:
        for days in (4, 3, 2, 1):
            self.keep(f"walk-{days}", days)
        self.store.timing._database.connection().execute("UPDATE run_stages SET marks = 'not json' WHERE execution_id = 1")
        plan = self.plan(TimingHistory(self.store))
        self.assertEqual((plan.tier, plan.runs, plan.skipped), ("signature", 3, 1))

    def test_the_fit_is_kept_until_a_run_completes_or_one_is_pruned(self) -> None:
        history = TimingHistory(self.store)
        self.keep("old", 15.5)
        self.keep("recent", 1)
        first = history.model()
        self.assertIs(history.model(), first)
        self.keep("newer", 0.5)
        second = history.model()
        self.assertIsNot(second, first)
        self.assertIs(history.model(), second)
        self.store.prune()
        self.assertIsNot(history.model(), second)

    def test_a_database_that_cannot_be_read_gives_an_empty_model_and_no_error(self) -> None:
        history = TimingHistory(self.store)
        with mock.patch.object(type(self.store.timing), "complete_runs", side_effect=sqlite3.OperationalError("database is locked")):
            plan = self.plan(history)
        self.assertEqual((plan.tier, plan.runs), ("live_only", 0))
