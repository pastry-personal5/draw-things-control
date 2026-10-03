"""Tests for parking a running entry and holding the queue (Milestone 05): the worker's reservation and hold, and the
service functions the API calls."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest import mock

from draw_things_control.core.arguments import DrawThingsGenerateArguments
from draw_things_control.core.errors import NotPermittedError
from draw_things_control.services.queue_events import QueueEventPublisher
from draw_things_control.services.queue_hold import HOLD_KEY, HoldState, QueueHold, hold_text, read_hold
from draw_things_control.services.queue_park import ParkRefusedError, park_entry, unpark_entry
from draw_things_control.services.queue_recovery import recover_queue
from draw_things_control.services.queue_resume import resume_entry
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.queue import QueueState
from draw_things_control.state.settings import SettingsRepository
from tests.fixtures import job_data
from tests.jobs.test_executor import BlockingRunner, FakeResult, FakeRunner
from tests.services.test_queue_worker import NOW, QueueWorkerCase


class ParkCase(QueueWorkerCase):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None
        self.worker = self.build_worker(on_event=self.record_event)

    def record_event(self, kind: str, data: dict[str, Any]) -> None:
        self.events.append((kind, data))
        if self.on_event is not None:
            self.on_event(kind, data)

    def kinds(self, prefix: str = "queue_") -> list[str]:
        return [kind for kind, _data in self.events if kind.startswith(prefix) and kind != "queue_entry_changed"]

    def during_run(self, number: int, action: Callable[[], object]) -> None:
        """Call ``action`` as run ``number`` of the claimed entry starts (counted across this test's runs)."""
        make = self.next_runner

        def runner(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            if self.starts == number:
                action()
            return make(arguments)

        self.next_runner = runner

    def park(self, label: str) -> None:
        park_entry(self.store, self.worker, self.entry(label).id)

    def execution_status(self, label: str) -> tuple[str, int]:
        entry = self.entry(label)
        assert entry.execution_number is not None
        execution = self.store.executions.by_number(entry.execution_number)
        assert execution is not None
        return execution.status, len(execution.succeeded_runs)


class ParkTests(ParkCase):
    def test_a_park_during_run_2_of_3_ends_it_parked_and_holds_the_queue(self) -> None:
        label = self.submit(run_count=3)
        waiting = self.submit(run_count=1)
        self.during_run(2, lambda: self.park(label))
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual((self.entry(label).state, self.entry(label).succeeded), ("parked", 2))
        self.assertEqual(self.execution_status(label), ("parked", 2))
        self.assertEqual(self.starts, 2)
        hold = self.worker.hold_state()
        self.assertEqual((hold.held, hold.by, hold.since is not None), (True, label, True))
        self.assertEqual(self.worker.state(), "held")
        # Held: nothing is claimed, and the queued entry waits.
        self.assertFalse(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(waiting).state, "queued")
        self.assertEqual(self.kinds(), ["queue_held", "queue_park_changed"])
        self.assertEqual(self.events[[kind for kind, _ in self.events].index("queue_park_changed")][1], {"queue_id": label, "park_requested": True})
        # A release starts it again.
        self.assertEqual(self.worker.release(), (True, HoldState()))
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(waiting).state, "succeeded")
        self.assertEqual(self.kinds()[-1], "queue_released")

    def test_the_parked_entry_resumes_at_the_next_run_with_its_seed_and_last_frame(self) -> None:
        label = self.submit(run_count=3)
        self.during_run(2, lambda: self.park(label))
        self.worker.claim_and_run_one()
        run_2 = self.runners[1].arguments
        resumed = resume_entry(self.store, self.entry(label).id, self.global_config, self.params, clock=lambda: NOW)
        assert run_2.output is not None
        self.assertEqual((resumed.resume_first_run, resumed.resume_input, resumed.resume_seed), (3, str(run_2.output.with_name(f"{run_2.output.stem}-last-frame.png")), run_2.seed))
        # A resume never releases the hold.
        self.assertTrue(self.worker.hold_state().held)

    def test_retention_keeps_a_parked_chain_resumable_until_a_resume_in_it_succeeds_a_run(self) -> None:
        label = self.submit(run_count=3)
        self.during_run(1, lambda: self.park(label))
        self.worker.claim_and_run_one()
        self.worker.release()
        # A resume whose first run fails, then a resume of that, cancelled before it ran.
        failed = resume_entry(self.store, self.entry(label).id, self.global_config, self.params, clock=lambda: NOW)
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(return_code=1), write_output=False)
        self.worker.claim_and_run_one()
        cancelled = resume_entry(self.store, failed.id, self.global_config, self.params, clock=lambda: NOW)
        self.assertTrue(self.store.queue.cancel_queued(cancelled.id, now=NOW))
        chain = [self.entry(name) for name in (label, failed.label, cancelled.label)]
        with mock.patch.object(self.store, "retention_cutoff", return_value=float("inf")):
            self.store.prune()
        # All kept, with their executions, so the newest resume still walks back to the parked entry's run 1.
        self.assertEqual([self.store.queue.get(entry.id) is not None for entry in chain], [True, True, True])
        self.assertTrue(all(self.store.executions.by_number(entry.execution_number) is not None for entry in chain if entry.execution_number is not None))
        resumed = resume_entry(self.store, cancelled.id, self.global_config, self.params, clock=lambda: NOW)
        self.assertEqual(resumed.resume_first_run, 2)
        # A succeeded run anywhere down the chain lets it all go.
        self.next_runner = lambda arguments: FakeRunner(arguments, FakeResult(), write_output=True)
        self.worker.claim_and_run_one()
        with mock.patch.object(self.store, "retention_cutoff", return_value=float("inf")):
            self.store.prune()
        self.assertEqual([self.store.queue.get(entry.id) is not None for entry in chain], [False, False, False])

    def test_a_park_before_the_job_starts_parks_after_the_first_run_it_makes(self) -> None:
        label = self.submit(run_count=3)
        patcher, claimed, release = self.blocked_start()
        with patcher:
            thread = threading.Thread(target=self.worker.claim_and_run_one)
            thread.start()
            self.assertTrue(claimed.wait(5))
            self.park(label)
            self.assertTrue(self.worker.park_requested(self.entry(label).id))
            release.set()
            thread.join(timeout=5)
        self.assertEqual((self.entry(label).state, self.starts), ("parked", 1))

    def test_a_park_that_lands_as_the_entry_is_claimed_is_published_after_its_running(self) -> None:
        def record_slowly(kind: str, data: dict[str, Any]) -> None:
            # A slow sink: the claim's own 'running' takes a while to publish, so a park made meanwhile could overtake it.
            if kind == "queue_entry_changed" and data["state"] == "running":
                time.sleep(0.05)
            self.record_event(kind, data)

        self.worker = self.build_worker(on_event=record_slowly)
        label = self.submit(run_count=3)
        real_claim = self.store.queue.claim_oldest
        parkers: list[threading.Thread] = []
        # Asserted after the claim: claim_and_run_one logs and swallows an error raised inside it.
        parker_waiting: list[bool] = []

        def claim_then_park(now: Any) -> Any:
            entry = real_claim(now)
            if entry is not None and not parkers:
                # The park waits on the worker's lock, which the claim holds, and takes it the moment the claim lets go.
                waiting = threading.Event()

                def park() -> None:
                    waiting.set()
                    self.worker.park_running(entry.id, entry.label)

                parkers.append(threading.Thread(target=park))
                parkers[0].start()
                parker_waiting.append(waiting.wait(5))
                time.sleep(0.05)
            return entry

        with mock.patch.object(self.store.queue, "claim_oldest", side_effect=claim_then_park):
            self.worker.claim_and_run_one()
        parkers[0].join(timeout=5)
        self.assertEqual(parker_waiting, [True])
        # A front end reads 'running' as the claim, and a park it hears before that as nobody's.
        changes = [(kind, data.get("state")) for kind, data in self.events if kind in ("queue_entry_changed", "queue_park_changed")]
        self.assertEqual(changes[:2], [("queue_entry_changed", "running"), ("queue_park_changed", None)])
        self.assertEqual(self.entry(label).state, "parked")

    def test_a_release_once_the_parked_job_has_ended_drops_the_between_jobs_cooldown(self) -> None:
        waits: list[float] = []
        self.worker = self.build_worker(on_event=self.record_event, wait_between_jobs=waits.append)
        label = self.submit(run_count=3, cooldown={"mode": "manual", "seconds": 30})
        waiting = self.submit(run_count=1)
        self.during_run(1, lambda: self.park(label))

        def release_once_parked(kind: str, data: dict[str, Any]) -> None:
            # The entry is marked parked, and the worker has not yet decided on the cooldown after it.
            if kind == "queue_entry_changed" and data["state"] == "parked":
                self.worker.release()

        self.on_event = release_once_parked
        self.worker.claim_and_run_one()
        self.assertEqual((self.entry(label).state, waits), ("parked", []))
        self.assertTrue(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(waiting).state, "succeeded")

    def test_a_release_while_the_parking_job_still_runs_keeps_the_between_jobs_cooldown(self) -> None:
        waits: list[float] = []
        self.worker = self.build_worker(on_event=self.record_event, wait_between_jobs=waits.append)
        label = self.submit(run_count=3, cooldown={"mode": "manual", "seconds": 30})
        self.submit(run_count=1)
        self.during_run(1, lambda: (self.park(label), self.worker.release()))
        self.worker.claim_and_run_one()
        # The entry still parks, and the worker moves on as after a succeeded job, with the usual cooldown.
        self.assertEqual((self.entry(label).state, waits), ("parked", [30.0]))

    def test_an_unpark_before_the_job_starts_withdraws_the_pending_park(self) -> None:
        label = self.submit(run_count=3)
        patcher, claimed, release = self.blocked_start()
        with patcher:
            thread = threading.Thread(target=self.worker.claim_and_run_one)
            thread.start()
            self.assertTrue(claimed.wait(5))
            self.park(label)
            unpark_entry(self.store, self.worker, self.entry(label).id)
            release.set()
            thread.join(timeout=5)
        self.assertEqual((self.entry(label).state, self.starts), ("succeeded", 3))
        self.assertFalse(self.worker.hold_state().held)

    def test_a_park_during_the_last_run_lets_it_succeed_and_holds_the_queue(self) -> None:
        label = self.submit(run_count=2)
        self.during_run(2, lambda: self.park(label))
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertEqual(self.worker.hold_state().by, label)

    def test_a_parking_run_that_fails_ends_failed_and_the_queue_stays_held(self) -> None:
        label = self.submit(run_count=3)
        make = self.next_runner

        def fail_run_2(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            if self.starts == 2:
                self.park(label)
                return FakeRunner(arguments, FakeResult(return_code=1), write_output=False)
            return make(arguments)

        self.next_runner = fail_run_2
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "failed")
        self.assertTrue(self.worker.hold_state().held)

    def test_a_cancel_of_a_parking_entry_ends_it_cancelled_and_the_queue_stays_held(self) -> None:
        label = self.submit(run_count=3)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        thread = threading.Thread(target=self.worker.claim_and_run_one)
        thread.start()
        self.wait_until(lambda: self.starts >= 1)
        self.park(label)
        # Parking an entry being cancelled is refused: the cancel has already cost the run.
        self.assertTrue(self.worker.cancel_running(self.entry(label).id))
        with self.assertRaisesRegex(ParkRefusedError, f"{label} cannot be parked: it is being cancelled"):
            self.park(label)
        thread.join(timeout=5)
        self.assertEqual(self.entry(label).state, "cancelled")
        self.assertTrue(self.worker.hold_state().held)

    def test_a_cancel_after_the_park_took_effect_leaves_the_entry_parked(self) -> None:
        label = self.submit(run_count=3)
        self.during_run(1, lambda: self.park(label))

        def cancel_at_job_finished(kind: str, _data: dict[str, Any]) -> None:
            if kind == "job_finished":
                self.worker.cancel_running(self.entry(label).id)

        self.on_event = cancel_at_job_finished
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "parked")

    def test_an_unpark_lets_the_job_run_on_and_releases_the_hold_its_reservation_made(self) -> None:
        label = self.submit(run_count=3)
        self.during_run(2, lambda: (self.park(label), unpark_entry(self.store, self.worker, self.entry(label).id)))
        self.worker.claim_and_run_one()
        self.assertEqual((self.entry(label).state, self.starts), ("succeeded", 3))
        self.assertFalse(self.worker.hold_state().held)
        self.assertEqual(self.kinds(), ["queue_held", "queue_park_changed", "queue_released", "queue_park_changed"])

    def test_an_unpark_keeps_a_hold_the_queue_already_had(self) -> None:
        label = self.submit(run_count=2)
        self.during_run(1, lambda: (self.worker.hold(), self.park(label), unpark_entry(self.store, self.worker, self.entry(label).id)))
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "succeeded")
        hold = self.worker.hold_state()
        self.assertEqual((hold.held, hold.by), (True, None))

    def test_a_park_whose_hold_cannot_be_saved_makes_no_reservation(self) -> None:
        label = self.submit(run_count=2)
        parking: list[bool] = []

        def park_with_the_store_locked() -> None:
            with mock.patch.object(SettingsRepository, "set", side_effect=sqlite3.OperationalError("database is locked")), self.assertRaises(sqlite3.OperationalError):
                self.park(label)
            parking.append(self.worker.park_requested(self.entry(label).id))

        self.during_run(1, park_with_the_store_locked)
        self.worker.claim_and_run_one()
        self.assertEqual(parking, [False])
        self.assertEqual((self.entry(label).state, self.starts), ("succeeded", 2))
        self.assertFalse(self.worker.hold_state().held)
        self.assertEqual(self.kinds(), [])

    def test_an_unpark_whose_hold_cannot_be_released_keeps_the_reservation(self) -> None:
        label = self.submit(run_count=3)
        parking: list[bool] = []

        def park_then_unpark_with_the_store_locked() -> None:
            self.park(label)
            with mock.patch.object(SettingsRepository, "delete", side_effect=sqlite3.OperationalError("database is locked")), self.assertRaises(sqlite3.OperationalError):
                unpark_entry(self.store, self.worker, self.entry(label).id)
            parking.append(self.worker.park_requested(self.entry(label).id))

        self.during_run(1, park_then_unpark_with_the_store_locked)
        self.worker.claim_and_run_one()
        self.assertEqual(parking, [True])
        self.assertEqual((self.entry(label).state, self.starts), ("parked", 1))
        self.assertEqual(self.worker.hold_state().by, label)
        self.assertEqual(self.kinds(), ["queue_held", "queue_park_changed"])

    def test_a_direct_hold_makes_a_reservations_hold_its_own_so_an_unpark_keeps_it(self) -> None:
        label = self.submit(run_count=2)
        changed: list[bool] = []
        self.during_run(1, lambda: (self.park(label), changed.append(self.worker.hold()[0]), unpark_entry(self.store, self.worker, self.entry(label).id)))
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "succeeded")
        hold = self.worker.hold_state()
        self.assertEqual((hold.held, hold.by), (True, None))
        # The queue was already held, by the reservation, so the direct hold reports no change.
        self.assertEqual(changed, [False])

    def test_a_park_again_after_a_release_holds_the_queue_again(self) -> None:
        label = self.submit(run_count=3)

        def park_release_park() -> None:
            self.park(label)
            self.worker.release()
            self.assertFalse(self.worker.hold_state().held)
            self.park(label)

        self.during_run(1, park_release_park)
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "parked")
        self.assertEqual(self.worker.hold_state().by, label)

    def test_a_release_while_parking_lets_it_park_and_the_worker_moves_on_after_the_cooldown(self) -> None:
        waits: list[float] = []
        self.worker = self.build_worker(wait_between_jobs=waits.append)
        label = self.submit(run_count=3, cooldown={"mode": "manual", "seconds": 12.5})
        self.submit(run_count=1)
        self.during_run(1, lambda: (self.park(label), self.worker.release()))
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "parked")
        self.assertEqual(waits, [12.5])

    def test_an_unpark_after_the_park_took_effect_is_refused(self) -> None:
        label = self.submit(run_count=3)
        self.during_run(1, lambda: self.park(label))
        refusals: list[str] = []

        def unpark_at_job_finished(kind: str, _data: dict[str, Any]) -> None:
            if kind == "job_finished":
                try:
                    unpark_entry(self.store, self.worker, self.entry(label).id)
                except ParkRefusedError as error:
                    refusals.append(str(error))

        self.on_event = unpark_at_job_finished
        self.worker.claim_and_run_one()
        self.assertEqual(refusals, [f"{label} has already parked"])
        self.assertEqual(self.entry(label).state, "parked")
        self.assertTrue(self.worker.hold_state().held)
        with self.assertRaisesRegex(ParkRefusedError, f"{label} cannot be unparked: it is parked"):
            unpark_entry(self.store, self.worker, self.entry(label).id)

    def test_park_and_unpark_refuse_a_queued_or_finished_entry_and_change_nothing(self) -> None:
        done = self.submit(run_count=1)
        self.worker.claim_and_run_one()
        queued = self.submit(run_count=1)
        with self.assertRaisesRegex(ParkRefusedError, f"{queued} cannot be parked: it is queued"):
            self.park(queued)
        with self.assertRaisesRegex(ParkRefusedError, f"{done} cannot be parked: it is succeeded"):
            self.park(done)
        with self.assertRaisesRegex(ParkRefusedError, f"{queued} cannot be unparked: it is queued"):
            unpark_entry(self.store, self.worker, self.entry(queued).id)
        self.assertFalse(self.worker.hold_state().held)
        self.assertEqual(self.kinds(), [])

    def test_a_park_that_loses_the_race_with_the_jobs_end_is_refused_and_holds_nothing(self) -> None:
        label = self.submit(run_count=1)
        # What the service read just before the job ended.
        running = replace(self.entry(label), state="running")
        self.worker.claim_and_run_one()
        real_get = self.store.queue.get
        reads = iter([running])
        with mock.patch.object(self.store.queue, "get", side_effect=lambda entry_id: next(reads, None) or real_get(entry_id)):
            with self.assertRaisesRegex(ParkRefusedError, f"{label} cannot be parked: it is succeeded"):
                self.park(label)
        self.assertFalse(self.worker.hold_state().held)

    def test_a_park_after_the_entry_is_marked_finished_but_still_current_is_refused(self) -> None:
        label = self.submit(run_count=1)
        results: list[bool] = []

        def park_at_the_finish(kind: str, data: dict[str, Any]) -> None:
            if kind == "queue_entry_changed" and data["state"] == "succeeded":
                results.append(self.worker.park_running(self.entry(label).id, label))

        self.on_event = park_at_the_finish
        self.worker.claim_and_run_one()
        self.assertEqual(results, [False])
        self.assertFalse(self.worker.hold_state().held)

    def test_a_park_on_an_entry_already_parking_is_a_no_op(self) -> None:
        label = self.submit(run_count=3)
        self.during_run(1, lambda: (self.park(label), self.park(label)))
        self.worker.claim_and_run_one()
        self.assertEqual(self.kinds(), ["queue_held", "queue_park_changed"])

    def test_stopping_the_server_while_parking_interrupts_the_entry_and_keeps_the_hold(self) -> None:
        label = self.submit(run_count=3)
        self.next_runner = lambda arguments: BlockingRunner(arguments)
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.wait_until(lambda: self.starts >= 1)
        self.park(label)
        self.worker.stop()
        self.assertEqual(self.entry(label).state, "interrupted")
        self.assertEqual(read_hold(self.store.settings).by, label)


