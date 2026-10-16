"""Tests for what the state store keeps of a run's timing: schema 14, the two tables, and how they follow their execution."""

import json
import sqlite3
from dataclasses import replace

from framecontrol.jobs.events import RunFinished, RunStatus, StageFinished, StageStarted, TimingFinished, TimingStarted
from framecontrol.jobs.timing.model import StageSpan
from framecontrol.state.recorder import ExecutionRecorder
from framecontrol.state.schema import MIGRATIONS, SCHEMA_VERSION
from tests.state.test_store import StoreCase, iso


def span(stage: str = "denoise", start: float = 10.0, end: float = 20.0, **changes: object) -> StageSpan:
    return replace(StageSpan(stage, stage, "child", start, end, 2, 2, "steps", ((15.0, 1), (20.0, 2))), **changes)


class TimingSchemaTests(StoreCase):
    def test_schema_14_adds_the_two_tables_without_touching_existing_rows(self) -> None:
        path = self.path.with_name("v13.db")
        connection = sqlite3.connect(path)
        for migration in MIGRATIONS[:13]:
            for statement in migration.split(";\n"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("PRAGMA user_version = 13")
        connection.execute("INSERT INTO executions (job_name, job_file, mode, status, started_at, started_epoch, execution_number) VALUES ('old', 'old.yaml', 'i2v', 'succeeded', '2026-09-01T09:00:00+00:00', 0, 1)")
        connection.execute("INSERT INTO runs (execution_id, number, pair, positive, started_at, started_epoch, status, output) VALUES (1, 1, 'p', 'text', '2026-09-01T09:00:00+00:00', 0, 'succeeded', 'a.mov')")
        before = [tuple(row) for row in connection.execute("SELECT * FROM runs")]
        connection.commit()
        connection.close()
        store = self.open(path=path)
        database = store._database.connection()
        self.assertEqual(SCHEMA_VERSION, 14)
        self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 14)
        self.assertEqual([tuple(row) for row in database.execute("SELECT * FROM runs")], before)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM run_stages").fetchone()[0], 0)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM run_timing").fetchone()[0], 0)
        self.assertEqual(store.timing.stages_of(1, 1), [])
        self.assertIsNone(store.timing.timing_of(1, 1))


class TimingRepositoryTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.execution = self.add("walk", started=iso(1), finished=iso(1))

    def test_a_run_is_marked_incomplete_when_its_timing_starts_and_complete_when_it_ends(self) -> None:
        timing = self.store.timing
        timing.start(self.execution, 1, "h3", "r2av", {"frames": 243}, {"load_start": [1.0, 1.0, 1.0]})
        row = timing.timing_of(self.execution, 1)
        assert row is not None
        self.assertEqual((row.backend, row.pipeline, row.workload, row.fidelity, row.complete), ("h3", "r2av", {"frames": 243}, "live", False))
        timing.finish(self.execution, 1, complete=True, environment={"load_end": [2.0, 1.5, 1.0]})
        row = timing.timing_of(self.execution, 1)
        assert row is not None
        self.assertEqual((row.complete, row.environment), (True, {"load_start": [1.0, 1.0, 1.0], "load_end": [2.0, 1.5, 1.0]}))

    def test_a_span_comes_back_as_written_in_the_order_of_its_start(self) -> None:
        timing = self.store.timing
        timing.add_stage(self.execution, 1, span("late", 30.0, 40.0))
        timing.add_stage(self.execution, 1, span("early", -2.0, -1.0, source="framecontrol", phase=None, done=None, total=None, unit=None, marks=()))
        timing.add_stage(self.execution, 1, span("middle", 10.0, 20.0))
        timing.add_stage(self.execution, 2, span("other run"))
        rows = timing.stages_of(self.execution, 1)
        self.assertEqual([row.stage for row in rows], ["early", "middle", "late"])
        middle = rows[1]
        self.assertEqual((middle.phase, middle.source, middle.start, middle.end, middle.done, middle.total, middle.unit, middle.marks, middle.uncertain), ("middle", "child", 10.0, 20.0, 2, 2, "steps", ((15.0, 1), (20.0, 2)), False))
        self.assertEqual((rows[0].phase, rows[0].done, rows[0].marks), (None, None, ()))

    def test_marks_are_kept_whole_to_a_thousand_and_thinned_beyond(self) -> None:
        marks = tuple((float(index), index) for index in range(2500))
        self.store.timing.add_stage(self.execution, 1, span("long", 0.0, 2500.0, marks=marks))
        (row,) = self.store.timing.stages_of(self.execution, 1)
        self.assertEqual(len(row.marks), 1000)
        self.assertEqual((row.marks[0], row.marks[-1]), (marks[0], marks[-1]))

    def test_an_uncertain_first_mark_is_flagged_in_the_marks_and_comes_back_flagged(self) -> None:
        self.store.timing.add_stage(self.execution, 1, span(uncertain=True))
        (row,) = self.store.timing.stages_of(self.execution, 1)
        self.assertTrue(row.uncertain)
        self.assertEqual(row.marks, ((15.0, 1), (20.0, 2)))
        stored = self.store._database.connection().execute("SELECT marks FROM run_stages").fetchone()[0]
        self.assertEqual(json.loads(stored)[0], [15.0, 1, 1])

    def test_replacing_a_runs_rows_keeps_the_other_runs(self) -> None:
        timing = self.store.timing
        timing.add_stage(self.execution, 1, span("old"))
        timing.add_stage(self.execution, 2, span("kept"))
        timing.replace_run(self.execution, 1, backend="draw-things-cli", pipeline="i2v", workload={}, environment={}, fidelity="log", complete=True, spans=[span("new")])
        self.assertEqual([row.stage for row in timing.stages_of(self.execution, 1)], ["new"])
        self.assertEqual([row.stage for row in timing.stages_of(self.execution, 2)], ["kept"])
        row = timing.timing_of(self.execution, 1)
        assert row is not None
        self.assertEqual((row.fidelity, row.complete), ("log", True))

    def test_an_execution_knows_whether_it_has_timing(self) -> None:
        self.assertFalse(self.store.timing.has_timing(self.execution))
        self.store.timing.start(self.execution, 1, "h3", None, {}, {})
        self.assertTrue(self.store.timing.has_timing(self.execution))


