"""End-to-end tests for the queue endpoints: submit, list, detail, cancel, resume, rules/limits, and the audit log."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from typing import Any

import yaml
from fastapi.testclient import TestClient

from draw_things_control.core.global_config import ApiLimits
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.queue import QueueState
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor

TOKEN = "a" * 64


class FakeWorker:
    def __init__(self) -> None:
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
        self.worker = FakeWorker()
        self.client = self.build_client()

    def build_client(self, *, api_limits: Any = None) -> TestClient:
        global_config = replace(self.global_config, api_limits=api_limits) if api_limits is not None else self.global_config
        self.context = ServerContext(paths=self.paths, global_config=global_config, store=self.store, worker=self.worker, executor=self.executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8765)  # pyright: ignore[reportArgumentType]  (FakeWorker only needs the methods the queue routes call)
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
        self.assertEqual(self.worker.woken, 1)
        listed = self.request("get", "/v1/queue").json()["queue"]
        self.assertEqual([row["queue_id"] for row in listed], ["Q0001"])

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

    def test_an_unauthenticated_submission_leaves_no_audit_row(self) -> None:
        self.write_job_in_catalog("job.yaml")
        self.client.post("/v1/queue", json={"job": "job.yaml"})  # no Authorization header
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"], [])


class CallerHeaderTests(QueueRoutesTestCase):
    def test_a_known_caller_is_recorded_and_an_unknown_one_is_refused(self) -> None:
        self.write_job_in_catalog("job.yaml")
        response = self.request("post", "/v1/queue", json={"job": "job.yaml"}, headers={"X-Dtc-Caller": "tui"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request("get", "/v1/audit").json()["audit"][0]["caller"], "tui")
        bad = self.request("post", "/v1/queue", json={"job": "job.yaml"}, headers={"X-Dtc-Caller": "browser"})
        self.assertEqual(bad.status_code, 422)

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


if __name__ == "__main__":
    import unittest

    unittest.main()
