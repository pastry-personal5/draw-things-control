"""Tests for the SQLite state store."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from draw_things_control.state import store as store_module
from draw_things_control.state.store import SCHEMA_VERSION, StateError, Store

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def iso(days_ago: float, offset: str = "+00:00") -> str:
    return (NOW - timedelta(days=days_ago)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + offset


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "dtc.db"
        self.store = self.open()

    def open(self, retention_days: int = 14) -> Store:
        store = Store(self.path, retention_days=retention_days, clock=lambda: NOW)
        self.addCleanup(store.close)
        return store

    def add(self, name: str, *, started: str, finished: str | None = None, status: str = "succeeded", runs: int = 1) -> int:
        execution_id = self.store.start_execution(job_name=name, job_file=f"{name}.yaml", mode="i2v", started_at=started, settings={"output_directory": "/out"})
        for number in range(1, runs + 1):
            self.store.start_run(execution_id, number, pair="p", positive="text", started_at=started, command=["draw-things-cli", "--prompt", "text"])
            if finished is not None:
                self.store.finish_run(execution_id, number, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
        if finished is not None:
            self.store.finish_execution(execution_id, status=status, exit_code=0, signal=None, finished_at=finished)
        return execution_id

    def test_a_new_database_uses_wal_foreign_keys_and_the_current_schema(self) -> None:
        connection = self.store._connection()
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_reopening_keeps_the_data(self) -> None:
        execution_id = self.add("first", started=iso(1), finished=iso(1))
        self.store.close()
        again = self.open()
        self.assertEqual(again.get_execution(execution_id)["job_name"], "first")

    def test_a_database_with_a_newer_schema_is_refused(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        with self.assertRaisesRegex(StateError, "newer version"):
            Store(self.path)

    def test_a_filesystem_without_wal_is_refused(self) -> None:
        real_connect = sqlite3.connect

        class NoWal:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection
                self.row_factory = None

            def execute(self, sql: str, *arguments: object):
                if "journal_mode" in sql:
                    return self.connection.execute("PRAGMA journal_mode = DELETE")
                return self.connection.execute(sql, *arguments)

            def close(self) -> None:
                self.connection.close()

        with mock.patch.object(store_module.sqlite3, "connect", lambda *a, **k: NoWal(real_connect(*a, **k))):
            with self.assertRaisesRegex(StateError, "does not support SQLite WAL"):
                Store(Path(self._temporary.name) / "other.db")

    def test_an_execution_round_trips_with_its_runs_and_settings(self) -> None:
        execution_id = self.add("walk", started=iso(1), finished=iso(1), runs=2)
        execution = self.store.get_execution(execution_id)
        self.assertEqual((execution["status"], execution["settings"], execution["job_file"]), ("succeeded", {"output_directory": "/out"}, "walk.yaml"))
        self.assertEqual([(run["number"], run["status"], run["command"]) for run in execution["runs"]], [(1, "succeeded", ["draw-things-cli", "--prompt", "text"]), (2, "succeeded", ["draw-things-cli", "--prompt", "text"])])
        self.assertIsNone(self.store.get_execution(999))

    def test_listing_is_newest_first_by_time_not_by_text_and_filters_and_pages(self) -> None:
        # 10:00+02:00 is 08:00 UTC, earlier than 09:30 UTC, though its text sorts later.
        older = self.add("older", started="2026-09-01T10:00:00+02:00", finished="2026-09-01T10:30:00+02:00")
        newer = self.add("newer", started="2026-09-01T09:30:00+00:00", finished="2026-09-01T10:00:00+00:00", status="failed")
        self.assertEqual([row["id"] for row in self.store.list_executions()], [newer, older])
        self.assertEqual([row["id"] for row in self.store.list_executions(status="failed")], [newer])
        self.assertEqual([row["id"] for row in self.store.list_executions(name="older")], [older])
        self.assertEqual([row["id"] for row in self.store.list_executions(limit=1, offset=1)], [older])

    def test_the_sweep_closes_running_rows_and_their_runs_as_interrupted(self) -> None:
        crashed = self.add("crashed", started=iso(1))
        done = self.add("done", started=iso(1), finished=iso(1))
        self.assertEqual(self.store.sweep_interrupted(), 1)
        execution = self.store.get_execution(crashed)
        self.assertEqual((execution["status"], execution["runs"][0]["status"]), ("interrupted", "interrupted"))
        self.assertIsNotNone(execution["recovered_at"])
        self.assertIsNotNone(execution["finished_at"])
        self.assertEqual(self.store.get_execution(done)["status"], "succeeded")
        self.assertIsNone(self.store.get_execution(done)["recovered_at"])

    def test_a_read_only_view_shows_running_as_interrupted_without_writing(self) -> None:
        crashed = self.add("crashed", started=iso(1))
        self.assertEqual(self.store.list_executions(running_as_interrupted=True)[0]["status"], "interrupted")
        self.assertEqual(self.store.get_execution(crashed, running_as_interrupted=True)["runs"][0]["status"], "interrupted")
        self.assertEqual([row["id"] for row in self.store.list_executions(status="interrupted", running_as_interrupted=True)], [crashed])
        self.assertEqual(self.store.list_executions(status="running", running_as_interrupted=True), [])
        self.assertEqual(self.store.get_execution(crashed)["status"], "running")

    def test_prune_removes_old_executions_with_their_runs_and_keeps_the_rest(self) -> None:
        old = self.add("old", started=iso(15.5), finished=iso(15), runs=2)
        recent = self.add("recent", started=iso(13.5), finished=iso(13), runs=2)
        running = self.add("running", started=iso(40))
        self.assertEqual(self.store.prune(), 1)
        self.assertIsNone(self.store.get_execution(old))
        self.assertEqual(len(self.store.get_execution(recent)["runs"]), 2)
        self.assertEqual(self.store.get_execution(running)["status"], "running")
        remaining = self.store._connection().execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        self.assertEqual(remaining, 2 + 1)

    def test_prune_compares_instants_across_offsets(self) -> None:
        # The cutoff is 2026-09-11T12:00Z. The first instant is 11:00Z (text looks later, so text comparison would keep it); the second is 22:00Z (text looks earlier, so it would prune it).
        expired = self.add("expired", started=iso(15), finished="2026-09-12T01:00:00+14:00")
        kept = self.add("kept", started=iso(15), finished="2026-09-11T08:00:00-14:00")
        self.store.prune()
        self.assertIsNone(self.store.get_execution(expired))
        self.assertIsNotNone(self.store.get_execution(kept))

    def test_zero_retention_keeps_everything_and_opening_prunes(self) -> None:
        self.add("ancient", started=iso(4000), finished=iso(4000))
        self.assertEqual(self.open(retention_days=0).prune(), 0)
        self.assertEqual(len(self.store.list_executions()), 1)
        self.open(retention_days=14)
        self.assertEqual(self.store.list_executions(), [])

    def test_a_swept_row_is_pruned_like_any_other(self) -> None:
        self.add("crashed", started=iso(30))
        self.store.sweep_interrupted()
        later = Store(self.path, retention_days=14, clock=lambda: NOW + timedelta(days=15))
        self.addCleanup(later.close)
        self.assertEqual(later.list_executions(), [])

    def test_a_failed_write_leaves_no_partial_row(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.import_execution({"job_name": "n", "job_file": "j", "mode": "i2v", "started_at": iso(1), "manifest_path": "/m.json"}, [{"pair": "p", "positive": "x", "started_at": iso(1)}, {"pair": None, "positive": "x", "started_at": iso(1)}])
        self.assertEqual(self.store.list_executions(), [])
        self.assertFalse(self.store.has_manifest("/m.json"))

    def test_a_failed_commit_is_rolled_back_so_later_writes_still_work(self) -> None:
        real = self.store._connection()

        class FailingCommit:
            def __init__(self) -> None:
                self.fail = True

            def __getattr__(self, name: str):
                return getattr(real, name)

            def execute(self, sql: str, *arguments: object):
                if sql == "COMMIT" and self.fail:
                    self.fail = False
                    raise sqlite3.OperationalError("database is locked")
                return real.execute(sql, *arguments)

        self.store._local.connection = FailingCommit()
        with self.assertRaises(sqlite3.OperationalError):
            self.add("first", started=iso(1))
        # The transaction is gone, so the next write works instead of failing to begin.
        self.add("second", started=iso(1))
        self.assertEqual([row["job_name"] for row in self.store.list_executions()], ["second"])

    def test_a_store_that_fails_to_open_closes_its_connection(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        closed: list[bool] = []
        with mock.patch.object(Store, "close", autospec=True, side_effect=lambda store: closed.append(True)):
            with self.assertRaises(StateError):
                Store(self.path)
        self.assertEqual(closed, [True])
