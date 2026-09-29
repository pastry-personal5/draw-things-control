"""Headless tests of parking and holding from the TUI (Milestone 05): the commands, the aliases, the Queue widget's keys
and title, and the Status widget, against a fake ``dtc serve``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from rich.text import Text

from draw_things_control.jobs.events import JobFinished, JobStatus
from draw_things_control.services.queue_hold import HOLD_KEY
from draw_things_control.state.store import Store, StoreMode
from draw_things_control.tui.commands import help_text
from draw_things_control.tui.panes.queue import QueuePane
from draw_things_control.tui.text.queue import queue_entry_detail_text
from tests.tui.fake_server import AT, job_started, run_finished, run_started
from tests.tui.feed_case import FeedTestCase

RUNNING = {"queue_id": "Q0007", "job_path": "/jobs/walk.yaml", "state": "running", "total_runs": 2, "succeeded": 0, "park_requested": False}


class ParkTests(FeedTestCase):
    async def follow(self, pilot: Any, queue_id: str = "Q0007") -> None:
        """Follow a running entry, in its first run of two."""
        self.server.send_queue_entry(queue_id, "running")
        await self.push(pilot, job_started(total_runs=2), run_started(1))
        await self.wait_for(pilot, lambda: pilot.app.live is not None and pilot.app.live.active_run == 1, "the run to start")

    def status(self, app: Any) -> list[str]:
        return self.text(app, "status").split("\n")

    async def test_queue_park_shows_parking_everywhere_and_says_the_queue_is_held(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.follow(pilot)
            await self.command(pilot, "/queue park q7")
            await self.wait_for(pilot, lambda: app.live is not None and app.live.park_requested, "the park")
            status = self.status(app)
            run_line = self.text(app, "run-line")
            title = str(app.screen.query_one(QueuePane).border_title)
        self.assertEqual(self.server.parked_ids, ["Q0007"])
        self.assertIn("Q0007 parks after run 1/2; the queue is held ('/queue release' starts it again)", self.said)
        self.assertEqual(status[0], "parking after run 1/2  E0001: walk")
        self.assertTrue(status[1].startswith("Job") and status[1].endswith("parking"), status[1])
        # The run goes on, so its bar keeps its own estimate.
        self.assertFalse(status[2].endswith("parking"), status[2])
        self.assertIn("parking", run_line)
        self.assertEqual(title, "Queue (held)")

    async def test_park_and_unpark_act_on_the_followed_entry(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/park")
            self.assertEqual(self.since("/park"), ["No job is running"])
            await self.follow(pilot)
            await self.command(pilot, "/unpark")
            self.assertEqual(self.since("/unpark"), ["Not parking"])
            await self.command(pilot, "/park")
            await self.wait_for(pilot, lambda: app.live is not None and app.live.park_requested, "the park")
            await self.command(pilot, "/park")
            self.assertEqual(self.since("/park"), ["Already parking"])
            await self.command(pilot, "/unpark")
            await self.wait_for(pilot, lambda: app.live is not None and not app.live.park_requested, "the unpark")
            status = self.status(app)
        self.assertEqual((self.server.parked_ids, self.server.unparked_ids), (["Q0007"], ["Q0007"]))
        self.assertIn("Q0007 runs on; the queue is not held", self.said)
        self.assertEqual(status[0], "running  E0001: walk  run 1/2")

    async def test_a_park_on_the_last_run_or_one_that_already_parked_is_worded_so(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            self.server.details["Q0007"] = {"queue_id": "Q0007", "state": "running", "total_runs": 2, "succeeded": 1, "current_run": 2}
            await self.command(pilot, "/queue park Q0007")
            self.server.details["Q0008"] = {"queue_id": "Q0008", "state": "parked", "total_runs": 5, "succeeded": 3, "current_run": None}
            await self.command(pilot, "/queue park Q0008")
        self.assertIn("Q0007 is on its last run and will finish; the queue is held ('/queue release' starts it again)", self.said)
        self.assertIn("Q0008 parked after run 3/5; the queue is held ('/queue release' starts it again)", self.said)

    async def test_a_refused_park_says_why(self) -> None:
        self.server.park_responses["Q0003"] = httpx.Response(409, json={"code": "invalid_state", "message": "Q0003 cannot be parked: it is queued and has not started"})
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/queue park Q0003")
        self.assertEqual(self.since("/queue park Q0003"), ["Q0003 cannot be parked: it is queued and has not started"])

    async def test_a_reservation_made_elsewhere_shows_and_a_reseed_keeps_it(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.follow(pilot)
            await self.received(pilot, [self.server.send_park_changed("Q0007", True)])
            self.assertEqual(self.status(app)[0], "parking after run 1/2  E0001: walk")
            await self.received(pilot, [self.server.send_park_changed("Q0007", False)])
            self.assertEqual(self.status(app)[0], "running  E0001: walk  run 1/2")
            # Attaching again, as after a lost connection: the entry's own detail says it is parking.
            self.server.running = [{"queue_id": "Q0007", "job_path": "/jobs/walk.yaml", "total_runs": 2, "submitted_at": "t"}]
            self.server.details["Q0007"] = {"queue_id": "Q0007", "execution_id": None, "current_run": 1, "current_run_elapsed_seconds": 3.0, "park_requested": True}
            self.server.send_reset()
            await self.wait_for(pilot, lambda: app.live is not None and app.live.park_requested, "the seeded reservation")
        self.assertTrue(app.live is not None and app.live.phase == "parking")

    async def test_a_reservation_on_the_last_run_reads_as_finishing_it(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            self.server.send_queue_entry("Q0007", "running")
            await self.push(pilot, job_started(total_runs=2), run_started(1), run_finished(1), run_started(2))
            await self.received(pilot, [self.server.send_park_changed("Q0007", True)])
            self.assertEqual(self.status(app)[0], "parking on its last run  E0001: walk")

    async def test_a_park_at_verbose_low_shows_at_once(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/verbose low")
            await self.follow(pilot)
            await self.command(pilot, "/park")
            await self.wait_for(pilot, lambda: self.status(app)[0].startswith("parking"), "parking on the Status widget")

    async def test_a_job_that_parks_says_how_to_go_on(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            self.server.send_queue_entry("Q0007", "running")
            await self.push(pilot, job_started(total_runs=5, first_run=3), run_started(3, 5), run_finished(3))
            await self.push(pilot, JobFinished(at=AT, status=JobStatus.PARKED, exit_code=3, completed_runs=1, total_runs=5, signal=None))
            await self.wait_for(pilot, lambda: app.live is not None and app.live.ended, "the end")
            status = self.status(app)
        self.assertIn("Q0007 parked after run 3/5 ('/queue resume Q0007' continues at run 4)", self.log())
        self.assertEqual(status[0], "finished (parked)  E0001: walk")


class HoldTests(FeedTestCase):
    async def test_hold_and_release_and_their_aliases(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.command(pilot, "/hold")
            await self.wait_for(pilot, lambda: app.queue_hold.held, "the hold")
            title = str(app.screen.query_one(QueuePane).border_title)
            first_line = self.text(app, "status").split("\n")[0]
            await self.command(pilot, "/queue release")
            await self.wait_for(pilot, lambda: not app.queue_hold.held, "the release")
            released_title = str(app.screen.query_one(QueuePane).border_title)
            await self.command(pilot, "/release")
            await self.command(pilot, "/queue hold")
        self.assertTrue(self.since("/hold")[0].startswith("Queue held since "))
        self.assertEqual((title, released_title), ("Queue (held)", "Queue"))
        self.assertTrue(first_line.startswith("Queue held since "), first_line)
        self.assertEqual(self.since("/release")[0], "The queue is not held")
        self.assertTrue(self.since("/queue hold")[0].startswith("Queue held since "))
        self.assertIn("Queue released", self.said)

    async def test_a_hold_made_elsewhere_shows_and_a_submission_while_held_says_so(self) -> None:
        self.write_data_job("walk.yaml")
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            await self.received(pilot, [self.server.send_held(by="Q0003")])
            await self.wait_for(pilot, lambda: app.queue_hold.held, "the hold")
            since = datetime.fromisoformat(AT).astimezone()
            wanted = f"Queue held since {since.strftime('%H:%M') if since.date() == datetime.now().date() else since.strftime('%Y-%m-%d %H:%M')} (by Q0003)"
            self.assertEqual(self.text(app, "status").split("\n")[0], wanted)
            await self.command(pilot, "/apply walk.yaml")
            await self.wait_for(pilot, lambda: any("queued" in line for line in self.said), "the submission")
            await self.received(pilot, [self.server.send_released()])
            await self.wait_for(pilot, lambda: not app.queue_hold.held, "the release")
        self.assertIn("Q0001 queued: walk.yaml; the queue is held ('/queue release' starts it)", self.said)

    async def test_the_hold_is_the_third_line_under_a_finished_job(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            self.server.send_queue_entry("Q0001", "running")
            await self.push(pilot, job_started(total_runs=1), run_started(1, 1), run_finished(1))
            await self.push(pilot, JobFinished(at=AT, status=JobStatus.SUCCEEDED, exit_code=0, completed_runs=1, total_runs=1, signal=None))
            await self.received(pilot, [self.server.send_held(by="Q0001")])
            await self.wait_for(pilot, lambda: self.text(app, "status").split("\n")[2].startswith("Queue held since"), "the hold under the job")

    async def test_the_queue_widget_parks_and_unparks_the_selected_row_and_shows_parking(self) -> None:
        self.server.entries = [{**RUNNING, "park_requested": True}]
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.connect(pilot)
            # A queue event reads the queue again, whatever the first read found.
            await self.received(pilot, [self.server.send_park_changed("Q0007", True)])
            pane = app.screen.query_one(QueuePane)
            await self.wait_for(pilot, lambda: pane.row_count == 1, "the queue rows")
            state = str(pane.get_row_at(0)[2])
            pane.focus()
            await pilot.press("u")
            await self.wait_for(pilot, lambda: self.server.unparked_ids == ["Q0007"], "the unpark")
            await pilot.press("p")
            await self.wait_for(pilot, lambda: self.server.parked_ids == ["Q0007"], "the park")
        self.assertEqual(state, "parking")

    async def test_the_saved_hold_shows_from_the_store_while_the_server_is_down(self) -> None:
        self.server.health_ok = False
        store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        store.settings.set(HOLD_KEY, '{"since": "2026-09-29T10:00:00+00:00", "by": null}')
        store.close()
        app = self.make_app()
        async with app.run_test(size=(160, 60)) as pilot:
            await self.settle(pilot)
            await self.wait_for(pilot, lambda: app.queue_hold.held, "the saved hold")
            await self.wait_for(pilot, lambda: str(app.screen.query_one(QueuePane).border_title) == "Queue (held)", "the title")
            await self.wait_for(pilot, lambda: self.text(app, "status").split("\n")[0].startswith("Queue held since"), "the Status widget")

    def test_describe_queue_names_the_park_reservation_and_the_hold(self) -> None:
        entry = {**RUNNING, "park_requested": True, "current_run": 1, "held": True, "held_since": "2026-09-29T12:04:05+00:00", "held_by": "Q0007", "resumable": False, "resume_refused_reason": "Q0007 is running"}
        text = queue_entry_detail_text(entry).plain
        self.assertIn("Queue entry Q0007: parking", text)
        self.assertIn("  Park reservation: yes", text)
        self.assertIn("  Queue: held since 2026-09-29 12:04:05 (by Q0007)", text)
        self.assertNotIn("Park reservation", queue_entry_detail_text({**entry, "park_requested": False}).plain)

    def test_help_lists_the_park_keys_and_commands(self) -> None:
        text = Text.assemble(help_text()).plain
        for words in ("p / u", "/queue park <Queue ID>", "/queue unpark <Queue ID>", "/queue hold", "/queue release", "/park", "/unpark", "/hold", "/release"):
            self.assertIn(words, text)
