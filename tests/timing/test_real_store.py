"""The owner-host comparison on the real history: the frozen estimator and the forecast scored by one metrics function.

Opt-in, because the real history is small, moves under the test, and is the owner's. Name a state database and run it::

    FRAMECONTROL_REPLAY_STATE=state/framecontrol.db uv run pytest tests/timing/test_real_store.py -s

The database is copied through SQLite's backup (a read-only connection, so a write-ahead log is included) and the copy is
what is read: nothing is written to the one named. The tables are printed and nothing is asserted about the numbers, which
are measured once and recorded in ``docs/research/time-estimation.md``, with each cell's run count and its interval."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from framecontrol.services.timing_history import load_executions
from framecontrol.services.timing_replay import BUILTIN_ESTIMATORS, replay_leave_one_out, report_lines
from framecontrol.state.store import Store, StoreMode
from tests.timing.frozen_estimator import FrozenReplayEstimator

ENVIRONMENT_VARIABLE = "FRAMECONTROL_REPLAY_STATE"


@unittest.skipUnless(os.environ.get(ENVIRONMENT_VARIABLE), f"set {ENVIRONMENT_VARIABLE} to a state database to score it")
class RealHistoryTests(unittest.TestCase):
    def test_the_frozen_estimator_and_the_forecast_are_scored_on_the_real_runs_by_one_metrics_function(self) -> None:
        source = Path(os.environ[ENVIRONMENT_VARIABLE]).expanduser()
        self.assertTrue(source.is_file(), f"{source} is not a file")
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "framecontrol.db"
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as read, sqlite3.connect(copy) as target:
                read.backup(target)
            store = Store.open(copy, mode=StoreMode.BROWSE)
            try:
                executions, skipped = load_executions(store)
            finally:
                store.close()
        runs = sum(len(execution.runs) for execution in executions)
        self.assertGreater(runs, 0, "the database holds no run with complete timing")
        for name, factory in (("frozen estimator", FrozenReplayEstimator), ("forecast", BUILTIN_ESTIMATORS["forecast"])):
            report = replay_leave_one_out(executions, factory)
            sys.stderr.write("\n" + "\n".join(report_lines(report, name, len(executions), runs)) + "\n")
            self.assertTrue(report.observations)
        if skipped:
            sys.stderr.write(f"\n{skipped} rows could not be read\n")
