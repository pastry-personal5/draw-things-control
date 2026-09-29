"""Tests for the SQLite state store: the database file, the execution repository, and the store that opens them."""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from draw_things_control.state import database as database_module
from draw_things_control.state.database import Database, StateError
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.schema import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_VERSION
from draw_things_control.state.store import Store, StoreMode

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def iso(days_ago: float, offset: str = "+00:00") -> str:
    return (NOW - timedelta(days=days_ago)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + offset


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "dtc.db"
        self.store = self.open()

    def open(self, retention_days: int = 14, mode: StoreMode = StoreMode.RUN, path: Path | None = None, now: datetime = NOW) -> Store:
        store = Store.open(path or self.path, mode=mode, retention_days=retention_days, clock=lambda: now)
        self.addCleanup(store.close)
        return store

    def add(self, name: str, *, started: str, finished: str | None = None, status: str = "succeeded", runs: int = 1) -> int:
        executions = self.store.executions
        execution_id = executions.start(NewExecution(job_name=name, job_file=f"{name}.yaml", mode="i2v", started_at=started, settings=ExecutionSettings(output_directory="/out")))
        for number in range(1, runs + 1):
            executions.start_run(execution_id, number, NewRun(pair="p", positive="text", started_at=started, command=["draw-things-cli", "--prompt", "text"]))
            if finished is not None:
                executions.finish_run(execution_id, number, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
        if finished is not None:
            executions.finish(execution_id, status=status, exit_code=0, signal=None, finished_at=finished)
        return execution_id

    def stored(self, execution_id: int):
        execution = self.store.executions.get(execution_id)
        assert execution is not None
        return execution


class DatabaseTests(StoreCase):
    def test_a_new_database_uses_wal_foreign_keys_and_the_current_schema(self) -> None:
        connection = self.store._database.connection()
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_opening_a_second_store_keeps_the_first_ones_writes_visible_to_other_processes(self) -> None:
        # The API server opens several stores on one file while it runs a job; a TUI process then opens and closes the
        # database. Opening an existing file used to release this process's SQLite locks, so the TUI's close deleted the WAL
        # and the job's later writes went to that unlinked file, never seen by any other process.
        execution_id = self.add("first", started=iso(1))
        self.open(mode=StoreMode.WRITE)
        other = f"import sqlite3; connection = sqlite3.connect({str(self.path)!r}); connection.execute('SELECT COUNT(*) FROM runs').fetchone(); connection.close()"
        subprocess.run([sys.executable, "-c", other], check=True)
        self.store.executions.finish_run(execution_id, 1, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
        reader = f"import sqlite3; print(sqlite3.connect({str(self.path)!r}).execute('SELECT status FROM runs').fetchone()[0])"
        self.assertEqual(subprocess.run([sys.executable, "-c", reader], check=True, capture_output=True, text=True).stdout.strip(), "succeeded")

    def test_reopening_keeps_the_data(self) -> None:
        execution_id = self.add("first", started=iso(1), finished=iso(1))
        self.store.close()
        again = self.open()
        row = again.executions.get(execution_id)
        assert row is not None
        self.assertEqual(row.job_name, "first")

    def test_a_version_1_database_is_upgraded_on_open_with_every_row_kept(self) -> None:
        path = self.path.with_name("old.db")
        connection = sqlite3.connect(path)
        for statement in SCHEMA_V1.split(";\n"):
            if statement.strip():
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.execute("INSERT INTO executions (job_name, job_file, mode, status, started_at, started_epoch) VALUES ('old', 'old.yaml', 'i2v', 'succeeded', '2026-09-01T09:00:00+00:00', 0)")
        connection.execute("INSERT INTO runs (execution_id, number, pair, positive, started_at, started_epoch, status, output) VALUES (1, 1, 'p', 'text', '2026-09-01T09:00:00+00:00', 0, 'succeeded', 'a.mov')")
        connection.commit()
        connection.close()
        # Opened only to browse, as the TUI's history reader opens it: the upgrade is the one write it makes.
        store = self.open(mode=StoreMode.BROWSE, path=path)
        self.assertEqual(store._database.connection().execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        execution = store.executions.get(1)
        assert execution is not None
        self.assertEqual([(run.output, run.output_width, run.output_height, run.output_frames) for run in execution.runs], [("a.mov", None, None, None)])

    def test_a_database_with_a_newer_schema_is_refused(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        with self.assertRaisesRegex(StateError, "newer version"):
            Store.open(self.path)

    def test_a_filesystem_without_wal_is_refused(self) -> None:
        real_connect = sqlite3.connect

        class NoWal:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection
                self.row_factory = None

            def execute(self, sql: str, *arguments: Any):
                if "journal_mode" in sql:
                    return self.connection.execute("PRAGMA journal_mode = DELETE")
                return self.connection.execute(sql, *arguments)

            def close(self) -> None:
                self.connection.close()

        with mock.patch.object(database_module.sqlite3, "connect", lambda *a, **k: NoWal(real_connect(*a, **k))):
            with self.assertRaisesRegex(StateError, "does not support SQLite WAL"):
                Store.open(Path(self._temporary.name) / "other.db")

    def test_a_failed_commit_is_rolled_back_so_later_writes_still_work(self) -> None:
        real = self.store._database.connection()

        class FailingCommit:
            def __init__(self) -> None:
                self.fail = True

            def __getattr__(self, name: str):
                return getattr(real, name)

            def execute(self, sql: str, *arguments: Any):
                if sql == "COMMIT" and self.fail:
                    self.fail = False
                    raise sqlite3.OperationalError("database is locked")
                return real.execute(sql, *arguments)

        self.store._database._local.connection = FailingCommit()
        with self.assertRaises(sqlite3.OperationalError):
            self.add("first", started=iso(1))
        # The transaction is gone, so the next write works instead of failing to begin.
        self.add("second", started=iso(1))
        self.assertEqual([row.job_name for row in self.store.executions.page()], ["second"])

    def test_a_database_that_fails_to_open_closes_its_connection(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        closed: list[bool] = []
        with mock.patch.object(Database, "close", autospec=True, side_effect=lambda database: closed.append(True)):
            with self.assertRaises(StateError):
                Store.open(self.path)
        self.assertEqual(closed, [True])

    def test_browsing_never_creates_the_database(self) -> None:
        missing = Path(self._temporary.name) / "missing.db"
        with self.assertRaises(StateError):
            Store.open(missing, mode=StoreMode.BROWSE)
        self.assertFalse(missing.exists())


class ExecutionTests(StoreCase):
    def test_a_finished_run_keeps_its_measured_output(self) -> None:
        execution_id = self.store.executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at=iso(0)))
        self.store.executions.start_run(execution_id, 1, NewRun(pair="p", positive="text", started_at=iso(0)))
        self.store.executions.finish_run(execution_id, 1, status="succeeded", exit_code=0, seconds=4.0, output="a.mov", last_frame=None, output_width=832, output_height=448, output_frames=81)
        run = self.stored(execution_id).runs[0]
        self.assertEqual((run.output_width, run.output_height, run.output_frames), (832, 448, 81))

    def test_the_latest_successful_run_of_any_job_is_found(self) -> None:
        executions = self.store.executions
        self.assertIsNone(executions.latest_succeeded_run())
        self.add("older", started=iso(2), finished=iso(2))
        newer = executions.start(NewExecution(job_name="newer", job_file="newer.yaml", mode="i2v", started_at=iso(1)))
        executions.start_run(newer, 1, NewRun(pair="p", positive="text", started_at=iso(1), command=["draw-things-cli", "--steps", "8"]))
        executions.finish_run(newer, 1, status="failed", exit_code=1, seconds=3.0, output=None, last_frame=None)
        run = executions.latest_succeeded_run()
        assert run is not None
        # A failed run is never the one to estimate from.
        self.assertEqual((run.seconds, run.command), (1.0, ("draw-things-cli", "--prompt", "text")))

    def test_an_execution_round_trips_with_its_runs_and_settings(self) -> None:
        execution = self.stored(self.add("walk", started=iso(1), finished=iso(1), runs=2))
        self.assertEqual((execution.status, execution.settings, execution.job_file), ("succeeded", ExecutionSettings(output_directory="/out"), "walk.yaml"))
        self.assertEqual([(run.number, run.status, run.command) for run in execution.runs], [(1, "succeeded", ("draw-things-cli", "--prompt", "text")), (2, "succeeded", ("draw-things-cli", "--prompt", "text"))])
        self.assertIsNone(self.store.executions.get(999))

    def test_settings_keep_only_the_keys_that_are_set_and_read_older_rows(self) -> None:
        settings = ExecutionSettings(output_directory="/out", cooldown={"mode": "off"})
        self.assertEqual(settings.to_json(), '{"output_directory": "/out", "cooldown": {"mode": "off"}}')
        self.assertEqual(ExecutionSettings.from_json(settings.to_json()), settings)
        # A row written before some keys existed, or with keys since dropped.
        self.assertEqual(ExecutionSettings.from_json('{"input": "a.png", "extra": 1, "input_resize": null}'), ExecutionSettings(input="a.png"))
        self.assertEqual(ExecutionSettings.from_json(""), ExecutionSettings())

    def test_listing_is_newest_first_by_time_not_by_text_and_filters_and_pages(self) -> None:
        # 10:00+02:00 is 08:00 UTC, earlier than 09:30 UTC, though its text sorts later.
        older = self.add("older", started="2026-09-01T10:00:00+02:00", finished="2026-09-01T10:30:00+02:00")
        newer = self.add("newer", started="2026-09-01T09:30:00+00:00", finished="2026-09-01T10:00:00+00:00", status="failed")
        page = self.store.executions.page
        self.assertEqual([row.id for row in page()], [newer, older])
        self.assertEqual([row.id for row in page(status="failed")], [newer])
        self.assertEqual([row.id for row in page(name="older")], [older])
        self.assertEqual([row.id for row in page(limit=1, offset=1)], [older])

    def test_name_contains_matches_the_job_name_or_the_file_name_as_written(self) -> None:
        walk = self.add("Sunset-Walk", started=iso(3), finished=iso(3))
        bare = self.store.executions.start(NewExecution(job_name="dawn", job_file="evening_1.yaml", mode="i2v", started_at=iso(2)))
        nested = self.store.executions.start(NewExecution(job_name="dusk", job_file="/walk/night.yaml", mode="i2v", started_at=iso(1)))
        page = self.store.executions.page
        self.assertEqual([row.id for row in page(name_contains="walk")], [walk])
        self.assertEqual([row.id for row in page(name_contains="EVENING")], [bare])
        self.assertEqual([row.id for row in page(name_contains="g_1")], [bare])
        # % and _ are literal, and the directory is not part of the file name.
        self.assertEqual(page(name_contains="t_w"), [])
        self.assertEqual(page(name_contains="%"), [])
        self.assertEqual([row.id for row in page(name_contains="night")], [nested])
        self.assertEqual(page(name_contains="walk/"), [])

    def test_executions_are_read_by_id(self) -> None:
        done = self.add("done", started=iso(2), finished=iso(2))
        running = self.add("running", started=iso(1))
        rows = {row.id: row for row in self.store.executions.by_ids([running, done, 999], running_as_interrupted=True)}
        self.assertEqual(sorted(rows), sorted([done, running]))
        self.assertEqual((rows[done].status, rows[running].status, rows[done].settings), ("succeeded", "interrupted", ExecutionSettings(output_directory="/out")))
        self.assertEqual(self.store.executions.by_ids([]), [])

    def test_succeeded_runs_are_counted_with_each_execution(self) -> None:
        done = self.add("done", started=iso(1), finished=iso(1), runs=3)
        running = self.add("running", started=iso(1), runs=2)
        self.store.executions.finish_run(done, 2, status="failed", exit_code=1, seconds=1.0, output=None, last_frame=None)
        counted = {row.id: row.succeeded for row in self.store.executions.page()}
        self.assertEqual(counted, {done: 2, running: 0})
        self.assertEqual({row.id: row.succeeded for row in self.store.executions.by_ids([done, running])}, {done: 2, running: 0})
        self.assertEqual(self.stored(done).succeeded, 2)
        self.assertEqual(len(self.stored(done).succeeded_runs), 2)

    def test_the_sweep_closes_running_rows_and_their_runs_as_interrupted(self) -> None:
        crashed = self.add("crashed", started=iso(1))
        done = self.add("done", started=iso(1), finished=iso(1))
        self.assertEqual(self.store.sweep_interrupted(), 1)
        execution = self.stored(crashed)
        self.assertEqual((execution.status, execution.runs[0].status), ("interrupted", "interrupted"))
        self.assertIsNotNone(execution.recovered_at)
        self.assertIsNotNone(execution.finished_at)
        self.assertEqual(self.stored(done).status, "succeeded")
        self.assertIsNone(self.stored(done).recovered_at)

    def test_a_read_only_view_shows_running_as_interrupted_without_writing(self) -> None:
        crashed = self.add("crashed", started=iso(1))
        page = self.store.executions.page
        self.assertEqual(page(running_as_interrupted=True)[0].status, "interrupted")
        execution = self.store.executions.get(crashed, running_as_interrupted=True)
        assert execution is not None
        self.assertEqual(execution.runs[0].status, "interrupted")
        self.assertEqual([row.id for row in page(status="interrupted", running_as_interrupted=True)], [crashed])
        self.assertEqual(page(status="running", running_as_interrupted=True), [])
        self.assertEqual(self.stored(crashed).status, "running")

    def test_prune_removes_old_executions_with_their_runs_and_keeps_the_rest(self) -> None:
        old = self.add("old", started=iso(15.5), finished=iso(15), runs=2)
        recent = self.add("recent", started=iso(13.5), finished=iso(13), runs=2)
        running = self.add("running", started=iso(40))
        self.assertEqual(self.store.prune(), 1)
        self.assertIsNone(self.store.executions.get(old))
        self.assertEqual(len(self.stored(recent).runs), 2)
        self.assertEqual(self.stored(running).status, "running")
        remaining = self.store._database.connection().execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(remaining, 2 + 1)

    def test_prune_deletes_the_log_of_a_pruned_execution_only(self) -> None:
        directory = Path(self._temporary.name)
        logs = {name: directory / f"{name}.log" for name in ("old", "recent", "running")}
        manifest = directory / "old.json"
        for path in (*logs.values(), manifest):
            path.write_text("x")
        for name, (started, finished) in {"old": (iso(15.5), iso(15)), "recent": (iso(13.5), iso(13)), "running": (iso(40), None)}.items():
            execution_id = self.add(name, started=started, finished=finished)
            self.store._database.connection().execute("UPDATE executions SET log_path = ? WHERE id = ?", (str(logs[name]), execution_id)).connection.commit()
        self.store.prune()
        self.assertFalse(logs["old"].exists())
        self.assertTrue(logs["recent"].exists())
        self.assertTrue(logs["running"].exists())
        self.assertTrue(manifest.exists())

    def test_prune_ignores_a_missing_log_and_a_path_that_is_not_a_log(self) -> None:
        other = Path(self._temporary.name) / "keep.png"
        other.write_text("x")
        for path in (Path(self._temporary.name) / "gone.log", other):
            execution_id = self.add(path.name, started=iso(15.5), finished=iso(15))
            self.store._database.connection().execute("UPDATE executions SET log_path = ? WHERE id = ?", (str(path), execution_id)).connection.commit()
        self.assertEqual(self.store.prune(), 2)
        self.assertTrue(other.exists())

    def test_prune_compares_instants_across_offsets(self) -> None:
        # The cutoff is 2026-09-11T12:00Z. The first instant is 11:00Z (text looks later, so text comparison would keep it); the second is 22:00Z (text looks earlier, so it would prune it).
        expired = self.add("expired", started=iso(15), finished="2026-09-12T01:00:00+14:00")
        kept = self.add("kept", started=iso(15), finished="2026-09-11T08:00:-14:00".replace(":-14:00", "-14:00"))
        self.store.prune()
        self.assertIsNone(self.store.executions.get(expired))
        self.assertIsNotNone(self.store.executions.get(kept))

    def test_zero_retention_keeps_everything_and_opening_prunes(self) -> None:
        self.add("ancient", started=iso(4000), finished=iso(4000))
        self.assertEqual(self.open(retention_days=0).prune(), 0)
        self.assertEqual(len(self.store.executions.page()), 1)
        self.open(retention_days=14)
        self.assertEqual(self.store.executions.page(), [])

    def test_only_a_run_open_prunes(self) -> None:
        self.add("ancient", started=iso(4000), finished=iso(4000))
        for mode in (StoreMode.WRITE, StoreMode.BROWSE):
            self.open(mode=mode)
            self.assertEqual(len(self.store.executions.page()), 1, mode)
        self.open(mode=StoreMode.RUN)
        self.assertEqual(self.store.executions.page(), [])

    def test_a_swept_row_is_pruned_like_any_other(self) -> None:
        self.add("crashed", started=iso(30))
        self.store.sweep_interrupted()
        later = self.open(now=NOW + timedelta(days=15))
        self.assertEqual(later.executions.page(), [])

    def test_a_failed_write_leaves_no_partial_row(self) -> None:
        good = NewRun(pair="p", positive="x", started_at=iso(1))
        bad = NewRun(pair=None, positive="x", started_at=iso(1))  # pyright: ignore[reportArgumentType]  (a NOT NULL column, to make the insert fail)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.executions.import_execution(NewExecution(job_name="n", job_file="j", mode="i2v", started_at=iso(1), manifest_path="/m.json"), [good, bad])
        self.assertEqual(self.store.executions.page(), [])
        self.assertFalse(self.store.executions.has_manifest("/m.json"))


class IdTests(StoreCase):
    """Milestone 10: execution numbers and job numbers, which only go up, and the remembered settings."""

    def number(self, execution_id: int) -> int:
        return self.stored(execution_id).execution_number

    def test_a_version_2_database_numbers_its_executions_by_start_time(self) -> None:
        path = self.path.with_name("v2.db")
        connection = sqlite3.connect(path)
        for schema in (SCHEMA_V1, SCHEMA_V2):
            for statement in schema.split(";\n"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        # Row order is not start order: an imported phase 1 execution started before the rows written first.
        for name, started in (("second", 200.0), ("third", 300.0), ("first", 100.0), ("tie", 300.0)):
            connection.execute("INSERT INTO executions (job_name, job_file, mode, status, started_at, started_epoch) VALUES (?, 'x.yaml', 'i2v', 'succeeded', '2026-09-01T09:00:00+00:00', ?)", (name, started))
        connection.commit()
        connection.close()
        store = self.open(mode=StoreMode.WRITE, path=path)
        numbers = {row.job_name: row.execution_number for row in store.executions.page()}
        # By start time, ties by row order; the next execution continues after them.
        self.assertEqual(numbers, {"first": 1, "second": 2, "third": 3, "tie": 4})
        self.assertEqual(store.executions.reserve_number(), 5)

    def test_numbers_are_never_given_twice_after_pruning_or_a_reservation(self) -> None:
        executions = self.store.executions
        old = self.add("old", started=iso(20), finished=iso(20))
        reserved = executions.reserve_number()
        recent = executions.start(NewExecution(execution_number=reserved, job_name="recent", job_file="r.yaml", mode="i2v", started_at=iso(1)))
        self.assertEqual((self.number(old), self.number(recent)), (1, 2))
        self.assertEqual(self.store.prune(), 1)
        # A reservation whose job never started leaves a gap; the pruned 1 is not given again either.
        self.assertEqual(executions.reserve_number(), 3)
        imported, imported_number = executions.import_execution(NewExecution(job_name="imported", job_file="i.yaml", mode="i2v", status="succeeded", started_at=iso(30), finished_at=iso(1)), [])
        self.assertEqual((self.number(imported), imported_number), (4, 4))
        self.assertEqual((executions.row_of(4), executions.row_of(1), executions.number_of(recent)), (imported, None, 2))

    def test_a_job_file_name_keeps_its_number_and_a_retired_number_is_never_reused(self) -> None:
        job_ids = self.store.job_ids
        self.assertEqual(job_ids.assign(["walk.yaml", "wave.yaml"], iso(0)), {"walk.yaml": 1, "wave.yaml": 2})
        # Deleted: wave is retired; a new name takes the next number, not wave's.
        self.assertEqual(job_ids.assign(["walk.yaml", "run.yaml"], iso(0)), {"walk.yaml": 1, "run.yaml": 3})
        self.assertEqual(job_ids.lookup(2), ("wave.yaml", False))
        # Restored under the same name: its number comes back.
        self.assertEqual(job_ids.assign(["wave.yaml", "walk.yaml"], iso(0)), {"wave.yaml": 2, "walk.yaml": 1})
        self.assertEqual((job_ids.lookup(2), job_ids.lookup(3), job_ids.lookup(9)), (("wave.yaml", True), ("run.yaml", False), None))
        # A listing that changes nothing writes nothing.
        with mock.patch.object(self.store._database, "transaction", side_effect=AssertionError("no write expected")):
            self.assertEqual(job_ids.assign(["walk.yaml", "wave.yaml"], iso(0)), {"walk.yaml": 1, "wave.yaml": 2})
        # A second process sees the same numbers.
        self.assertEqual(self.open().job_ids.assign(["walk.yaml", "new.yaml"], iso(0)), {"walk.yaml": 1, "new.yaml": 4})

    def test_a_setting_is_kept_and_replaced(self) -> None:
        self.assertIsNone(self.store.settings.get("job_definition.sort"))
        self.store.settings.set("job_definition.sort", "name asc")
        self.store.settings.set("job_definition.sort", "id desc")
        self.store.close()
        self.assertEqual(self.open().settings.get("job_definition.sort"), "id desc")


class MilestoneTenDatabaseTests(StoreCase):
    """A database written before this refactor (schema 3, settings with null keys) opens, reads, and records as it is,
    migrating forward to the current schema."""

    def test_it_opens_reads_and_records_migrating_forward(self) -> None:
        path = self.path.with_name("m10.db")
        connection = sqlite3.connect(path)
        for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3):
            for statement in schema.split(";\n"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("PRAGMA user_version = 3")
        settings = '{"input": null, "output_directory": "/out", "cooldown_seconds": null, "cooldown_source": "default", "cooldown": {"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 3600.0}, "config_file": "base.yaml", "config_override": {}, "input_resize": null}'
        connection.execute("INSERT INTO executions (job_name, job_file, mode, status, started_at, started_epoch, execution_number, settings) VALUES ('walk', 'walk.yaml', 'i2v', 'succeeded', '2026-09-24T10:00:00+00:00', 1, 7, ?)", (settings,))
        connection.execute("UPDATE counters SET value = 7 WHERE name = 'execution'")
        connection.commit()
        connection.close()
        for mode in (StoreMode.BROWSE, StoreMode.WRITE, StoreMode.RUN):
            store = self.open(mode=mode, path=path, retention_days=0)
            [row] = store.executions.page()
            self.assertEqual((row.label, row.settings.output_directory, row.settings.cooldown, row.settings.input_resize), ("E0007", "/out", {"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 3600.0}, None))
            self.assertEqual(store._database.connection().execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        recorded = store.executions.start(NewExecution(job_name="next", job_file="next.yaml", mode="i2v", started_at=iso(0)))
        self.assertEqual(store.executions.number_of(recorded), 8)
