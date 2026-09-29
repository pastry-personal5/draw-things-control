"""Tests for the Execution History widget's row text."""

from __future__ import annotations

import unittest

from draw_things_control.tui.text.history import delete_summary, history_cells
from tests.fixtures import execution_row


class HistoryCellsResumeTests(unittest.TestCase):
    def test_a_plain_executions_runs_cell_is_its_own_succeeded_count(self) -> None:
        cells = history_cells(execution_row(total_runs=3, succeeded=2))
        self.assertEqual(str(cells[-1]), "2/3")

    def test_a_resumed_executions_runs_cell_counts_the_whole_chain(self) -> None:
        # Resumed at run 4 of 7 (runs 1-3 already succeeded, in an earlier execution); this leg's own 2 succeeded
        # (runs 4 and 5) add to a chain total of 5, not 2.
        cells = history_cells(execution_row(total_runs=7, succeeded=2, first_run=4))
        self.assertEqual(str(cells[-1]), "5/7")


class DeleteSummaryTests(unittest.TestCase):
    def test_the_dialog_names_the_execution_with_its_chain_runs_and_full_start(self) -> None:
        row = execution_row(execution_number=12, status="failed", total_runs=7, succeeded=2, first_run=2, started_at="2026-09-28T14:03:00")
        self.assertEqual(delete_summary(row), "E0012 (walk, failed, 3/7 runs, 2026-09-28 14:03)")

    def test_a_marked_row_shows_its_mark(self) -> None:
        self.assertEqual([str(history_cells(execution_row(), marked=marked)[0]) for marked in (True, False)], ["*E0001", "E0001"])
