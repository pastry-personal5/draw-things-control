"""Tests for the audit log repository."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from draw_things_control.core.clock import local_timestamp
from draw_things_control.state.store import Store, StoreMode

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


class AuditRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = Store.open(Path(self._temporary.name) / "dtc.db", mode=StoreMode.WRITE, clock=lambda: NOW)
        self.addCleanup(self.store.close)

    def test_a_recorded_entry_round_trips(self) -> None:
        row = self.store.audit.record(action="submit", target="Q0001", outcome="ok", caller="cli", at=local_timestamp(NOW))
        self.assertEqual((row.action, row.target, row.outcome, row.caller), ("submit", "Q0001", "ok", "cli"))
        self.assertEqual(row.at, "2026-09-28T12:00:00+00:00")

    def test_a_refused_request_is_recorded_with_no_target(self) -> None:
        row = self.store.audit.record(action="submit", target=None, outcome="invalid_input", caller="mcp", at=local_timestamp(NOW))
        self.assertIsNone(row.target)
        self.assertEqual(row.outcome, "invalid_input")

    def test_paging_is_newest_first(self) -> None:
        for index, minutes in enumerate((0, 1, 2)):
            self.store.audit.record(action="submit", target=f"Q000{index}", outcome="ok", caller="cli", at=local_timestamp(NOW + timedelta(minutes=minutes)))
        self.assertEqual([row.target for row in self.store.audit.page(limit=200)], ["Q0002", "Q0001", "Q0000"])
        self.assertEqual([row.target for row in self.store.audit.page(limit=1, offset=1)], ["Q0001"])
        self.assertEqual(self.store.audit.page(limit=200, offset=99), [])

    def test_history_retention_never_prunes_the_audit_log(self) -> None:
        self.store.audit.record(action="submit", target="Q0001", outcome="ok", caller="cli", at=local_timestamp(NOW - timedelta(days=4000)))
        pruned = self.open_pruning()
        self.assertEqual(len(pruned.audit.page(limit=200)), 1)

    def open_pruning(self) -> Store:
        store = Store.open(self.store.path, mode=StoreMode.RUN, retention_days=14, clock=lambda: NOW)
        self.addCleanup(store.close)
        return store


if __name__ == "__main__":
    unittest.main()