class TimingFollowsItsExecutionTests(StoreCase):
    def keep(self, execution_id: int) -> None:
        self.store.timing.start(execution_id, 1, "h3", "r2av", {}, {})
        self.store.timing.add_stage(execution_id, 1, span())

    def counts(self) -> tuple[int, int]:
        connection = self.store._database.connection()
        return connection.execute("SELECT COUNT(*) FROM run_stages").fetchone()[0], connection.execute("SELECT COUNT(*) FROM run_timing").fetchone()[0]

    def test_pruning_by_retention_removes_the_rows_with_the_execution(self) -> None:
        old = self.add("old", started=iso(15.5), finished=iso(15))
        recent = self.add("recent", started=iso(13.5), finished=iso(13))
        self.keep(old)
        self.keep(recent)
        self.assertEqual(self.counts(), (2, 2))
        self.store.prune()
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(len(self.store.timing.stages_of(recent, 1)), 1)

    def test_deleting_an_execution_removes_the_rows_with_it(self) -> None:
        execution = self.add("walk", started=iso(1), finished=iso(1))
        number = self.store.executions.number_of(execution)
        assert number is not None
        self.keep(execution)
        self.store.executions.delete([number], in_use={})
        self.assertEqual(self.counts(), (0, 0))


class RecorderTimingTests(StoreCase):
    """The recorder keeps each span when it finishes, so a run that is killed keeps what it had finished."""

    AT = "2026-10-16T10:00:00+00:00"

    def recorder(self) -> ExecutionRecorder:
        recorder = ExecutionRecorder(self.store)
        from framecontrol.core.cooldown import CooldownPolicy
        from framecontrol.jobs.events import JobStarted, RunStarted

        recorder(JobStarted(at=self.AT, job_name="walk", job_file="/jobs/walk.yaml", source_text="x", mode="i2v", total_runs=1, output_directory="/out", input=None, model="m", seed=1, seed_source="given", cooldown=CooldownPolicy(mode="off"), cooldown_source="job", manifest=None, log=None, backend="h3"))
        recorder(RunStarted(at=self.AT, number=1, total=1, pair="p", positive="t", negative=None, input=None, resized_input=None, output="o.mp4", last_frame=None, command=()))
        return recorder

    def finished(self, stage: str, start: float, end: float, **changes: object) -> StageFinished:
        return StageFinished(at=self.AT, number=1, stage=stage, phase=None, source="framecontrol", start=start, end=end, done=None, total=None, unit=None, marks=(), **changes)  # type: ignore[arg-type]

    def test_a_run_killed_mid_stage_keeps_its_finished_spans_and_stays_incomplete(self) -> None:
        recorder = self.recorder()
        recorder(TimingStarted(at=self.AT, number=1, backend="h3", pipeline="r2av", workload={"frames": 243}, environment={}))
        recorder(StageStarted(at=self.AT, number=1, stage="prepare", phase=None, source="framecontrol", start=0.0))
        recorder(self.finished("prepare", 0.0, 0.5))
        recorder(self.finished("tokenize", 0.5, 0.7))
        recorder(StageStarted(at=self.AT, number=1, stage="denoise", phase="denoise", source="child", start=0.7))
        # The process is killed here: no TimingFinished, no RunFinished.
        execution_id = recorder.execution_id
        assert execution_id is not None
        self.assertEqual([row.stage for row in self.store.timing.stages_of(execution_id, 1)], ["prepare", "tokenize"])
        row = self.store.timing.timing_of(execution_id, 1)
        assert row is not None
        self.assertFalse(row.complete)

    def test_a_run_that_ends_well_is_complete_and_keeps_the_load_it_ended_under(self) -> None:
        recorder = self.recorder()
        recorder(TimingStarted(at=self.AT, number=1, backend="h3", pipeline="i2av", workload={}, environment={"load_start": [1.0, 1.0, 1.0]}))
        recorder(TimingFinished(at=self.AT, number=1, complete=True, load_end=(2.0, 1.0, 1.0)))
        recorder(RunFinished(at=self.AT, number=1, status=RunStatus.SUCCEEDED, exit_code=0, seconds=1.0, output="o.mp4", last_frame=None))
        execution_id = recorder.execution_id
        assert execution_id is not None
        row = self.store.timing.timing_of(execution_id, 1)
        assert row is not None
        self.assertEqual((row.complete, row.pipeline, row.environment["load_end"]), (True, "i2av", [2.0, 1.0, 1.0]))

    def test_a_timing_write_that_fails_is_skipped_and_recording_goes_on(self) -> None:
        recorder = self.recorder()

        def broken(*_arguments: object) -> None:
            raise sqlite3.OperationalError("disk is full")

        self.store.timing.add_stage = broken  # type: ignore[method-assign]
        recorder(self.finished("prepare", 0.0, 0.5))
        recorder(self.finished("verify", 0.5, 0.7))
        recorder(RunFinished(at=self.AT, number=1, status=RunStatus.SUCCEEDED, exit_code=0, seconds=1.0, output="o.mp4", last_frame=None))
        execution_id = recorder.execution_id
        assert execution_id is not None
        execution = self.store.executions.get(execution_id)
        assert execution is not None
        self.assertEqual(execution.runs[0].status, "succeeded")
