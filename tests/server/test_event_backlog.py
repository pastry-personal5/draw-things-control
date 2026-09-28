"""Tests for the bounded in-memory event backlog behind WatchEvents."""

from __future__ import annotations

import json
import threading
import time
import unittest

from draw_things_control.server.event_backlog import EventBacklog


class EventBacklogTests(unittest.TestCase):
    def test_last_event_id_zero_replays_nothing(self) -> None:
        backlog = EventBacklog()
        backlog.append("job_started", {})
        self.assertEqual(backlog.since(0), ())

    def test_events_after_the_given_id_are_replayed_in_order(self) -> None:
        backlog = EventBacklog()
        first = backlog.append("job_started", {})
        second = backlog.append("run_started", {})
        third = backlog.append("run_finished", {})
        replayed = backlog.since(first.id)
        assert replayed is not None
        self.assertEqual([event.id for event in replayed], [second.id, third.id])
        self.assertEqual(backlog.since(third.id), ())

    def test_an_id_older_than_the_retained_backlog_resets(self) -> None:
        backlog = EventBacklog(capacity=2)
        backlog.append("a", {})
        backlog.append("b", {})
        third = backlog.append("c", {})
        self.assertIsNone(backlog.since(1))
        self.assertEqual(backlog.since(third.id), ())

    def test_a_nonzero_id_on_a_still_empty_backlog_resets(self) -> None:
        # A client reconnecting to a freshly (re)started server, before its first event: an empty backlog is not
        # "nothing to compare against", it must still say Reset rather than silently returning ().
        backlog = EventBacklog()
        self.assertIsNone(backlog.since(1))

    def test_an_id_from_a_previous_run_is_effectively_always_older_and_resets(self) -> None:
        # IDs are seeded from wall-clock milliseconds, so a small ID like 1 (as a fresh restart's counter would
        # never produce) reads the same as any other stale ID: older than the retained backlog.
        backlog = EventBacklog()
        backlog.append("job_started", {})
        self.assertIsNone(backlog.since(1))

    def test_latest_id_is_the_seed_when_empty_and_the_last_event_once_appended(self) -> None:
        backlog = EventBacklog()
        seed = backlog.latest_id()
        appended = backlog.append("job_started", {})
        self.assertEqual(backlog.latest_id(), appended.id)
        self.assertGreater(appended.id, seed)

    def test_wait_for_more_returns_once_a_newer_event_arrives(self) -> None:
        backlog = EventBacklog()
        first = backlog.append("job_started", {})

        def append_later() -> None:
            time.sleep(0.05)
            backlog.append("run_started", {})

        thread = threading.Thread(target=append_later)
        thread.start()
        self.assertTrue(backlog.wait_for_more(first.id, timeout=2.0))
        thread.join()

    def test_appended_data_is_serialized_to_json(self) -> None:
        backlog = EventBacklog()
        event = backlog.append("run_output", {"number": 1, "text": "hello"})
        self.assertEqual(json.loads(event.data_json), {"number": 1, "text": "hello"})

    def test_wait_for_more_times_out_when_nothing_new_arrives(self) -> None:
        backlog = EventBacklog()
        latest = backlog.append("job_started", {})
        self.assertFalse(backlog.wait_for_more(latest.id, timeout=0.05))


if __name__ == "__main__":
    unittest.main()
