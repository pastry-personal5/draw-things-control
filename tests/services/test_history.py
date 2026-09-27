"""Tests for reading the execution history: a page, one execution, and what failure looks like."""

from __future__ import annotations

import unittest
from unittest import mock

from draw_things_control.core.errors import NotFoundError, StateUnavailableError
from draw_things_control.core.run_lock import RunLock
from draw_things_control.services.history import NO_HISTORY, HistoryFilter, HistoryReader
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.store import Store, StoreMode
from draw_things_control.tui.reader import PaneHistory
from tests.fixtures import JobTestCase

STARTED = "2026-09-24T10:00:00+00:00"


class HistoryReaderTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = StoreProvider(self.paths, 14)
        self.addCleanup(self.provider.close)
        self.reader = HistoryReader(self.paths, self.provider)

    def add(self, name: str, *, minutes: int = 0, status: str = "succeeded") -> int:
        self.paths.state.mkdir(exist_ok=True)
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(store.close)
        started = f"2026-09-24T10:{minutes:02d}:00+00:00"
        execution_id = store.executions.start(NewExecution(job_name=name, job_file=f"/jobs/{name}.yaml", mode="i2v", started_at=started, settings=ExecutionSettings(output_directory="/out")))
        store.executions.start_run(execution_id, 1, NewRun(pair="p", positive="text", started_at=started, output="a.mov"))
        if status != "running":
            store.executions.finish_run(execution_id, 1, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
            store.executions.finish(execution_id, status=status, exit_code=0, signal=None, finished_at=started)
        return execution_id

    def test_without_a_database_a_page_says_so_and_nothing_is_created(self) -> None:
        page = self.reader.page(HistoryFilter(), 0)
        self.assertEqual((page.rows, page.complete, page.message), ([], True, NO_HISTORY))
        for read in (lambda: self.reader.rows([1]), lambda: self.reader.newest_id(HistoryFilter()), lambda: self.reader.execution(1), lambda: self.reader.numbered(1)):
            with self.assertRaisesRegex(NotFoundError, NO_HISTORY):
                read()
        self.assertFalse(self.paths.database.exists())

    def test_a_page_is_newest_first_filtered_and_says_when_nothing_matches(self) -> None:
        old = self.add("walk", minutes=1)
        new = self.add("wave", minutes=2, status="failed")
        self.assertEqual([row.id for row in self.reader.page(HistoryFilter(), 0).rows], [new, old])
        self.assertEqual([row.id for row in self.reader.page(HistoryFilter(status="failed"), 0).rows], [new])
        self.assertEqual([row.id for row in self.reader.page(HistoryFilter(name="wal"), 0).rows], [old])
        page = self.reader.page(HistoryFilter(name="nothing"), 0)
        self.assertEqual((page.rows, page.message), ([], "No executions match the filter"))
        first = self.reader.page(HistoryFilter(), 0, limit=1)
        self.assertEqual(([row.id for row in first.rows], first.complete), ([new], False))
        self.assertEqual([row.id for row in self.reader.page(HistoryFilter(), 1, limit=1).rows], [old])
        self.assertEqual(self.reader.newest_id(HistoryFilter()), new)
        self.assertIsNone(self.reader.newest_id(HistoryFilter(name="nothing")))

    def test_an_execution_is_read_by_row_or_by_number_with_its_runs(self) -> None:
        execution_id = self.add("walk")
        by_row = self.reader.execution(execution_id)
        by_number = self.reader.numbered(by_row.execution_number)
        self.assertEqual((by_row.label, [run.number for run in by_number.runs], by_row.succeeded), ("E0001", [1], 1))
        with self.assertRaisesRegex(NotFoundError, "That execution is no longer in the history"):
            self.reader.execution(999)
        with self.assertRaisesRegex(NotFoundError, "No execution E0099"):
            self.reader.numbered(99)
        self.assertEqual([row.id for row in self.reader.rows([execution_id, 999])], [execution_id])

    def test_a_running_row_reads_as_interrupted_only_while_nothing_holds_the_lock(self) -> None:
        execution_id = self.add("crashed", status="running")
        self.assertTrue(self.reader.lock_is_free())
        self.assertEqual(self.reader.execution(execution_id).status, "interrupted")
        with RunLock("other", directory=self.paths.state):
            self.assertFalse(self.reader.lock_is_free())
            self.assertEqual(self.reader.execution(execution_id).status, "running")

    def test_a_store_that_cannot_be_read_raises_a_state_error_naming_the_database(self) -> None:
        self.add("walk")
        store = self.provider.get(create=False)
        assert store is not None
        with mock.patch.object(store.executions, "page", side_effect=ValueError("bad JSON")):
            with self.assertRaisesRegex(StateUnavailableError, r"Cannot read the state database .*dtc\.db: bad JSON"):
                self.reader.page(HistoryFilter(), 0)


class PaneHistoryTests(unittest.TestCase):
    def test_every_failure_becomes_the_text_a_pane_shows(self) -> None:
        reader = mock.Mock(spec=HistoryReader)
        for name in ("page", "rows", "newest_id", "execution", "numbered"):
            getattr(reader, name).side_effect = NotFoundError("gone")
        pane = PaneHistory(reader)
        page = pane.page(HistoryFilter(), 3)
        self.assertEqual((page.offset, page.rows, page.message), (3, [], "gone"))
        self.assertEqual([pane.rows([1]), pane.newest_id(HistoryFilter()), pane.execution(1), pane.numbered(1)], ["gone"] * 4)

    def test_success_passes_through_and_an_unexpected_error_is_not_swallowed(self) -> None:
        reader = mock.Mock(spec=HistoryReader)
        reader.newest_id.return_value = 7
        pane = PaneHistory(reader)
        self.assertEqual(pane.newest_id(HistoryFilter()), 7)
        reader.execution.side_effect = RuntimeError("bug")
        with self.assertRaises(RuntimeError):
            pane.execution(1)
