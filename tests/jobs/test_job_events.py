"""Tests for the job event observer helpers."""

import unittest

from draw_things_control.jobs.job_events import CooldownEnded, combine_observers, notify


class ObserverTests(unittest.TestCase):
    EVENT = CooldownEnded(at="2026-09-25T10:00:00+00:00", waited_seconds=1.0, cut_short=False)

    def test_notify_swallows_an_observer_error(self) -> None:
        def broken(_event: object) -> None:
            raise RuntimeError("display failed")

        notify(broken, self.EVENT)

    def test_notify_does_not_swallow_a_keyboard_interrupt(self) -> None:
        def interrupted(_event: object) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            notify(interrupted, self.EVENT)

    def test_combined_observers_run_in_order_and_isolate_failures(self) -> None:
        seen: list[str] = []

        def broken(_event: object) -> None:
            seen.append("broken")
            raise RuntimeError("display failed")

        combine_observers(lambda _event: seen.append("first"), broken, lambda _event: seen.append("last"))(self.EVENT)
        self.assertEqual(seen, ["first", "broken", "last"])
