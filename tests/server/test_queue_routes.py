"""End-to-end tests for the queue endpoints: submit, list, detail, cancel, resume, rules/limits, and the audit log."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
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
from draw_things_control.services.queue_events import QueueEventPublisher
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

    def submit(self, name: str = "job.yaml", **changes: Any) -> dict[str, Any]:
        self.write_job_in_catalog(name, **changes)
        response = self.request("post", "/v1/queue", json={"job": name})
        assert response.status_code == 200, response.text
        return response.json()

    def seed_resumable_entry(self) -> str:
        """A queued entry submitted, claimed, given three succeeded runs of seven, and left interrupted."""
        entry = self.submit("chain.yaml", run_count=7)
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


if __name__ == "__main__":
    import unittest

    unittest.main()
