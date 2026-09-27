"""Tests for the store a browsing front end shares: opened when needed, kept, and never created by a read."""

from draw_things_control.core.errors import StateUnavailableError
from draw_things_control.services.history import HistoryFilter, HistoryReader
from draw_things_control.services.store_provider import StoreProvider
from draw_things_control.state.database import StateError
from draw_things_control.tui.job_sort import DEFAULT_SORT, SortPreference
from tests.fixtures import JobTestCase


class StoreProviderTests(JobTestCase):
    def test_a_read_finds_no_store_and_creates_no_file(self) -> None:
        provider = StoreProvider(self.paths, 14)
        self.addCleanup(provider.close)
        self.assertIsNone(provider.get(create=False))
        self.assertFalse(self.paths.database.exists())

    def test_a_write_creates_the_database_and_the_same_store_is_kept(self) -> None:
        provider = StoreProvider(self.paths, 14)
        self.addCleanup(provider.close)
        store = provider.get(create=True)
        assert store is not None
        self.assertTrue(self.paths.database.exists())
        self.assertIs(provider.get(create=False), store)
        self.assertIs(provider.get(create=True), store)

    def test_after_close_no_worker_can_open_another(self) -> None:
        provider = StoreProvider(self.paths, 14)
        self.assertIsNotNone(provider.get(create=True))
        provider.close()
        for create in (False, True):
            with self.subTest(create=create), self.assertRaisesRegex(StateError, "closed"):
                provider.get(create=create)
        provider.close()

    def test_a_late_worker_after_close_gets_a_message_and_never_raises(self) -> None:
        provider = StoreProvider(self.paths, 14)
        self.assertIsNotNone(provider.get(create=True))
        provider.close()
        with self.assertRaises(StateUnavailableError):
            HistoryReader(self.paths, provider).page(HistoryFilter(), 0)
        sort = SortPreference(provider)
        self.assertEqual(sort.sort(), DEFAULT_SORT)
        self.assertIn("Cannot keep the sort", sort.keep("name", True, 1) or "")
