"""What a front end shows of a published span: the seconds left, the share done, and whether the range is worth a mention."""

from __future__ import annotations

import unittest

from framecontrol.services.forecast_view import RANGE_SHARE, progress, seconds_left, show_range

SPAN = {"started_at": 100.0, "end_at": 200.0, "low": 180.0, "high": 230.0, "floor": 0.0}


class SecondsLeftTests(unittest.TestCase):
    def test_it_counts_down_from_the_end_and_stops_at_zero(self) -> None:
        self.assertEqual(seconds_left(SPAN, 150.0), 50.0)
        self.assertEqual(seconds_left(SPAN, 200.0), 0.0)
        self.assertEqual(seconds_left(SPAN, 500.0), 0.0)


class ProgressTests(unittest.TestCase):
    def test_it_is_the_share_of_the_time_from_start_to_end_that_has_passed(self) -> None:
        self.assertEqual(progress(SPAN, 100.0), 0.0)
        self.assertEqual(progress(SPAN, 150.0), 0.5)
        self.assertEqual(progress(SPAN, 200.0), 1.0)
        self.assertEqual(progress(SPAN, 300.0), 1.0)

    def test_it_never_falls_below_the_floor(self) -> None:
        held = {**SPAN, "floor": 0.6}
        self.assertEqual(progress(held, 150.0), 0.6)
        self.assertEqual(progress(held, 190.0), 0.9)

    def test_it_is_the_floor_when_the_end_is_not_after_the_start(self) -> None:
        self.assertEqual(progress({**SPAN, "end_at": 100.0, "floor": 0.4}, 150.0), 0.4)
        self.assertEqual(progress({**SPAN, "end_at": 90.0, "floor": 0.0}, 150.0), 0.0)

    def test_a_time_before_the_start_is_zero(self) -> None:
        self.assertEqual(progress(SPAN, 50.0), 0.0)


class ShowRangeTests(unittest.TestCase):
    def test_the_range_is_shown_when_its_half_width_is_over_three_percent_of_the_time_left(self) -> None:
        self.assertEqual(RANGE_SHARE, 0.03)
        # 100 s left at the start: the range is 80 s to 130 s, a half-width of 25 s.
        self.assertTrue(show_range(SPAN, 100.0))
        narrow = {**SPAN, "low": 199.0, "high": 201.0}
        self.assertFalse(show_range(narrow, 100.0))

    def test_it_is_not_shown_once_nothing_is_left(self) -> None:
        self.assertFalse(show_range(SPAN, 200.0))
