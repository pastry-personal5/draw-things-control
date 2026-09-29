"""Tests for ``POST /v1/executions/delete`` (Milestone 06): each execution's outcome, the refusals of a request as a
whole, the audit log, the race with a resume, and ``GET /v1/executions?name=``."""

from __future__ import annotations

import threading
from typing import Any

from draw_things_control.server import routes_queue
from tests.server.test_queue_routes import QueueRoutesTestCase


class DeleteRouteTests(QueueRoutesTestCase):
    def delete(self, *executions: str, dry_run: bool = False, **kwargs: Any):
        return self.request("post", "/v1/executions/delete", json={"executions": list(executions), "dry_run": dry_run}, **kwargs)

    def audit(self) -> list[tuple[str, str | None, str, str]]:
        return [(row["action"], row["target"], row["outcome"], row["caller"]) for row in reversed(self.request("get", "/v1/audit").json()["audit"])]

    def test_each_execution_has_its_outcome_and_its_own_audit_row(self) -> None:
        original = self.seed_resumable_entry()
        self.request("post", f"/v1/queue/{original}/resume")
        response = self.delete("E0001", "e2", headers={"X-Dtc-Caller": "cli"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"dry_run": False, "deleted": [], "refused": [{"execution_id": "E0001", "reason": "Q0002 is queued to resume from E0001"}], "missing": ["E0002"], "resumes_ended": [], "manifests_kept": []})
        self.request("post", "/v1/queue/Q0002/cancel")
        body = self.delete("E0001", "E0001").json()
        # The cancelled resume never ran, so Q0002 would still resume from E0001: the deletion ends it.
        self.assertEqual((body["deleted"], body["resumes_ended"]), (["E0001"], [{"execution_id": "E0001", "queue_ids": ["Q0002"]}]))
        deletes = [row for row in self.audit() if row[0] == "delete_execution"]
        self.assertEqual(deletes, [("delete_execution", "E0001", "invalid_state", "cli"), ("delete_execution", "E0002", "not_found", "cli"), ("delete_execution", "E0001", "ok", "api")])

    def test_a_dry_run_deletes_nothing_and_writes_no_audit_row(self) -> None:
        self.seed_resumable_entry()
        body = self.delete("E0001", dry_run=True).json()
        self.assertEqual((body["dry_run"], body["deleted"], body["resumes_ended"]), (True, ["E0001"], [{"execution_id": "E0001", "queue_ids": ["Q0001"]}]))
        self.assertIsNotNone(self.store.executions.by_number(1))
        self.assertEqual(self.delete(dry_run=True).status_code, 422)
        self.assertEqual([row for row in self.audit() if row[0] == "delete_execution"], [])

    def test_a_request_refused_as_a_whole_has_one_audit_row(self) -> None:
        cases = [
            (self.delete(), "executions"),
            (self.delete(*[f"E{number}" for number in range(1, 202)]), "executions"),
            (self.delete("E0001", "Q0001"), "executions"),
            (self.delete("E0001", headers={"X-Dtc-Caller": "nobody"}), "X-Dtc-Caller"),
        ]
        for response, field in cases:
            self.assertEqual((response.status_code, response.json()["code"], response.json()["field"]), (422, "invalid_input", field))
        self.assertEqual(self.audit(), [("delete_execution", None, "invalid_input", "api")] * 4)

    def test_a_body_fastapi_refuses_is_audited_and_an_unauthenticated_one_is_not(self) -> None:
        self.assertEqual(self.request("post", "/v1/executions/delete", json={"executions": "E0001"}, headers={"X-Dtc-Caller": "tui"}).status_code, 422)
        self.assertEqual(self.request("post", "/v1/executions/delete", content=b"not json", headers={"Content-Type": "application/json"}).status_code, 422)
        self.client.post("/v1/executions/delete", json={"executions": ["E0001"]})
        self.assertEqual(self.audit(), [("delete_execution", None, "invalid_input", "tui"), ("delete_execution", None, "invalid_input", "api")])

    def test_a_deletion_racing_a_resume_waits_for_its_insert_and_is_refused(self) -> None:
        """The delete lands after the resume resolved its chain and before its insert: the submission lock keeps it
        out until the insert, and the queued resume then uses the execution."""
        original = self.seed_resumable_entry()
        real_check = routes_queue.check_api_rules
        result: dict[str, Any] = {}
        threads: list[threading.Thread] = []

        def delete() -> None:
            result["response"] = self.delete("E0001")

        def check_then_delete(*args: Any, **kwargs: Any) -> None:
            real_check(*args, **kwargs)
            thread = threading.Thread(target=delete)
            thread.start()
            threads.append(thread)
            thread.join(timeout=0.3)
            result["waited"] = thread.is_alive()

        routes_queue.check_api_rules = check_then_delete  # type: ignore[assignment]
        try:
            resumed = self.request("post", f"/v1/queue/{original}/resume")
        finally:
            routes_queue.check_api_rules = real_check  # type: ignore[assignment]
        threads[0].join(timeout=5)
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(result["waited"], "the deletion did not wait for the resume's insert")
        self.assertEqual(result["response"].json()["refused"], [{"execution_id": "E0001", "reason": "Q0002 is queued to resume from E0001"}])
        self.assertIsNotNone(self.store.executions.by_number(1))

    def test_the_executions_list_filters_by_name_as_the_tui_does(self) -> None:
        self.seed_resumable_entry()
        self.assertEqual([row["execution_id"] for row in self.request("get", "/v1/executions", params={"name": "CHAIN"}).json()["executions"]], ["E0001"])
        self.assertEqual([row["execution_id"] for row in self.request("get", "/v1/executions", params={"name": "sunset"}).json()["executions"]], ["E0001"])
        self.assertEqual(self.request("get", "/v1/executions", params={"name": "nothing"}).json()["executions"], [])
