"""End-to-end tests for the queue endpoints: submit, list, detail, cancel, resume, rules/limits, and the audit log."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

import yaml
from fastapi.testclient import TestClient

from draw_things_control.core.global_config import ApiLimits
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server import routes_queue
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.server.event_backlog import EventBacklog
from draw_things_control.services.history_delete import DeleteReport, delete_executions
from draw_things_control.services.queue_events import QueueEventPublisher
from draw_things_control.services.queue_hold import HoldState, QueueHold
from draw_things_control.services.queue_submit import submit_job
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import QueueRow, QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor

TOKEN = "a" * 64


class FakeWorker:
    """Stands in for ``QueueWorker`` in the routes' own tests: ``enqueue`` and ``cancel_queued`` mirror the real
    worker's insert-and-publish shape (minus its claim lock, which nothing here contends for) closely enough that a
    route driving either sees the same store state and the same published event."""

    def __init__(self, store: Store, event_backlog: EventBacklog) -> None:
        self._store = store
        self._events = QueueEventPublisher(event_backlog.append)
        self.woken = 0
        self.cancelled: list[int] = []
        self._current_id: int | None = None
        self._current_run: tuple[int, float] | None = None
        self._current_step: tuple[int, int] | None = None
        # Milestone 05: a reservation per entry, the stop asked for one (refusing a park), whether its park has taken
        # effect (refusing an unpark), and the real hold, over the real store.
        self.parking: set[int] = set()
        self.stop_reasons: dict[int, str] = {}
        self.park_taken = False
        self._between_runs_after: int | None = None
        self._hold = QueueHold(store.settings, self._events)

    def is_alive(self) -> bool:
        return True

    def state(self) -> str:
        return "idle"

    def cooldown_until(self) -> float | None:
        return None

    def current_entry_id(self) -> int | None:
        return self._current_id

    def current_run(self) -> tuple[int, float] | None:
        return self._current_run

    def current_step(self) -> tuple[int, int] | None:
        return self._current_step

    def wake(self) -> None:
        self.woken += 1

    def enqueue(self, insert: Callable[[], QueueRow]) -> QueueRow:
        entry = insert()
        self._events.entry_changed(entry.label, entry.state)
        self.wake()
        return entry

    def cancel_queued(self, entry_id: int, label: str) -> bool:
        cancelled = self._store.queue.cancel_queued(entry_id, now=datetime.now())
        if cancelled:
            self._events.entry_changed(label, str(QueueState.CANCELLED))
            self.wake()
        return cancelled

    def cancel_running(self, entry_id: int) -> bool:
        self.cancelled.append(entry_id)
        return True

    def between_runs_after(self) -> int | None:
        return self._between_runs_after

    def park_requested(self, entry_id: int) -> bool:
        return entry_id in self.parking

    def parking_entry_id(self) -> int | None:
        return self._current_id if self._current_id in self.parking else None

    def stop_reason(self, entry_id: int) -> str | None:
        return self.stop_reasons.get(entry_id)

    def park_running(self, entry_id: int, label: str, caller: str | None = None) -> bool:
        if entry_id != self._current_id or entry_id in self.stop_reasons:
            return False
        self._hold.hold(label, caller)
        self.parking.add(entry_id)
        return True

    def unpark_running(self, entry_id: int, label: str, caller: str | None = None) -> bool:
        if entry_id != self._current_id or self.park_taken:
            return False
        self._hold.release_if_by(label, caller)
        self.parking.discard(entry_id)
        return True

    def hold(self, caller: str | None = None) -> tuple[bool, HoldState]:
        return self._hold.hold(None, caller), self._hold.snapshot()

    def release(self, caller: str | None = None) -> tuple[bool, HoldState]:
        return self._hold.release(caller), self._hold.snapshot()

    def hold_state(self) -> HoldState:
        return self._hold.snapshot()

    def delete_executions(self, numbers: Sequence[int], *, dry_run: bool = False) -> DeleteReport:
        return delete_executions(self._store, numbers, dry_run=dry_run)


class QueueRoutesTestCase(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        self.executor: JobExecutor = job_executor(runner_factory=lambda *a: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False)
        self.event_backlog = EventBacklog()
        self.worker = FakeWorker(self.store, self.event_backlog)
        self.client = self.build_client()

    def build_client(self, *, api_limits: Any = None) -> TestClient:
        global_config = replace(self.global_config, api_limits=api_limits) if api_limits is not None else self.global_config
        self.context = ServerContext(paths=self.paths, global_config=global_config, store=self.store, worker=self.worker, executor=self.executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8765, grpc_port=8766, event_backlog=self.event_backlog)  # pyright: ignore[reportArgumentType]  (FakeWorker only needs the methods the queue routes call)
        return TestClient(create_app(self.context), base_url="http://127.0.0.1:8765")

    def request(self, method: str, path: str, **kwargs: Any):
        headers = {"Authorization": f"Bearer {TOKEN}", **kwargs.pop("headers", {})}
        return getattr(self.client, method)(path, headers=headers, **kwargs)

    def write_job_in_catalog(self, name: str = "job.yaml", **changes: Any):
        changes.setdefault("run_timeout_seconds", 60)
        path = self.paths.jobs / name
        path.write_text(yaml.safe_dump(job_data(prompt_pairs=[{"name": "only", "positive": "text"}], **changes), sort_keys=False), encoding="utf-8")
        return path

    def submit(self, name: str = "job.yaml", *, caller: str | None = None, **changes: Any) -> dict[str, Any]:
        self.write_job_in_catalog(name, **changes)
        response = self.request("post", "/v1/queue", json={"job": name}, headers={"X-Dtc-Caller": caller} if caller is not None else {})
        assert response.status_code == 200, response.text
        return response.json()

    def seed_resumable_entry(self, *, caller: str | None = None) -> str:
        """A queued entry submitted, claimed, given three succeeded runs of seven, and left interrupted."""
        entry = self.submit("chain.yaml", caller=caller, run_count=7)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        last_frame = self.output_directory / "last-frame-3.png"
        last_frame.parent.mkdir(parents=True, exist_ok=True)
        last_frame.write_bytes(b"png")
        execution_row = self.store.executions.start(NewExecution(job_name="sunset-walk", job_file="chain.yaml", mode="i2v", started_at="2026-09-28T10:00:00+00:00", seed=42, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        for number in (1, 2, 3):
            self.store.executions.start_run(execution_row, number, NewRun(pair="only", positive="text", started_at="2026-09-28T10:00:00+00:00", status="succeeded", output=f"run-{number}.mov", last_frame=last_frame.name if number == 3 else None))
        self.store.executions.finish(execution_row, status="interrupted", exit_code=None, signal=None, finished_at="2026-09-28T10:10:00+00:00")
        execution_number = self.store.executions.number_of(execution_row)
        assert execution_number is not None
        self.store.queue.link_execution(claimed.id, execution_number)
        self.store.queue.finish(claimed.id, state=QueueState.INTERRUPTED, finished_at="2026-09-28T10:10:00+00:00")
        return entry["queue_id"]


class SubmitTests(QueueRoutesTestCase):
    def test_a_valid_submission_is_queued_and_wakes_the_worker(self) -> None:
        entry = self.submit(run_count=1)
        self.assertEqual((entry["queue_id"], entry["state"]), ("Q0001", "queued"))
        self.assertEqual((entry["total_runs"], entry["succeeded"]), (1, 0))
        self.assertEqual(self.worker.woken, 1)
        listed = self.request("get", "/v1/queue").json()["queue"]
        self.assertEqual([row["queue_id"] for row in listed], ["Q0001"])
        self.assertEqual((listed[0]["total_runs"], listed[0]["succeeded"]), (1, 0))

    def test_a_job_without_run_timeout_seconds_is_refused(self) -> None:
        self.write_job_in_catalog("no-timeout.yaml", run_timeout_seconds=None)
        response = self.request("post", "/v1/queue", json={"job": "no-timeout.yaml"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "timeout_required")
        self.assertEqual(self.store.queue.list(), [])

    def test_a_job_over_max_job_runs_is_refused(self) -> None:
        self.client = self.build_client(api_limits=ApiLimits(max_job_runs=3))
        self.write_job_in_catalog("too-many.yaml", run_count=7)
        response = self.request("post", "/v1/queue", json={"job": "too-many.yaml"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "limit_exceeded")

    def test_the_queue_length_limit_is_enforced(self) -> None:
        self.client = self.build_client(api_limits=ApiLimits(max_queued_jobs=1))
        self.submit("first.yaml", run_count=1)
        self.write_job_in_catalog("second.yaml", run_count=1)
        response = self.request("post", "/v1/queue", json={"job": "second.yaml"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "limit_exceeded")

    def test_the_submission_lock_serializes_a_submissions_check_and_insert(self) -> None:
        """Another submission already mid check-then-insert, simulated by holding the lock externally: a concurrent
        submission must wait for it rather than reading the queued count while it is still stale (the race that let
        two concurrent submissions both pass ``max_queued_jobs`` before either had inserted)."""
        self.client = self.build_client(api_limits=ApiLimits(max_queued_jobs=1))
        self.write_job_in_catalog("first.yaml")
        self.context.submission_lock.acquire()
        result: dict[str, Any] = {}

        def submit() -> None:
            result["response"] = self.request("post", "/v1/queue", json={"job": "first.yaml"})

        thread = threading.Thread(target=submit)
        thread.start()
        thread.join(timeout=0.3)
        self.assertTrue(thread.is_alive(), "the submission completed without waiting for the submission lock")

        self.context.submission_lock.release()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "the submission never resumed once the lock was released")
        self.assertEqual(result["response"].status_code, 200, result["response"].text)

    def test_an_unknown_job_reference_is_not_found_and_stores_nothing(self) -> None:
        response = self.request("post", "/v1/queue", json={"job": "nope.yaml"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.queue.list(), [])

    def test_every_submission_is_audited_accepted_and_refused_alike(self) -> None:
        self.submit(run_count=1)
        self.request("post", "/v1/queue", json={"job": "nope.yaml"})
        audit = self.request("get", "/v1/audit").json()["audit"]
        self.assertEqual([(row["action"], row["outcome"], row["target"]) for row in audit], [("submit", "not_found", "nope.yaml"), ("submit", "ok", "job.yaml")])

    def test_a_submission_publishes_its_queued_entry_to_the_event_backlog(self) -> None:
        start = self.context.event_backlog.latest_id()
        entry = self.submit(run_count=1)
        events = self.context.event_backlog.since(start)
        assert events is not None
        self.assertEqual([(event.kind, json.loads(event.data_json)) for event in events], [("queue_entry_changed", {"queue_id": entry["queue_id"], "state": "queued"})])

    def test_the_rules_are_checked_on_the_text_that_is_stored(self) -> None:
        """A job file edited right after the rules passed cannot change what is stored: the rules run on the job
        parsed from the exact text submit_job stores, not on an earlier read of the file."""
        path = self.write_job_in_catalog("job.yaml")
        real_check = routes_queue.check_api_rules

        def check_then_edit(*args: Any, **kwargs: Any) -> None:
            real_check(*args, **kwargs)
            path.write_text(yaml.safe_dump(job_data(prompt_pairs=[{"name": "only", "positive": "text"}]), sort_keys=False), encoding="utf-8")

        routes_queue.check_api_rules = check_then_edit  # type: ignore[assignment]
        try:
            response = self.request("post", "/v1/queue", json={"job": "job.yaml"})
        finally:
            routes_queue.check_api_rules = real_check  # type: ignore[assignment]
        self.assertEqual(response.status_code, 200, response.text)
        (stored,) = self.store.queue.list()
        self.assertIn("run_timeout_seconds", stored.job_text)

    def test_a_body_fastapi_refuses_gets_the_stable_error_shape(self) -> None:
        response = self.request("post", "/v1/queue", json={})
        self.assertEqual(response.status_code, 422)
        self.assertEqual((response.json()["code"], response.json()["field"]), ("invalid_input", "job"))

    def test_a_body_fastapi_refuses_is_still_audited_with_the_default_caller(self) -> None:
        """The audit fix (Milestone 02, phase-3 changelog 2026-09-28): a body FastAPI's own validation refuses
        never reaches post_queue's body -- and so never opens its own audited() block -- but the spec still calls
        for one entry per request, refused ones included."""
        self.request("post", "/v1/queue", json={})
        row = self.request("get", "/v1/audit").json()["audit"][0]
        self.assertEqual((row["action"], row["target"], row["outcome"], row["caller"]), ("submit", None, "invalid_input", "api"))

    def test_an_unauthenticated_submission_leaves_no_audit_row(self) -> None:
        self.write_job_in_catalog("job.yaml")
        self.client.post("/v1/queue", json={"job": "job.yaml"})  # no Authorization header
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"], [])

    def test_an_unauthenticated_malformed_submission_also_leaves_no_audit_row(self) -> None:
        self.client.post("/v1/queue", json={})  # neither a valid Authorization header nor a valid body
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"], [])

    def test_a_malformed_submission_with_a_known_caller_records_that_caller_not_api(self) -> None:
        """Headers are parsed independently of the body: a malformed body's own audit row still names the real
        caller when ``X-Dtc-Caller`` itself is valid, not the default it falls back to only when that header
        isn't."""
        self.request("post", "/v1/queue", json={}, headers={"X-Dtc-Caller": "tui"})
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"][0]["caller"], "tui")

    def test_body_that_is_not_json_at_all_is_still_audited_when_authenticated(self) -> None:
        """``json={}`` above is valid JSON missing a field; this is the other shape of body FastAPI's validation
        refuses (unparsable JSON), reaching ``RequestValidationError`` a different way (a ``json_invalid`` problem,
        not a missing-field one) -- exercised separately since it could in principle reach the route differently."""
        response = self.request("post", "/v1/queue", content=b'{"job": ', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        row = self.request("get", "/v1/audit").json()["audit"][0]
        self.assertEqual((row["action"], row["target"], row["outcome"], row["caller"]), ("submit", None, "invalid_input", "api"))

    def test_body_that_is_not_json_at_all_and_unauthenticated_leaves_no_audit_row(self) -> None:
        self.client.post("/v1/queue", content=b'{"job": ', headers={"Content-Type": "application/json"})  # no Authorization header
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"], [])


class CallerHeaderTests(QueueRoutesTestCase):
    def test_a_known_caller_is_recorded_and_an_unknown_one_is_refused(self) -> None:
        self.write_job_in_catalog("job.yaml")
        response = self.request("post", "/v1/queue", json={"job": "job.yaml"}, headers={"X-Dtc-Caller": "tui"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"][0]["caller"], "tui")
        bad = self.request("post", "/v1/queue", json={"job": "job.yaml"}, headers={"X-Dtc-Caller": "browser"})
        self.assertEqual(bad.status_code, 422)

    def test_an_unknown_caller_is_audited_with_the_default_caller_not_the_bad_value(self) -> None:
        self.write_job_in_catalog("job.yaml")
        self.request("post", "/v1/queue", json={"job": "job.yaml"}, headers={"X-Dtc-Caller": "browser"})
        row = self.request("get", "/v1/audit").json()["audit"][0]
        self.assertEqual((row["action"], row["target"], row["outcome"], row["caller"]), ("submit", "job.yaml", "invalid_input", "api"))
        self.assertEqual(self.store.queue.list(), [])

    def test_absent_caller_defaults_to_api(self) -> None:
        self.submit(run_count=1)
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"][0]["caller"], "api")


class CancelTests(QueueRoutesTestCase):
    def test_cancelling_a_queued_entry(self) -> None:
        entry = self.submit(run_count=1)
        response = self.request("post", f"/v1/queue/{entry['queue_id']}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "cancelled")
        row = self.request("get", "/v1/audit").json()["audit"][0]
        self.assertEqual((row["action"], row["target"], row["outcome"], row["caller"]), ("cancel", entry["queue_id"], "ok", "api"))

    def test_cancelling_a_queued_entry_publishes_its_change(self) -> None:
        entry = self.submit(run_count=1)
        start = self.context.event_backlog.latest_id()
        self.request("post", f"/v1/queue/{entry['queue_id']}/cancel")
        events = self.context.event_backlog.since(start)
        assert events is not None
        self.assertEqual([json.loads(event.data_json) for event in events], [{"queue_id": entry["queue_id"], "state": "cancelled"}])

    def test_cancelling_an_already_finished_entry_is_refused_and_audited(self) -> None:
        entry = self.submit(run_count=1)
        self.request("post", f"/v1/queue/{entry['queue_id']}/cancel")
        response = self.request("post", f"/v1/queue/{entry['queue_id']}/cancel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_state")
        outcomes = [(row["action"], row["outcome"]) for row in self.request("get", "/v1/audit").json()["audit"]]
        self.assertEqual(outcomes, [("cancel", "invalid_state"), ("cancel", "ok"), ("submit", "ok")])

    def test_cancelling_an_unknown_entry_is_not_found(self) -> None:
        self.assertEqual(self.request("post", "/v1/queue/Q9999/cancel").status_code, 404)


class ResumeTests(QueueRoutesTestCase):
    def test_resuming_an_interrupted_entry_creates_a_new_queued_one(self) -> None:
        original = self.seed_resumable_entry()
        response = self.request("post", f"/v1/queue/{original}/resume")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["queue_id"], body["state"], body["resumes"]), ("Q0002", "queued", "Q0001"))
        self.assertEqual(self.worker.woken, 2)  # once for the submit, once for the resume

    def test_the_submission_lock_serializes_a_resumes_check_and_insert(self) -> None:
        """Same guarantee as ``SubmitTests``' equivalent test, for ``POST /queue/{id}/resume``: it shares one
        ``submission_lock`` with ``POST /queue`` so the two kinds of insert cannot race each other's
        ``max_queued_jobs`` check either."""
        original = self.seed_resumable_entry()
        self.context.submission_lock.acquire()
        result: dict[str, Any] = {}

        def resume() -> None:
            result["response"] = self.request("post", f"/v1/queue/{original}/resume")

        thread = threading.Thread(target=resume)
        thread.start()
        thread.join(timeout=0.3)
        self.assertTrue(thread.is_alive(), "the resume completed without waiting for the submission lock")

        self.context.submission_lock.release()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "the resume never resumed once the lock was released")
        self.assertEqual(result["response"].status_code, 200, result["response"].text)

    def test_the_queue_entry_detail_reports_resumability(self) -> None:
        original = self.seed_resumable_entry()
        detail = self.request("get", f"/v1/queue/{original}").json()
        self.assertEqual((detail["resumable"], detail["resume_from_run"]), (True, 4))

    def test_a_non_resumable_entrys_detail_names_the_reason(self) -> None:
        entry = self.submit(run_count=1)
        detail = self.request("get", f"/v1/queue/{entry['queue_id']}").json()
        self.assertFalse(detail["resumable"])
        self.assertIn("queued", detail["resume_refused_reason"])

    def test_the_entry_the_worker_is_currently_running_reports_its_run_and_step(self) -> None:
        entry = self.submit(run_count=1)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        self.worker._current_id = claimed.id
        self.worker._current_run = (1, 12.5)
        self.worker._current_step = (7, 20)
        detail = self.request("get", f"/v1/queue/{entry['queue_id']}").json()
        self.assertEqual((detail["current_run"], detail["current_run_elapsed_seconds"]), (1, 12.5))
        self.assertEqual((detail["current_step"], detail["current_step_total"]), (7, 20))

    def test_a_different_entrys_current_run_and_step_are_not_reported(self) -> None:
        entry = self.submit(run_count=1)
        self.worker._current_id = 999999
        self.worker._current_run = (1, 12.5)
        self.worker._current_step = (7, 20)
        detail = self.request("get", f"/v1/queue/{entry['queue_id']}").json()
        self.assertEqual((detail["current_run"], detail["current_run_elapsed_seconds"]), (None, None))
        self.assertEqual((detail["current_step"], detail["current_step_total"]), (None, None))

    def test_resuming_a_job_that_would_now_exceed_a_limit_is_refused_and_stores_nothing(self) -> None:
        original = self.seed_resumable_entry()
        self.client = self.build_client(api_limits=ApiLimits(max_job_runs=2))
        response = self.request("post", f"/v1/queue/{original}/resume")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.request("get", f"/v1/queue/{original}").json()["resumable"], True)

    def test_resuming_an_unknown_entry_is_not_found(self) -> None:
        self.assertEqual(self.request("post", "/v1/queue/Q9999/resume").status_code, 404)


