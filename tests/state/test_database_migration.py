"""The one-time state filename migration preserves committed WAL data and refuses ambiguous startup."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from framechain.core.paths import ProjectPaths
from framechain.core.run_lock import RunLock
from framechain.services.store_provider import StoreProvider
from framechain.state.database import StateError
from framechain.state.database_migration import migrate_legacy_database
from framechain.state.executions import ExecutionSettings, NewExecution
from framechain.state.queue import NewQueueEntry
from framechain.state.store import Store, StoreMode


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ProjectPaths(Path(temporary.name))
        self.old = self.paths.state / "dtc.db"
        self.new = self.paths.database

    def test_populated_wal_history_and_queue_survive(self) -> None:
        old_store = Store.open(self.old, mode=StoreMode.WRITE)
        self.addCleanup(old_store.close)
        started = "2026-10-13T00:00:00+00:00"
        old_store.executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at=started, settings=ExecutionSettings(output_directory="/out")))
        old_store.queue.submit(NewQueueEntry(job_path="/jobs/walk.yaml", job_text="name: walk\n", config_file="base.yaml", config_text="model: m\n", input_directory="/in", output_directory="/out", cooldown_default=None, settings=ExecutionSettings(output_directory="/out"), submitted_at=started, total_runs=1))
        self.assertTrue(self.old.with_name("dtc.db-wal").exists())
        migrated = Store.open(self.new, mode=StoreMode.BROWSE)
        self.addCleanup(migrated.close)
        execution = migrated.executions.by_number(1)
        assert execution is not None
        self.assertEqual(execution.job_name, "walk")
        self.assertEqual(migrated.queue.list()[0].label, "Q0001")
        self.assertFalse(self.old.exists())
        self.assertFalse(self.old.with_name("dtc.db-wal").exists())
        self.assertEqual(sqlite3.connect(self.new).execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_held_run_lock_refuses_without_copying(self) -> None:
        store = Store.open(self.old, mode=StoreMode.WRITE)
        store.close()
        with RunLock("serve", directory=self.paths.state):
            with self.assertRaisesRegex(StateError, "Stop the running server or job"):
                Store.open(self.new, mode=StoreMode.BROWSE)
        self.assertTrue(self.old.exists())
        self.assertFalse(self.new.exists())

    def test_both_files_after_interruption_require_manual_choice(self) -> None:
        store = Store.open(self.old, mode=StoreMode.WRITE)
        store.close()
        self.new.write_bytes(b"incomplete backup")
        with self.assertRaisesRegex(StateError, "move the unwanted one aside"):
            Store.open(self.new, mode=StoreMode.WRITE)
        self.assertTrue(self.old.exists())
        self.assertEqual(self.new.read_bytes(), b"incomplete backup")

    def test_fresh_browse_creates_no_state(self) -> None:
        provider = StoreProvider(self.paths, 14)
        self.assertIsNone(provider.get(create=False))
        provider.close()
        self.assertFalse(self.paths.state.exists())

    def test_project_path_with_uri_punctuation_preserves_legacy_data(self) -> None:
        for punctuation in ("?", "#"):
            with self.subTest(punctuation=punctuation):
                paths = ProjectPaths(self.paths.root / f"project{punctuation}name")
                old = paths.state / "dtc.db"
                old.parent.mkdir(parents=True)
                with sqlite3.connect(old) as connection:
                    connection.execute("CREATE TABLE sentinel (value TEXT)")
                    connection.execute("INSERT INTO sentinel VALUES (?)", (punctuation,))
                migrate_legacy_database(paths.database)
                with sqlite3.connect(paths.database) as connection:
                    self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone(), (punctuation,))
                self.assertFalse(old.exists())

    def test_relative_database_path_preserves_legacy_data(self) -> None:
        self.old.parent.mkdir(parents=True)
        with sqlite3.connect(self.old) as connection:
            connection.execute("CREATE TABLE sentinel (value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('retained')")
        migrate_legacy_database(Path(os.path.relpath(self.new)))
        with sqlite3.connect(self.new) as connection:
            self.assertEqual(connection.execute("SELECT value FROM sentinel").fetchone(), ("retained",))
        self.assertFalse(self.old.exists())
