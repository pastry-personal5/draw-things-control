"""What a front end shows of a published span: the seconds left, the share done, and whether the range is worth a mention."""

from __future__ import annotations

import unittest
from datetime import datetime

from framecontrol.services.forecast_view import RANGE_SHARE, end_text, forecast_text, progress, seconds_left, show_range, stage_text, whole_duration

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


class WordsTests(unittest.TestCase):
    """The words every front end writes a forecast in, so the TUI, the CLI, and a person reading JSON agree."""

    NOW = datetime(2026, 9, 26, 14, 0, 40).timestamp()

    @staticmethod
    def forecast(**changes: object) -> dict[str, object]:
        base = datetime(2026, 9, 26, 14, 0, 0).timestamp()
        run = {"started_at": base, "end_at": base + 420, "low": base + 416, "high": base + 424, "floor": 0.0}
        job = {"started_at": base, "end_at": base + 940, "low": base + 865, "high": base + 1015, "floor": 0.0}
        return {"stage": "denoise", "stage_done": 12, "stage_total": 30, "run": run, "job": job, **changes}

    def test_the_end_is_a_clock_time_and_what_is_left_or_the_range(self) -> None:
        now = datetime(2026, 9, 26, 16, 19, 0)
        self.assertEqual(end_text(23 * 60, now), "ends ~16:42 (in 23 min)")
        self.assertEqual(end_text(23 * 60, now, (12 * 60, 39 * 60)), "ends ~16:42 (16:31 to 16:58)")
        self.assertEqual(end_text(3600 * 11 + 1800.4, datetime(2026, 9, 26, 14, 40, 0)), "ends ~09-27 02:10 (in 11 h 30 min)")
        self.assertEqual(whole_duration(432.1), "7 min 12 s")
        self.assertEqual(whole_duration(-3), "0 s")

    def test_a_stage_is_its_name_with_spaces_and_its_units(self) -> None:
        self.assertEqual(stage_text(self.forecast()), "denoise 12/30")
        self.assertEqual(stage_text(self.forecast(stage="text_encode", stage_done=3, stage_total=50)), "text encode 3/50")
        self.assertEqual(stage_text(self.forecast(stage="finishing", stage_done=None, stage_total=None)), "finishing")
        self.assertIsNone(stage_text(self.forecast(stage=None, stage_done=None, stage_total=None)))
        self.assertIsNone(stage_text(None))

    def test_a_forecast_in_a_line_names_the_stage_and_where_the_run_and_the_job_end(self) -> None:
        self.assertEqual(forecast_text(self.forecast(), self.NOW), "denoise 12/30; run ends ~14:07 (in 6 min 20 s), job ends ~14:15 (14:14 to 14:16)")

    def test_between_runs_it_is_the_jobs_end_alone_and_with_no_number_it_says_so(self) -> None:
        self.assertEqual(forecast_text(self.forecast(stage=None, stage_done=None, stage_total=None, run=None), self.NOW), "job ends ~14:15 (14:14 to 14:16)")
        self.assertEqual(forecast_text(self.forecast(run=None, job=None), self.NOW), "denoise 12/30; no estimate yet")
        self.assertEqual(forecast_text(self.forecast(stage=None, run=None, job=None), self.NOW), "no estimate yet")

    def test_the_servers_seconds_left_stand_in_for_a_clock_that_cannot_be_trusted(self) -> None:
        forecast = self.forecast()
        base = datetime(2026, 9, 26, 14, 0, 0).timestamp()
        # The client's clock is an hour fast, but the server read the forecast at 14:00:40 and said 380 s were left.
        forecast["run"] = {**forecast["run"], "seconds_left": 380.0}  # type: ignore[dict-item]
        self.assertTrue(forecast_text(forecast, base + 3600).startswith("denoise 12/30; run ends ~14:07 (in 6 min 20 s)"))

    def test_a_span_whose_end_has_passed_is_finishing(self) -> None:
        late = self.NOW + 3600
        self.assertEqual(forecast_text(self.forecast(), late), "denoise 12/30; run finishing, job finishing")


class EndOfTheCalendarTests(unittest.TestCase):
    def test_a_range_far_past_the_calendar_is_shown_and_does_not_raise(self) -> None:
        now = datetime(2026, 10, 18, 12, 8, 34)
        text = end_text(6164.0, now, (0.0, 1.38e13))
        self.assertTrue(text.startswith("ends ~13:51 (12:08 to "))