class QueueListingTests(QueueRoutesTestCase):
    """``GET /v1/queue``'s paging fix (Milestone 02, phase-3 changelog 2026-09-28): active entries always come back
    in full, and the finished ones page, newest first, after them."""

    def finish(self, name: str, state: QueueState) -> str:
        """Submit ``name`` and finish it directly, straight from ``queued``: unlike ``seed_resumable_entry``, this
        never goes through ``claim_oldest`` (which would claim whichever entry is actually oldest across the whole
        table, not necessarily the one just submitted, when an earlier one is still queued)."""
        entry = self.submit(name, run_count=1)
        row = self.store.queue.by_number(int(entry["queue_id"][1:]))
        assert row is not None
        self.store.queue.finish(row.id, state=state, finished_at="2026-09-28T10:00:00+00:00")
        return entry["queue_id"]

    def test_active_entries_are_returned_in_full_regardless_of_limit(self) -> None:
        ids = [self.submit(f"job-{i}.yaml", run_count=1)["queue_id"] for i in range(3)]
        body = self.request("get", "/v1/queue", params={"limit": 1}).json()
        self.assertEqual([row["queue_id"] for row in body["queue"]], ids)
        self.assertIsNone(body["cursor"])

    def test_finished_entries_page_newest_first_after_the_active_ones(self) -> None:
        active = self.submit("active.yaml", run_count=1)["queue_id"]
        first = self.finish("first.yaml", QueueState.SUCCEEDED)
        second = self.finish("second.yaml", QueueState.FAILED)
        third = self.finish("third.yaml", QueueState.CANCELLED)
        page_one = self.request("get", "/v1/queue", params={"limit": 2}).json()
        self.assertEqual([row["queue_id"] for row in page_one["queue"]], [active, third, second])
        self.assertIsNotNone(page_one["cursor"])
        page_two = self.request("get", "/v1/queue", params={"limit": 2, "cursor": page_one["cursor"]}).json()
        self.assertEqual([row["queue_id"] for row in page_two["queue"]], [first])
        self.assertIsNone(page_two["cursor"])

    def test_a_finished_state_filter_is_paged(self) -> None:
        self.finish("a.yaml", QueueState.SUCCEEDED)
        self.finish("b.yaml", QueueState.SUCCEEDED)
        page = self.request("get", "/v1/queue", params={"state": "succeeded", "limit": 1}).json()
        self.assertEqual(len(page["queue"]), 1)
        self.assertIsNotNone(page["cursor"])

    def test_an_active_state_filter_is_unpaged(self) -> None:
        ids = [self.submit(f"job-{i}.yaml", run_count=1)["queue_id"] for i in range(3)]
        self.finish("done.yaml", QueueState.SUCCEEDED)
        body = self.request("get", "/v1/queue", params={"state": "queued", "limit": 1}).json()
        self.assertEqual([row["queue_id"] for row in body["queue"]], ids)
        self.assertIsNone(body["cursor"])

    def test_an_active_state_filter_the_plain_listing_and_the_detail_agree_on_succeeded(self) -> None:
        """The bug an active-state filter's own read (state/queue.py's plain, unjoined ``list()``) had until it was
        given the same join ``list_active`` and ``by_number`` already carry: it always reported ``succeeded`` as 0
        for a linked, running entry, disagreeing with the other two reads of the very same entry."""
        entry = self.submit("running.yaml", run_count=3)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        execution_row = self.store.executions.start(NewExecution(job_name="running", job_file="running.yaml", mode="i2v", started_at="2026-09-28T10:00:00+00:00", total_runs=3, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        self.store.executions.start_run(execution_row, 1, NewRun(pair="only", positive="text", started_at="2026-09-28T10:00:00+00:00", status="succeeded", output="run-1.mov"))
        execution_number = self.store.executions.number_of(execution_row)
        assert execution_number is not None
        self.store.queue.link_execution(claimed.id, execution_number)
        self.worker._current_id = claimed.id

        by_state = self.request("get", "/v1/queue", params={"state": "running"}).json()["queue"]
        plain = self.request("get", "/v1/queue").json()["queue"]
        detail = self.request("get", f"/v1/queue/{entry['queue_id']}").json()
        self.assertEqual(by_state[0]["succeeded"], 1)
        self.assertEqual(next(row["succeeded"] for row in plain if row["queue_id"] == entry["queue_id"]), 1)
        self.assertEqual(detail["succeeded"], 1)


class ParkAndHoldTests(QueueRoutesTestCase):
    """Milestone 05: ``POST /v1/queue/{queue_id}/park`` and ``/unpark``, ``POST /v1/queue/hold`` and ``/release``."""

    def running_entry(self) -> dict[str, Any]:
        entry = self.submit(run_count=3)
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None
        self.worker._current_id = claimed.id
        self.worker._current_run = (2, 5.0)
        return entry

    def audit(self) -> list[tuple[str, str | None, str, str]]:
        return [(row["action"], row["target"], row["outcome"], row["caller"]) for row in self.request("get", "/v1/audit").json()["audit"]]

    def test_parking_a_running_entry_holds_the_queue_and_returns_the_entry(self) -> None:
        queue_id = self.running_entry()["queue_id"]
        response = self.request("post", f"/v1/queue/{queue_id}/park", headers={"X-Dtc-Caller": "tui"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["queue_id"], body["state"], body["park_requested"], body["current_run"], body["total_runs"]), (queue_id, "running", True, 2, 3))
        self.assertEqual((body["held"], body["held_by"], body["held_since"] is not None), (True, queue_id, True))
        self.assertIn("between_runs_after_run", body)
        self.assertEqual(self.audit()[0], ("park", queue_id, "ok", "tui"))
        listing = self.request("get", "/v1/queue").json()
        self.assertEqual((listing["held"], listing["held_by"], listing["queue"][0]["park_requested"]), (True, queue_id, True))
        self.assertTrue(self.request("get", f"/v1/queue/{queue_id}").json()["park_requested"])

    def test_unparking_releases_the_hold_the_reservation_made(self) -> None:
        queue_id = self.running_entry()["queue_id"]
        self.request("post", f"/v1/queue/{queue_id}/park")
        body = self.request("post", f"/v1/queue/{queue_id}/unpark").json()
        self.assertEqual((body["park_requested"], body["held"], body["held_by"]), (False, False, None))
        self.assertEqual(self.audit()[0], ("unpark", queue_id, "ok", "api"))

    def test_an_unpark_after_the_park_took_effect_is_refused(self) -> None:
        queue_id = self.running_entry()["queue_id"]
        self.request("post", f"/v1/queue/{queue_id}/park")
        self.worker.park_taken = True
        response = self.request("post", f"/v1/queue/{queue_id}/unpark")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "invalid_state"))
        self.assertIn(f"{queue_id} has already parked", response.json()["message"])

    def test_parking_a_queued_finished_cancelling_or_unknown_entry_is_refused_and_audited(self) -> None:
        running = self.running_entry()["queue_id"]
        self.worker.stop_reasons[self.worker._current_id or 0] = "cancel"
        queued = self.submit("queued.yaml", run_count=1)["queue_id"]
        for queue_id, words in ((queued, "it is queued"), (running, "it is being cancelled")):
            response = self.request("post", f"/v1/queue/{queue_id}/park")
            self.assertEqual((response.status_code, response.json()["code"]), (409, "invalid_state"))
            self.assertIn(words, response.json()["message"])
        self.assertEqual(self.request("post", "/v1/queue/Q9999/park").status_code, 404)
        self.assertEqual([row[2] for row in self.audit()[:3]], ["not_found", "invalid_state", "invalid_state"])
        self.assertFalse(self.request("get", "/v1/queue").json()["held"])

    def test_hold_and_release_report_the_hold_and_whether_it_changed(self) -> None:
        body = self.request("post", "/v1/queue/hold").json()
        self.assertEqual((body["held"], body["held_by"], body["changed"], body["held_since"] is not None), (True, None, True, True))
        self.assertFalse(self.request("post", "/v1/queue/hold").json()["changed"])
        listing = self.request("get", "/v1/queue").json()
        self.assertEqual((listing["held"], listing["held_since"]), (True, body["held_since"]))
        self.assertEqual(self.request("post", "/v1/queue/release").json(), {"held": False, "held_since": None, "held_by": None, "hold_caller": None, "changed": True})
        self.assertFalse(self.request("post", "/v1/queue/release").json()["changed"])
        self.assertEqual([(action, target) for action, target, _outcome, _caller in self.audit()], [("release", None), ("release", None), ("hold", None), ("hold", None)])

    def test_hold_and_release_refuse_an_unknown_caller_and_audit_it(self) -> None:
        response = self.request("post", "/v1/queue/hold", headers={"X-Dtc-Caller": "nope"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.audit()[0][:3], ("hold", None, "invalid_input"))
        self.assertFalse(self.request("get", "/v1/queue").json()["held"])

    def test_parked_entries_are_listed_and_filtered_with_the_finished_ones(self) -> None:
        entry = self.submit(run_count=3)
        row = self.store.queue.by_number(int(entry["queue_id"][1:]))
        assert row is not None
        self.store.queue.finish(row.id, state=QueueState.PARKED, finished_at="2026-09-28T10:00:00+00:00")
        listed = self.request("get", "/v1/queue", params={"state": "parked"}).json()["queue"]
        self.assertEqual([(item["queue_id"], item["state"], item["park_requested"]) for item in listed], [(entry["queue_id"], "parked", False)])

    def test_no_response_or_audit_row_carries_the_token(self) -> None:
        queue_id = self.running_entry()["queue_id"]
        texts = [self.request("post", f"/v1/queue/{queue_id}/park").text, self.request("post", "/v1/queue/hold").text, self.request("post", "/v1/queue/release").text, self.request("post", f"/v1/queue/{queue_id}/unpark").text, self.request("get", "/v1/audit").text]
        self.assertFalse(any(TOKEN in text for text in texts))


class AgentLimitTests(QueueRoutesTestCase):
    """Milestone 10: each entry records its submitter and the hold its caller, and the API keeps an agent
    (``X-Dtc-Caller: mcp``) off the entries and holds a person made."""

    AGENT = {"X-Dtc-Caller": "mcp"}

    def running(self, entry: dict[str, Any]) -> str:
        claimed = self.store.queue.claim_oldest(datetime.now())
        assert claimed is not None and claimed.label == entry["queue_id"]
        self.worker._current_id = claimed.id
        return entry["queue_id"]

    def agent(self, path: str) -> Any:
        return self.request("post", path, headers=self.AGENT)

    def audit(self) -> list[tuple[str, str | None, str, str]]:
        return [(row["action"], row["target"], row["outcome"], row["caller"]) for row in self.request("get", "/v1/audit").json()["audit"]]

    def assert_not_permitted(self, response: Any, words: str) -> None:
        self.assertEqual((response.status_code, response.json()["code"]), (403, "not_permitted"), response.text)
        self.assertIn(words, response.json()["message"])

    def test_a_resume_records_its_resumer_as_the_new_entrys_submitter(self) -> None:
        queue_id = self.seed_resumable_entry(caller="mcp")
        resumed = self.request("post", f"/v1/queue/{queue_id}/resume", headers={"X-Dtc-Caller": "cli"})
        self.assertEqual(resumed.json()["submitted_by"], "cli", resumed.text)

    def test_each_entry_records_its_submitter(self) -> None:
        agents = self.submit("agents.yaml", caller="mcp", run_count=1)
        persons = self.submit("persons.yaml", run_count=1)
        self.assertEqual((agents["submitted_by"], persons["submitted_by"]), ("mcp", "api"))
        listed = {row["queue_id"]: row["submitted_by"] for row in self.request("get", "/v1/queue").json()["queue"]}
        self.assertEqual(listed, {agents["queue_id"]: "mcp", persons["queue_id"]: "api"})
        self.assertEqual(self.request("get", f"/v1/queue/{agents['queue_id']}").json()["submitted_by"], "mcp")

    def test_an_agent_is_refused_a_persons_entry_and_nothing_changes(self) -> None:
        queue_id = self.running(self.submit(caller="tui", run_count=3))
        self.assert_not_permitted(self.agent(f"/v1/queue/{queue_id}/cancel"), f"{queue_id} was submitted by tui; an agent may cancel only an entry an agent submitted")
        self.assert_not_permitted(self.agent(f"/v1/queue/{queue_id}/park"), "an agent may park only")
        self.assertEqual((self.worker.cancelled, self.worker.parking), ([], set()))
        self.request("post", f"/v1/queue/{queue_id}/park", headers={"X-Dtc-Caller": "tui"})
        self.assert_not_permitted(self.agent(f"/v1/queue/{queue_id}/unpark"), "an agent may unpark only")
        self.assert_not_permitted(self.agent("/v1/queue/release"), f"The queue was held with {queue_id}'s park by tui; an agent may release only a hold an agent made")
        detail = self.request("get", f"/v1/queue/{queue_id}").json()
        self.assertEqual((detail["state"], detail["park_requested"], detail["held"], detail["held_by"], detail["hold_caller"]), ("running", True, True, queue_id, "tui"))
        resumable = self.seed_resumable_entry(caller="cli")
        self.assert_not_permitted(self.agent(f"/v1/queue/{resumable}/resume"), "an agent may resume only")
        self.assertEqual([row.label for row in self.store.queue.list()], [queue_id, resumable])
        refused = [row for row in self.audit() if row[2] == "not_permitted"]
        self.assertEqual(sorted((action, caller) for action, _target, _outcome, caller in refused), [("cancel", "mcp"), ("park", "mcp"), ("release", "mcp"), ("resume", "mcp"), ("unpark", "mcp")])

    def test_an_entry_from_before_submitters_were_recorded_counts_as_a_persons(self) -> None:
        entry = submit_job(self.write_job_in_catalog("old.yaml", run_count=1), self.global_config, self.paths.params, self.store)
        self.assertIsNone(self.request("get", f"/v1/queue/{entry.label}").json()["submitted_by"])
        self.assert_not_permitted(self.agent(f"/v1/queue/{entry.label}/cancel"), "made before callers were recorded")
        self.assertEqual(self.request("get", f"/v1/queue/{entry.label}").json()["state"], "queued")

    def test_an_agent_acts_on_its_own_entries_and_holds(self) -> None:
        queue_id = self.running(self.submit(caller="mcp", run_count=3))
        parked = self.agent(f"/v1/queue/{queue_id}/park").json()
        self.assertEqual((parked["park_requested"], parked["held_by"], parked["hold_caller"]), (True, queue_id, "mcp"))
        self.assertEqual(self.agent(f"/v1/queue/{queue_id}/unpark").json()["held"], False)
        self.assertEqual(self.agent("/v1/queue/hold").json()["hold_caller"], "mcp")
        self.assertEqual(self.agent("/v1/queue/release").json()["changed"], True)
        self.assertEqual(self.agent(f"/v1/queue/{queue_id}/cancel").status_code, 200)
        self.assertEqual(len(self.worker.cancelled), 1)
        resumable = self.seed_resumable_entry(caller="mcp")
        self.assertEqual(self.agent(f"/v1/queue/{resumable}/resume").json()["submitted_by"], "mcp")
        self.assertEqual({outcome for _action, _target, outcome, _caller in self.audit()}, {"ok"})

    def test_a_persons_commands_on_an_agents_entry_and_hold_work(self) -> None:
        queue_id = self.running(self.submit(caller="mcp", run_count=3))
        self.agent("/v1/queue/hold")
        self.assertEqual(self.request("post", f"/v1/queue/{queue_id}/cancel", headers={"X-Dtc-Caller": "cli"}).status_code, 200)
        self.assertEqual(self.request("post", "/v1/queue/release", headers={"X-Dtc-Caller": "cli"}).json()["changed"], True)

    def test_an_agents_direct_hold_over_a_persons_leaves_it_the_persons(self) -> None:
        self.request("post", "/v1/queue/hold", headers={"X-Dtc-Caller": "tui"})
        body = self.agent("/v1/queue/hold").json()
        self.assertEqual((body["changed"], body["hold_caller"]), (False, "tui"))
        self.assert_not_permitted(self.agent("/v1/queue/release"), "The queue was held by tui")
        self.assertEqual(self.request("get", "/v1/queue").json()["hold_caller"], "tui")

    def test_a_persons_park_on_a_queue_an_agent_holds_makes_the_hold_the_persons(self) -> None:
        queue_id = self.running(self.submit(caller="mcp", run_count=3))
        self.agent("/v1/queue/hold")
        parked = self.request("post", f"/v1/queue/{queue_id}/park", headers={"X-Dtc-Caller": "tui"}).json()
        self.assertEqual((parked["held"], parked["held_by"], parked["hold_caller"]), (True, None, "tui"))
        self.assert_not_permitted(self.agent("/v1/queue/release"), "The queue was held by tui")
        self.assertTrue(self.request("get", "/v1/queue").json()["held"])


if __name__ == "__main__":
    import unittest

    unittest.main()