class HoldTests(QueueWorkerCase):
    def test_a_direct_hold_keeps_anything_from_starting_until_a_release(self) -> None:
        changed, hold = self.worker.hold()
        self.assertEqual((changed, hold.held, hold.by), (True, True, None))
        self.assertEqual(self.worker.hold()[0], False)
        label = self.submit(run_count=1)
        self.assertFalse(self.worker.claim_and_run_one())
        self.assertEqual((self.entry(label).state, self.worker.state()), ("queued", "held"))
        self.assertEqual(self.worker.release(), (True, HoldState()))
        self.assertEqual(self.worker.release(), (False, HoldState()))
        self.assertTrue(self.worker.claim_and_run_one())

    def test_a_hold_does_not_stop_a_running_job_and_nothing_starts_after_it(self) -> None:
        label = self.submit(run_count=2)
        after = self.submit(run_count=1)
        make = self.next_runner

        def hold_in_run_1(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            if self.starts == 1:
                self.worker.hold()
                self.assertEqual(self.worker.state(), "running")
            return make(arguments)

        self.next_runner = hold_in_run_1
        self.worker.claim_and_run_one()
        self.assertEqual(self.entry(label).state, "succeeded")
        self.assertFalse(self.worker.claim_and_run_one())
        self.assertEqual(self.entry(after).state, "queued")

    def test_a_hold_ends_the_between_jobs_wait_and_a_release_claims_at_once(self) -> None:
        first = self.submit(run_count=1, cooldown={"mode": "manual", "seconds": 30})
        second = self.submit(run_count=1)
        self.worker.start()
        self.addCleanup(self.worker.stop)
        self.wait_until(lambda: self.worker.state() == "cooling_down")
        self.worker.hold()
        self.wait_until(lambda: self.worker.state() == "held")
        self.assertEqual((self.entry(first).state, self.entry(second).state), ("succeeded", "queued"))
        self.worker.release()
        self.wait_until(lambda: self.entry(second).state == "succeeded", timeout=3)

    def test_the_hold_survives_a_restart_and_a_damaged_one_counts_as_held(self) -> None:
        self.worker.hold()
        again = self.build_worker()
        self.assertTrue(again.hold_state().held)
        self.store.settings.set(HOLD_KEY, "{not json")
        damaged = self.build_worker()
        self.assertEqual(damaged.hold_state(), HoldState(held=True))
        self.submit(run_count=1)
        self.assertFalse(damaged.claim_and_run_one())
        self.assertTrue(damaged.release()[0])
        self.assertIsNone(self.store.settings.get(HOLD_KEY))

    def test_the_saved_hold_is_json_naming_since_by_and_caller(self) -> None:
        self.worker.hold("tui")
        data = json.loads(self.store.settings.get(HOLD_KEY) or "")
        self.assertEqual(set(data), {"since", "by", "caller"})
        self.assertEqual((data["by"], data["caller"]), (None, "tui"))
        self.assertTrue(hold_text(self.worker.hold_state()).startswith("Queue held since 2026-09-27 15:30:12"))
        for text in ('{"since": 5, "by": null}', '{"since": "yesterday", "by": null}', '{"by": "Q0001"}', "[]", '{"since": "2026-09-27T15:30:12", "by": null, "caller": 5}'):
            with self.subTest(text=text):
                self.store.settings.set(HOLD_KEY, text)
                self.assertEqual(read_hold(self.store.settings), HoldState(held=True))

    def test_recovery_marks_an_entry_whose_execution_parked_as_parked(self) -> None:
        label = self.submit(run_count=3)
        make = self.next_runner

        def park_run_1(arguments: DrawThingsGenerateArguments) -> FakeRunner:
            if self.starts == 1:
                park_entry(self.store, self.worker, self.entry(label).id)
            return make(arguments)

        self.next_runner = park_run_1
        self.worker.claim_and_run_one()
        # As a crash would leave it: the execution parked, the entry still running.
        self.store._database.connection().execute("UPDATE queue SET state = 'running' WHERE queue_number = ?", (int(label[1:]),)).connection.commit()
        recover_queue(self.store, clock=lambda: NOW)
        self.assertEqual(self.entry(label).state, str(QueueState.PARKED))


class HoldCallerTests(ParkCase):
    """Milestone 10: the hold records its caller, and an agent never ends, or comes to own, a hold a person made."""

    def queue_hold(self) -> QueueHold:
        return QueueHold(self.store.settings, QueueEventPublisher(None), clock=lambda: NOW)

    def submit_as(self, caller: str, **changes: object) -> str:
        changes.setdefault("prompt_pairs", [{"name": "only", "positive": "text"}])
        return submit_job(self.write_job(job_data(**changes)), self.global_config, self.params, self.store, submitted_by=caller).label

    def test_an_agent_cannot_release_a_persons_hold_and_its_own_hold_over_one_changes_nothing(self) -> None:
        self.worker.hold("tui")
        self.assertEqual(self.worker.hold("mcp")[0], False)
        self.assertEqual(self.worker.hold_state().caller, "tui")
        with self.assertRaisesRegex(NotPermittedError, "^The queue was held by tui; an agent may release only a hold an agent made$"):
            self.worker.release("mcp")
        self.assertTrue(self.worker.hold_state().held)
        self.assertEqual(self.worker.release("cli")[0], True)

    def test_a_persons_direct_hold_makes_an_agents_hold_the_persons(self) -> None:
        hold = self.queue_hold()
        for agents in (None, "Q0007"):
            with self.subTest(agents_hold_by=agents):
                hold.hold(agents, "mcp")
                self.assertFalse(hold.hold(None, "cli"))
                self.assertEqual((hold.snapshot().by, hold.snapshot().caller), (None, "cli"))
                self.assertEqual(read_hold(self.store.settings).caller, "cli")
                self.assertRaises(NotPermittedError, hold.release, "mcp")
                hold.release("cli")

    def test_an_agents_direct_hold_over_a_persons_park_makes_it_direct_and_leaves_it_the_persons(self) -> None:
        hold = self.queue_hold()
        hold.hold("Q0007", "tui")
        self.assertRaisesRegex(NotPermittedError, "^The queue was held with Q0007's park by tui;", hold.release_if_by, "Q0007", "mcp")
        self.assertFalse(hold.hold(None, "mcp"))
        self.assertEqual((hold.snapshot().by, hold.snapshot().caller), (None, "tui"))
        self.assertFalse(hold.release_if_by("Q0007", "tui"), "the unpark ended a hold the direct hold made its own")
        self.assertRaises(NotPermittedError, hold.release, "mcp")
        self.assertTrue(hold.release("tui"))

    def test_a_persons_park_makes_an_agents_hold_the_persons(self) -> None:
        hold = self.queue_hold()
        for agents_by, persons_by in ((None, "Q0007"), ("Q0007", "Q0007")):
            with self.subTest(agents_hold_by=agents_by):
                hold.hold(agents_by, "mcp")
                self.assertFalse(hold.hold(persons_by, "tui"))
                self.assertEqual((hold.snapshot().by, hold.snapshot().caller), (agents_by, "tui"))
                self.assertRaises(NotPermittedError, hold.release, "mcp")
                hold.release("tui")

    def test_a_hold_that_changes_hands_says_so_in_its_event(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        hold = QueueHold(self.store.settings, QueueEventPublisher(lambda kind, data: events.append((kind, data))), clock=lambda: NOW)
        hold.hold(None, "mcp")
        hold.hold(None, "mcp")
        hold.hold(None, "cli")
        self.assertEqual([(kind, data["by"], data["caller"]) for kind, data in events], [("queue_held", None, "mcp"), ("queue_held", None, "cli")])

    def test_a_damaged_holds_refusal_says_it_cannot_be_read(self) -> None:
        self.store.settings.set(HOLD_KEY, "{not json")
        with self.assertRaisesRegex(NotPermittedError, "saved hold cannot be read, so it counts as a person's"):
            self.build_worker().release("mcp")

    def test_an_agents_direct_hold_over_its_own_park_becomes_direct(self) -> None:
        hold = self.queue_hold()
        hold.hold("Q0007", "mcp")
        hold.hold(None, "mcp")
        self.assertEqual((hold.snapshot().by, hold.snapshot().caller), (None, "mcp"))
        self.assertFalse(hold.release_if_by("Q0007", "mcp"))
        self.assertTrue(hold.release("mcp"))

    def test_a_hold_saved_before_callers_were_recorded_is_a_persons_not_damaged(self) -> None:
        self.store.settings.set(HOLD_KEY, json.dumps({"since": "2026-09-27T15:30:12", "by": None}))
        hold = read_hold(self.store.settings)
        self.assertEqual((hold.held, hold.damaged, hold.caller), (True, False, None))
        with self.assertRaisesRegex(NotPermittedError, "made before callers were recorded"):
            self.build_worker().release("mcp")

    def test_an_agents_unpark_of_its_entry_a_person_parked_is_refused_with_the_park_and_hold_standing(self) -> None:
        label = self.submit_as("mcp", run_count=2)
        seen: list[Any] = []

        def park_then_unpark() -> None:
            park_entry(self.store, self.worker, self.entry(label).id, caller="tui")
            try:
                unpark_entry(self.store, self.worker, self.entry(label).id, caller="mcp")
            except NotPermittedError as error:
                seen.append(str(error))
            seen.append((self.worker.park_requested(self.entry(label).id), self.worker.hold_state()))

        self.during_run(1, park_then_unpark)
        self.worker.claim_and_run_one()
        self.assertIn("an agent may release only a hold an agent made", seen[0])
        parked, hold = seen[1]
        self.assertEqual((parked, hold.by, hold.caller), (True, label, "tui"))
        self.assertEqual(self.entry(label).state, "parked")

    def test_an_agent_parks_and_unparks_its_own_entry(self) -> None:
        label = self.submit_as("mcp", run_count=2)
        seen: list[Any] = []

        def park_then_unpark() -> None:
            park_entry(self.store, self.worker, self.entry(label).id, caller="mcp")
            seen.append(self.worker.hold_state().caller)
            unpark_entry(self.store, self.worker, self.entry(label).id, caller="mcp")
            seen.append(self.worker.hold_state().held)

        self.during_run(1, park_then_unpark)
        self.worker.claim_and_run_one()
        self.assertEqual(seen, ["mcp", False])
        self.assertEqual(self.entry(label).state, "succeeded")

    def test_an_agent_is_refused_a_persons_entry_before_anything_changes(self) -> None:
        label = self.submit_as("cli", run_count=2)
        seen: list[str] = []

        def try_all() -> None:
            for action in (park_entry, unpark_entry):
                try:
                    action(self.store, self.worker, self.entry(label).id, caller="mcp")
                except NotPermittedError as error:
                    seen.append(str(error))
            seen.append(str(self.worker.park_requested(self.entry(label).id)))

        self.during_run(1, try_all)
        self.worker.claim_and_run_one()
        self.assertEqual(seen, [f"{label} was submitted by cli; an agent may park only an entry an agent submitted", f"{label} was submitted by cli; an agent may unpark only an entry an agent submitted", "False"])
        self.assertFalse(self.worker.hold_state().held)
