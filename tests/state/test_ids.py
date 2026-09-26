"""Tests for how job and execution IDs are written and typed."""

import unittest

from draw_things_control.state.ids import EXECUTION_LETTER, JOB_LETTER, execution_id_text, job_id_text, parse_typed_id


class IdTests(unittest.TestCase):
    def test_ids_show_in_upper_case_with_at_least_four_digits(self) -> None:
        self.assertEqual([job_id_text(1), execution_id_text(12), execution_id_text(9999), execution_id_text(10000)], ["J0001", "E0012", "E9999", "E10000"])

    def test_the_letter_is_case_ignored_and_the_leading_zeros_optional(self) -> None:
        for typed in ("E0012", "e0012", "e012", "E12", " e12 "):
            with self.subTest(typed=typed):
                self.assertEqual(parse_typed_id(typed, EXECUTION_LETTER), 12)
        self.assertEqual(parse_typed_id("j1", JOB_LETTER), 1)
        # The other letter, a bare number, zero, a sign, other digits, or too large a number is not an ID.
        for typed in ("J12", "12", "E0", "E-1", "E1.5", "E²", "EE12", "E", "", "E" + "9" * 20, "E" + "1" * 5000):
            with self.subTest(typed=typed):
                self.assertIsNone(parse_typed_id(typed, EXECUTION_LETTER))
        # Leading zeros never count against the length.
        self.assertEqual(parse_typed_id("E" + "0" * 40 + "7", EXECUTION_LETTER), 7)
