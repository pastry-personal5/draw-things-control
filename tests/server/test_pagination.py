"""Tests for the opaque pagination cursor and limit clamping."""

import unittest

from draw_things_control.core.errors import InputError
from draw_things_control.server.pagination import DEFAULT_LIMIT, MAX_LIMIT, clamp_limit, decode_cursor, encode_cursor, next_cursor


class CursorTests(unittest.TestCase):
    def test_a_cursor_round_trips(self) -> None:
        for offset in (0, 1, 200, 123456):
            with self.subTest(offset):
                self.assertEqual(decode_cursor(encode_cursor(offset)), offset)

    def test_no_cursor_is_offset_zero(self) -> None:
        self.assertEqual(decode_cursor(None), 0)

    def test_a_cursor_this_server_did_not_issue_is_refused(self) -> None:
        for bogus in ("not-base64!!", "0", "-5", "", "Q1VSU09S"):
            with self.subTest(bogus):
                with self.assertRaises(InputError):
                    decode_cursor(bogus)

    def test_next_cursor_is_none_when_the_page_was_not_full(self) -> None:
        self.assertIsNone(next_cursor(0, limit=200, returned=5))
        self.assertEqual(decode_cursor(next_cursor(0, limit=5, returned=5)), 5)
        self.assertEqual(decode_cursor(next_cursor(10, limit=5, returned=5)), 15)


class LimitTests(unittest.TestCase):
    def test_a_missing_limit_defaults_to_200(self) -> None:
        self.assertEqual(clamp_limit(None), DEFAULT_LIMIT)

    def test_a_limit_over_200_is_capped(self) -> None:
        self.assertEqual(clamp_limit(10_000), MAX_LIMIT)

    def test_a_limit_of_1_is_accepted(self) -> None:
        self.assertEqual(clamp_limit(1), 1)

    def test_a_limit_below_1_is_refused(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad):
                with self.assertRaises(InputError):
                    clamp_limit(bad)


if __name__ == "__main__":
    unittest.main()
