"""Tests for the store a browsing front end shares: opened when needed, kept, and never created by a read."""

from draw_things_control.services.store_provider import StoreProvider
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

    def test_close_closes_it_and_the_next_get_opens_another(self) -> None:
        provider = StoreProvider(self.paths, 14)
        first = provider.get(create=True)
        provider.close()
        second = provider.get(create=False)
        self.addCleanup(provider.close)
        self.assertIsNotNone(second)
        self.assertIsNot(first, second)
        provider.close()
        provider.close()
