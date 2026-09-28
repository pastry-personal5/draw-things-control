"""End-to-end tests for the read-only HTTP API: auth, the Host header check, and each GET endpoint."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi.testclient import TestClient

from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.state.executions import ExecutionSettings, NewExecution, NewRun
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor

TOKEN = "a" * 64


class FakeWorker:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class ReadRoutesTestCase(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        self.executor: JobExecutor = job_executor(runner_factory=lambda *a: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False)
        self.client = self.build_client()

    def build_client(self, *, worker_alive: bool = True) -> TestClient:
        worker = FakeWorker(worker_alive)
        context = ServerContext(paths=self.paths, global_config=self.global_config, store=self.store, worker=worker, executor=self.executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8765, grpc_port=8766)  # pyright: ignore[reportArgumentType]  (FakeWorker only needs is_alive() for these read-only routes)
        app = create_app(context)
        return TestClient(app, base_url="http://127.0.0.1:8765")

    def get(self, path: str, **kwargs: Any):
        headers = {"Authorization": f"Bearer {TOKEN}", **kwargs.pop("headers", {})}
        return self.client.get(path, headers=headers, **kwargs)

    def write_job_in_catalog(self, name: str = "job.yaml", **changes: Any):
        path = self.paths.jobs / name
        path.write_text(yaml.safe_dump(job_data(prompt_pairs=[{"name": "only", "positive": "text"}], **changes), sort_keys=False), encoding="utf-8")
        return path

    def add_execution(self, name: str = "walk", *, started: str = "2026-09-28T09:00:00+00:00", finished: str = "2026-09-28T09:05:00+00:00", job_file: str | None = None) -> int:
        execution_id = self.store.executions.start(NewExecution(job_name=name, job_file=job_file or f"{name}.yaml", mode="i2v", started_at=started, settings=ExecutionSettings(output_directory=str(self.output_directory))))
        self.store.executions.start_run(execution_id, 1, NewRun(pair="p", positive="text", started_at=started, command=["draw-things-cli", "--api-key", "secret", "--prompt", "text"]))
        self.store.executions.finish_run(execution_id, 1, status="succeeded", exit_code=0, seconds=4.0, output="a.mov", last_frame=None)
        self.store.executions.finish(execution_id, status="succeeded", exit_code=0, signal=None, finished_at=finished)
        return execution_id


class AuthAndHostTests(ReadRoutesTestCase):
    def test_health_needs_no_token_and_reports_liveness(self) -> None:
        response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["worker_alive"])
        self.assertIn("version", body)
        self.assertEqual(body["grpc_port"], 8766)

    def test_health_reports_a_dead_worker_but_still_answers_200(self) -> None:
        client = self.build_client(worker_alive=False)
        response = client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["worker_alive"])

    def test_every_endpoint_but_health_requires_the_token(self) -> None:
        for path in ("/v1/capabilities", "/v1/jobs", "/v1/inputs", "/v1/executions", "/v1/audit"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_a_wrong_token_is_401(self) -> None:
        response = self.client.get("/v1/capabilities", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)

    def test_a_token_in_the_query_string_is_never_accepted(self) -> None:
        response = self.client.get(f"/v1/capabilities?token={TOKEN}")
        self.assertEqual(response.status_code, 401)

    def test_a_host_header_naming_neither_loopback_nor_the_bound_address_is_refused(self) -> None:
        response = self.get("/v1/health", headers={"host": "evil.example.com"})
        self.assertEqual(response.status_code, 400)

    def test_a_loopback_host_header_is_accepted_even_off_the_bound_address(self) -> None:
        response = self.get("/v1/capabilities", headers={"host": "localhost"})
        self.assertEqual(response.status_code, 200)


class CapabilitiesTests(ReadRoutesTestCase):
    def test_capabilities_reports_the_limits_in_force(self) -> None:
        body = self.get("/v1/capabilities").json()
        self.assertEqual(body["allow_write"], False)
        self.assertEqual(body["limits"], {"max_queued_jobs": 20, "max_job_runs": 100, "max_job_seconds": 172800, "max_job_file_bytes": 65536})


class JobRoutesTests(ReadRoutesTestCase):
    def test_a_valid_job_is_listed_and_read_by_file_name_and_by_id(self) -> None:
        self.write_job_in_catalog("walk.yaml")
        listing = self.get("/v1/jobs").json()
        self.assertEqual(len(listing["jobs"]), 1)
        row = listing["jobs"][0]
        self.assertEqual((row["file_name"], row["name"], row["mode"], row["runs"], row["valid"], row["job_id"]), ("walk.yaml", "sunset-walk", "i2v", 5, True, "J0001"))
        by_name = self.get("/v1/jobs/walk.yaml").json()
        self.assertIn("name: sunset-walk", by_name["text"])
        self.assertEqual(by_name["name"], "sunset-walk")
        by_id = self.get("/v1/jobs/J0001").json()
        self.assertEqual(by_id["name"], "sunset-walk")

    def test_an_invalid_job_is_listed_with_its_error_and_field(self) -> None:
        self.write_job_in_catalog("bad.yaml", run_count=0)
        row = self.get("/v1/jobs").json()["jobs"][0]
        self.assertFalse(row["valid"])
        self.assertIsNotNone(row["error"])
        self.assertEqual(row["field"], "run_count")
        detail = self.get("/v1/jobs/bad.yaml").json()
        self.assertIsNotNone(detail["error"])
        self.assertIn("text", detail)

    def test_an_unknown_reference_is_not_found(self) -> None:
        for name in ("../x", "a/b", "/etc/passwd", "nope.yaml"):
            with self.subTest(name):
                self.assertEqual(self.get(f"/v1/jobs/{name}").status_code, 404)

    def test_a_symbolic_link_is_listed_and_read_as_invalid_never_followed(self) -> None:
        real = self.write_job_in_catalog("real.yaml")
        link = self.paths.jobs / "link.yaml"
        link.symlink_to(real)
        row = next(row for row in self.get("/v1/jobs").json()["jobs"] if row["file_name"] == "link.yaml")
        self.assertFalse(row["valid"])
        self.assertIn("symbolic link", row["error"])
        response = self.get("/v1/jobs/link.yaml")
        self.assertEqual(response.status_code, 422)

    def test_the_preview_lists_every_run_with_its_command(self) -> None:
        self.write_job_in_catalog("walk.yaml", run_count=1)
        preview = self.get("/v1/jobs/walk.yaml/preview").json()
        self.assertEqual(preview["runs"][0]["number"], 1)
        self.assertIn("draw-things-cli", preview["runs"][0]["command"])
        self.assertIsInstance(preview["seed"], int)


class InputsRouteTests(ReadRoutesTestCase):
    def test_the_fixtures_default_input_image_is_listed(self) -> None:
        body = self.get("/v1/inputs").json()
        self.assertEqual([image["path"] for image in body["inputs"]], ["first-frame.png"])
        self.assertEqual((body["inputs"][0]["width"], body["inputs"][0]["height"]), (832, 448))


class ExecutionRoutesTests(ReadRoutesTestCase):
    def test_listing_filters_by_status_and_by_exact_job_reference(self) -> None:
        self.write_job_in_catalog("walk.yaml")
        job_path = str((self.paths.jobs / "walk.yaml").resolve())
        self.add_execution("sunset-walk", job_file=job_path)
        self.add_execution("other", job_file=str(self.root / "other.yaml"))
        by_status = self.get("/v1/executions?status=succeeded").json()["executions"]
        self.assertEqual(len(by_status), 2)
        by_job = self.get("/v1/executions?job=walk.yaml").json()["executions"]
        self.assertEqual([row["job_name"] for row in by_job], ["sunset-walk"])

    def test_execution_detail_and_outputs_and_command_redaction(self) -> None:
        self.add_execution("walk")
        execution_id = self.get("/v1/executions").json()["executions"][0]["execution_id"]
        detail = self.get(f"/v1/executions/{execution_id}").json()
        self.assertEqual(len(detail["runs"]), 1)
        self.assertEqual(detail["runs"][0]["pair"], "p")
        self.assertIsNone(detail["manifest"])
        self.assertIsNone(detail["log"])
        self.assertIsNone(detail["cooldown"])
        self.assertNotIn("secret", " ".join(detail["runs"][0]["command"]))
        self.assertIn("[redacted]", detail["runs"][0]["command"])
        outputs = self.get(f"/v1/executions/{execution_id}/outputs").json()
        self.assertEqual(outputs["outputs"][0]["complete"], True)

    def test_execution_detail_includes_the_resolved_cooldown_mapping(self) -> None:
        execution_id = self.store.executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at="2026-09-28T09:00:00+00:00", settings=ExecutionSettings(output_directory=str(self.output_directory), cooldown={"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 3600.0})))
        self.store.executions.finish(execution_id, status="succeeded", exit_code=0, signal=None, finished_at="2026-09-28T09:05:00+00:00")
        number = self.store.executions.number_of(execution_id)
        detail = self.get(f"/v1/executions/E{number:04d}").json()
        self.assertEqual(detail["cooldown"], {"mode": "auto", "ratio": 0.5, "minimum_seconds": 0.0, "maximum_seconds": 3600.0})

    def test_an_unknown_execution_id_is_not_found(self) -> None:
        self.assertEqual(self.get("/v1/executions/E9999").status_code, 404)


class AuditRouteTests(ReadRoutesTestCase):
    def test_the_audit_log_is_listed_newest_first(self) -> None:
        self.store.audit.record(action="submit", target="Q0001", outcome="ok", caller="cli", at="2026-09-28T09:00:00+00:00")
        self.store.audit.record(action="cancel", target="Q0001", outcome="ok", caller="cli", at="2026-09-28T09:05:00+00:00")
        body = self.get("/v1/audit").json()
        self.assertEqual([entry["action"] for entry in body["audit"]], ["cancel", "submit"])


class PaginationTests(ReadRoutesTestCase):
    def test_a_full_page_carries_a_cursor_to_the_next_one(self) -> None:
        for index in range(3):
            self.add_execution(f"job{index}", started=f"2026-09-28T09:0{index}:00+00:00", finished=f"2026-09-28T09:0{index}:30+00:00")
        first = self.get("/v1/executions?limit=2").json()
        self.assertEqual(len(first["executions"]), 2)
        self.assertIsNotNone(first["cursor"])
        second = self.get(f"/v1/executions?limit=2&cursor={first['cursor']}").json()
        self.assertEqual(len(second["executions"]), 1)
        self.assertIsNone(second["cursor"])

    def test_a_forged_cursor_is_refused(self) -> None:
        response = self.get("/v1/executions?cursor=not-a-real-cursor")
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    import unittest

    unittest.main()
