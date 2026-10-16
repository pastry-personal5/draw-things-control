"""Tests for rebuilding timing rows from the job logs that still exist."""

from __future__ import annotations

import hashlib

from framecontrol.state.executions import ExecutionSettings, NewExecution, NewRun
from framecontrol.state.timing_import import import_timing
from tests.jobs.timing.test_log_import import job_log
from tests.jobs.timing.transcripts import draw_things, h3_r2av
from tests.state.test_store import StoreCase, iso


class TimingImportTests(StoreCase):
    def execution(self, name: str, log_text: str | None, *, backend: str = "draw-things-cli", status: str = "succeeded", runs: int = 1, log_name: str | None = None, run_status: str = "succeeded") -> int:
        directory = self.path.parent
        log = directory / (log_name or f"{name}.log")
        if log_text is not None:
            log.write_text(log_text, encoding="utf-8")
        executions = self.store.executions
        execution_id = executions.start(NewExecution(job_name=name, job_file=f"{name}.yaml", mode="i2av" if backend == "h3" else "i2v", backend=backend, started_at=iso(1), settings=ExecutionSettings(output_directory="/out"), log_path=str(log)))
        for number in range(1, runs + 1):
            executions.start_run(execution_id, number, NewRun(pair="p", positive="text", started_at=iso(1), pipeline="r2av" if backend == "h3" else None, knobs={"seed": 5, "steps": 30}))
            executions.finish_run(execution_id, number, status=run_status, exit_code=0, seconds=1.0, output="a.mp4", last_frame=None, output_width=832, output_height=448, output_frames=243)
        executions.finish(execution_id, status=status, exit_code=0, signal=None, finished_at=iso(1))
        return execution_id

    def test_a_draw_things_log_becomes_a_run_with_log_fidelity_and_its_workload(self) -> None:
        execution_id = self.execution("walk", job_log(draw_things(), lagged=False))
        report = import_timing(self.store)
        self.assertEqual((report.executions, report.runs, report.skipped), (1, 1, []))
        row = self.store.timing.timing_of(execution_id, 1)
        assert row is not None
        self.assertEqual((row.backend, row.fidelity, row.complete, row.pipeline), ("draw-things-cli", "log", True, "i2v"))
        self.assertEqual({key: row.workload[key] for key in ("width", "height", "frames", "pixels", "steps")}, {"width": 832, "height": 448, "frames": 243, "pixels": 832 * 448, "steps": 40})
        self.assertEqual(row.workload["knobs"], {"steps": 30})
        stages = self.store.timing.stages_of(execution_id, 1)
        self.assertEqual([stage.stage for stage in stages], ["prepare", "starting", "processing", "sampling", "finishing", "finalize"])
        self.assertTrue(all(stage.source == "framecontrol" for stage in stages if stage.stage in ("prepare", "finalize")))

    def test_an_h3_log_is_shifted_back_and_its_first_readings_marked(self) -> None:
        execution_id = self.execution("duo", job_log(h3_r2av(), lagged=True), backend="h3")
        import_timing(self.store)
        row = self.store.timing.timing_of(execution_id, 1)
        assert row is not None
        self.assertEqual((row.backend, row.pipeline, row.workload["steps"]), ("h3", "r2av", 30))
        stages = {stage.stage: stage for stage in self.store.timing.stages_of(execution_id, 1)}
        self.assertTrue(stages["ffmpeg"].uncertain)
        self.assertFalse(stages["denoise"].uncertain)
        self.assertEqual((stages["denoise"].done, stages["denoise"].total, len(stages["denoise"].marks)), (30, 30, 31))

    def test_importing_again_changes_nothing(self) -> None:
        self.execution("walk", job_log(draw_things(), lagged=False))
        first = import_timing(self.store)
        before = self.snapshot()
        second = import_timing(self.store)
        self.assertEqual((first.executions, second.executions, second.runs), (1, 0, 0))
        self.assertEqual([reason for _label, reason in second.skipped], ["it already has timing"])
        self.assertEqual(self.snapshot(), before)

    def test_each_execution_it_cannot_use_is_reported_with_why(self) -> None:
        self.execution("no-log", None, log_name="gone.log")
        no_path = self.execution("nopath", None)
        self.store._database.connection().execute("UPDATE executions SET log_path = NULL WHERE id = ?", (no_path,))
        self.execution("not-a-log", "x", log_name="notes.txt")
        self.execution("empty", "just some text\n")
        running = self.execution("running", job_log(draw_things(), lagged=False), status="running")
        timed = self.execution("timed", job_log(draw_things(), lagged=False))
        self.store.timing.start(timed, 1, "draw-things-cli", "i2v", {}, {})
        report = import_timing(self.store)
        self.assertEqual((report.executions, report.runs), (0, 0))
        reasons = dict(report.skipped)
        self.assertEqual(reasons["E0001"], "its log no longer exists")
        self.assertEqual(reasons["E0002"], "no log was written")
        self.assertEqual(reasons["E0003"], "its log path is not a .log file")
        self.assertEqual(reasons["E0004"], "its log holds no run")
        self.assertEqual(reasons["E0005"], "it is still running")
        self.assertEqual(reasons["E0006"], "it already has timing")
        self.assertEqual(len(report.skipped), 6)
        self.assertIsNone(self.store.timing.timing_of(running, 1))

    def test_a_run_the_log_does_not_hold_is_reported_and_the_others_are_kept(self) -> None:
        execution_id = self.execution("walk", job_log(draw_things(), lagged=False), runs=2)
        report = import_timing(self.store)
        self.assertEqual((report.executions, report.runs), (1, 1))
        self.assertEqual(report.skipped, [("E0001 run 2", "its log does not hold the run")])
        self.assertIsNotNone(self.store.timing.timing_of(execution_id, 1))
        self.assertIsNone(self.store.timing.timing_of(execution_id, 2))

    def test_a_run_that_did_not_succeed_is_not_complete(self) -> None:
        execution_id = self.execution("walk", job_log(draw_things(), lagged=False, status="interrupted"), run_status="interrupted")
        import_timing(self.store)
        row = self.store.timing.timing_of(execution_id, 1)
        assert row is not None
        self.assertFalse(row.complete)

    def test_a_log_is_only_read(self) -> None:
        self.execution("walk", job_log(draw_things(), lagged=False))
        log = self.path.parent / "walk.log"
        before = (hashlib.sha256(log.read_bytes()).hexdigest(), log.stat().st_mtime_ns)
        import_timing(self.store)
        self.assertEqual((hashlib.sha256(log.read_bytes()).hexdigest(), log.stat().st_mtime_ns), before)

    def test_a_log_stamped_as_read_is_not_shifted(self) -> None:
        execution_id = self.execution("duo", job_log(h3_r2av(), lagged=False, stamped=True), backend="h3")
        import_timing(self.store)
        self.assertFalse(any(stage.uncertain for stage in self.store.timing.stages_of(execution_id, 1)))

    def test_rows_made_by_the_import_go_with_their_execution(self) -> None:
        execution_id = self.execution("walk", job_log(draw_things(), lagged=False))
        import_timing(self.store)
        number = self.store.executions.number_of(execution_id)
        assert number is not None
        self.store.executions.delete([number], in_use={})
        connection = self.store._database.connection()
        self.assertEqual((connection.execute("SELECT COUNT(*) FROM run_stages").fetchone()[0], connection.execute("SELECT COUNT(*) FROM run_timing").fetchone()[0]), (0, 0))

    def snapshot(self) -> list[tuple[object, ...]]:
        connection = self.store._database.connection()
        return [tuple(row) for row in connection.execute("SELECT * FROM run_stages ORDER BY id")] + [tuple(row) for row in connection.execute("SELECT * FROM run_timing")]
