"""HTTP coverage for Milestone 07's draft and guarded job-file routes."""

from __future__ import annotations

import hashlib
from typing import Any

import yaml
from fastapi.testclient import TestClient

from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor

TOKEN = "b" * 64


class FakeWorker:
    def is_alive(self) -> bool:
        return True


class JobFileRoutesTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths.jobs.mkdir(parents=True)
        self.store = Store.open(self.paths.database, mode=StoreMode.WRITE)
        self.addCleanup(self.store.close)
        executor: JobExecutor = job_executor(runner_factory=lambda *args: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False)
        self.read_client = self.client_for(executor, allow_write=False)
        self.write_client = self.client_for(executor, allow_write=True)
        self.text = yaml.safe_dump(job_data(name="fresh", run_timeout_seconds=10, prompt_pairs=[{"name": "only", "positive": "text"}]), sort_keys=False)

    def client_for(self, executor: JobExecutor, *, allow_write: bool) -> TestClient:
        context = ServerContext(paths=self.paths, global_config=self.global_config, store=self.store, worker=FakeWorker(), executor=executor, executable="draw-things-cli", token=TOKEN, bound_host="127.0.0.1", bound_port=8765, grpc_port=8766, allow_write=allow_write)  # pyright: ignore[reportArgumentType]  # write routes do not call the worker
        return TestClient(create_app(context), base_url="http://127.0.0.1:8765")

    def request(self, client: TestClient, method: str, path: str, **kwargs: Any):
        return client.request(method, path, headers={"Authorization": f"Bearer {TOKEN}", **kwargs.pop("headers", {})}, **kwargs)

    def test_validate_is_read_only_and_has_the_submitted_hash(self) -> None:
        response = self.request(self.read_client, "POST", "/v1/validate?name=fresh", json={"yaml": self.text})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sha256"], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertFalse((self.paths.jobs / "fresh.yaml").exists())

    def test_writes_are_hidden_without_the_option_and_registered_with_it(self) -> None:
        response = self.request(self.read_client, "PUT", "/v1/jobs/fresh", json={"yaml": self.text})
        self.assertEqual((response.status_code, response.json()["code"]), (405, "writes_off"))
        self.assertNotIn("/v1/jobs/{name}", self.request(self.read_client, "GET", "/v1/openapi.json").json()["paths"])
        self.assertIn("/v1/jobs/{name}", self.request(self.write_client, "GET", "/v1/openapi.json").json()["paths"])

    def test_create_replace_and_delete_preserve_recovery_copies(self) -> None:
        created = self.request(self.write_client, "PUT", "/v1/jobs/fresh", json={"yaml": self.text})
        self.assertEqual(created.status_code, 201)
        original_hash = created.json()["sha256"]
        changed = self.text + "# comment\r\n"
        replaced = self.request(self.write_client, "PUT", "/v1/jobs/fresh?overwrite=1", json={"yaml": changed, "expected_sha256": original_hash})
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(list((self.paths.jobs_backups / "fresh").glob("*.yaml"))[0].read_text(), self.text)
        stale = self.request(self.write_client, "DELETE", "/v1/jobs/fresh?expected_sha256=" + original_hash)
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "conflict"))
        removed = self.request(self.write_client, "DELETE", "/v1/jobs/fresh?expected_sha256=" + replaced.json()["sha256"])
        self.assertEqual(removed.status_code, 200)
        self.assertFalse((self.paths.jobs / "fresh.yaml").exists())
        self.assertTrue(list(self.paths.jobs_trash.glob("fresh-*.yaml")))
