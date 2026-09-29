"""Tests for QueueReader: the Queue widget's read-only fallback while the server is down."""

from __future__ import annotations

from unittest import mock

from draw_things_control.core.errors import NotFoundError, StateUnavailableError
from draw_things_control.services import queue_hold
from draw_things_control.services.queue_hold import HOLD_KEY, HoldState
from draw_things_control.services.queue_reader import NO_QUEUE, QueueReader
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.execution_rows import ExecutionSettings
from draw_things_control.state.queue import NewQueueEntry, QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase


class QueueReaderTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = StoreProvider(self.paths, 14)
        self.addCleanup(self.provider.close)
        self.reader = QueueReader(self.paths, self.provider, finished_limit=2)

    def submit(self, *, total_runs: int = 1) -> int:
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        try:
            new = NewQueueEntry(job_path="/jobs/walk.yaml", job_text="name: walk\n", config_file="base.yaml", config_text="model: m\n", input_directory="/in", output_directory="/out", cooldown_default=None, settings=ExecutionSettings(output_directory="/out"), submitted_at="2026-09-29T09:00:00+00:00", total_runs=total_runs)
            return store.queue.submit(new).id
        finally:
            store.close()

    def test_no_database_yet_reports_no_queue_entries_and_creates_nothing(self) -> None:
        snapshot = self.reader.snapshot()
        self.assertEqual((snapshot.active, snapshot.finished, snapshot.message), ([], [], NO_QUEUE))
        self.assertFalse(self.paths.database.exists())

    def test_active_and_finished_entries_are_both_read(self) -> None:
        active_id = self.submit(total_runs=2)
        finished_id = self.submit(total_runs=1)
        store = Store.open(self.paths.database)
        try:
            store.queue.finish(finished_id, state=QueueState.SUCCEEDED, finished_at="2026-09-29T09:05:00+00:00")
        finally:
            store.close()
        snapshot = self.reader.snapshot()
        self.assertEqual([row.id for row in snapshot.active], [active_id])
        self.assertEqual([row.id for row in snapshot.finished], [finished_id])
        self.assertIsNone(snapshot.message)

    def test_the_saved_hold_is_read_beside_the_entries_and_a_parked_entry_is_finished(self) -> None:
        parked_id = self.submit(total_runs=3)
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        try:
            store.queue.finish(parked_id, state=QueueState.PARKED, finished_at="2026-09-29T09:05:00+00:00")
        finally:
            store.close()
        self.assertEqual(self.reader.snapshot().hold, HoldState())
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        try:
            store.settings.set(HOLD_KEY, '{"since": "2026-09-29T09:05:00+00:00", "by": "Q0001"}')
        finally:
            store.close()
        snapshot = self.reader.snapshot()
        self.assertEqual([row.id for row in snapshot.finished], [parked_id])
        self.assertEqual(snapshot.hold, HoldState(held=True, since="2026-09-29T09:05:00+00:00", by="Q0001"))

    def test_a_store_that_cannot_be_read_leaves_the_hold_unknown(self) -> None:
        self.submit()
        with mock.patch.object(StoreProvider, "get", side_effect=StateUnavailableError("database is locked")):
            snapshot = self.reader.snapshot()
        self.assertEqual((snapshot.message, snapshot.hold), ("database is locked", None))

    def test_a_damaged_saved_hold_counts_as_held_and_is_warned_about_once(self) -> None:
        self.submit()
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        try:
            store.settings.set(HOLD_KEY, "{not json")
        finally:
            store.close()
        with mock.patch.object(queue_hold.logger, "warning") as warning:
            holds = [self.reader.snapshot().hold for _ in range(3)]
        self.assertEqual(holds, [HoldState(held=True)] * 3)
        self.assertEqual(warning.call_count, 1)

    def test_by_number_finds_an_entry_or_raises_not_found(self) -> None:
        self.submit()
        store = Store.open(self.paths.database)
        try:
            [row] = store.queue.list_active()
        finally:
            store.close()
        self.assertEqual(self.reader.by_number(row.queue_number).id, row.id)
        with self.assertRaises(NotFoundError):
            self.reader.by_number(9999)

    def test_by_number_with_no_database_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.reader.by_number(1)
