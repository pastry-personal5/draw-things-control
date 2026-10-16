"""The loader: the store's complete runs as a fitted model, cached until a run completes or one is pruned."""

from __future__ import annotations

import sqlite3
from unittest import mock

from framecontrol.jobs.timing.model import StageSpan
from framecontrol.services.timing_history import TimingHistory, load_executions
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

    def test_old_draw_things_cli_override_workload_is_reconciled_from_recorded_facts(self) -> None:
        stale = {**WORKLOAD, "frames": 17, "steps": 40, "knobs": {"frames": 33}}
        self.keep("old-flags", 1, workload=stale, frames=33)
        executions, skipped = load_executions(self.store)
        self.assertEqual(skipped, 0)
        self.assertEqual((executions[0].runs[0].workload["frames"], executions[0].runs[0].workload["steps"]), (33, 2))
        self.assertEqual(stale["frames"], 17)

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


class LoadExecutionsTests(StoreCase):
    """The history as executions, which is what a replay needs: each one's policy, and the waits it recorded."""

    def keep(self, name: str, days_ago: float, *, runs: int = 2, cooldown: dict | None = None, waits: tuple[float | None, ...] = ()) -> int:
        from framecontrol.state.executions import ExecutionSettings, NewExecution, NewRun

        executions = self.store.executions
        execution = executions.start(NewExecution(job_name=name, job_file=f"{name}.yaml", mode="i2v", started_at=iso(days_ago), settings=ExecutionSettings(output_directory="/out", cooldown=cooldown)))
        for number in range(1, runs + 1):
            executions.start_run(execution, number, NewRun(pair="p", positive="t", started_at=iso(days_ago)))
            executions.finish_run(execution, number, status="succeeded", exit_code=0, seconds=100.0, output="a.mov", last_frame=None)
            if number <= len(waits) and waits[number - 1] is not None:
                executions.set_run_cooldown(execution, number, waits[number - 1] or 0.0)
            self.store.timing.replace_run(execution, number, backend="draw-things-cli", pipeline="i2v", workload=WORKLOAD, environment=ENVIRONMENT, fidelity="live", complete=True, spans=spans())
        executions.finish(execution, status="succeeded", exit_code=0, signal=None, finished_at=iso(days_ago))
        return execution

    def test_each_execution_comes_with_its_runs_its_policy_and_the_waits_it_recorded(self) -> None:
        from framecontrol.core.cooldown import CooldownPolicy
        from framecontrol.services.timing_history import load_executions

        self.keep("auto", 3, cooldown={"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 900.0}, waits=(55.0, None))
        self.keep("manual", 2, runs=3, cooldown={"mode": "manual", "seconds": 120.0}, waits=(120.0, 120.0))
        executions, skipped = load_executions(self.store)
        self.assertEqual(skipped, 0)
        self.assertEqual([(item.number, [run.run for run in item.runs]) for item in executions], [(1, [1, 2]), (2, [1, 2, 3])])
        self.assertEqual([item.cooldown for item in executions], [CooldownPolicy("auto", ratio=0.5, minimum_seconds=0.0, maximum_seconds=900.0), CooldownPolicy("manual", seconds=120.0)])
        self.assertEqual([item.waits for item in executions], [(55.0, 0.0), (120.0, 120.0, 0.0)])
        self.assertEqual(executions[0].runs[0].backend, "draw-things-cli")

    def test_an_execution_that_recorded_no_policy_is_off_and_an_old_one_with_seconds_is_manual(self) -> None:
        from framecontrol.core.cooldown import CooldownPolicy
        from framecontrol.services.timing_history import load_executions

        self.keep("plain", 3)
        executions, _ = load_executions(self.store)
        self.assertEqual(executions[0].cooldown, CooldownPolicy("off"))

    def test_an_execution_with_no_complete_run_is_left_out_and_a_database_that_cannot_be_read_gives_none(self) -> None:
        from framecontrol.services.timing_history import load_executions

        self.keep("walk", 3)
        self.store.timing._database.connection().execute("UPDATE run_timing SET complete = 0")
        self.assertEqual(load_executions(self.store), ([], 0))
        with mock.patch.object(type(self.store.timing), "complete_runs", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(load_executions(self.store), ([], 0))
