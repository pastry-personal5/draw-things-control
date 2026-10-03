"""HTTP coverage for Milestone 07's draft and guarded job-file routes."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

import httpx
import yaml
from fastapi.testclient import TestClient

from draw_things_control.core.global_config import ApiLimits
from draw_things_control.jobs.executor import JobExecutor
from draw_things_control.server import routes_job_files
from draw_things_control.server.app import create_app
from draw_things_control.server.context import ServerContext
from draw_things_control.state.store import Store, StoreMode
from tests.fixtures import JobTestCase, job_data, job_executor

TOKEN = "b" * 64


class FakeWorker:
    def is_alive(self) -> bool:
        return True

    def enqueue(self, *args: object) -> None:
        raise AssertionError("oversized configuration must be refused before enqueue")


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

    def test_malformed_job_text_is_a_typed_refusal_on_validate_and_write(self) -> None:
        client = TestClient(self.write_client.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
        malformed = ("", "not a mapping", "name: x\nname: y\n", "version: 0777\n", "a:\n\tb: c\n", "a: [", "a: &x [*x]", "a: " + "[" * 1500 + "x" + "]" * 1500, "\ud800")
        for text in malformed:
            for method, path in (("POST", "/v1/validate"), ("PUT", "/v1/jobs/fresh")):
                with self.subTest(method=method, text=text[:30]):
                    response = self.request(client, method, path, content=b'{"yaml":"\\ud800"}', headers={"Content-Type": "application/json"}) if text == "\ud800" else self.request(client, method, path, json={"yaml": text})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["code"], "invalid_input")
                    self.assertIn("message", response.json())
        for text in ("a: " + "9" * 5000, "x" * 70000 + ": 1"):
            with self.subTest(size=len(text)):
                response = self.request(client, "POST", "/v1/validate", json={"yaml": text})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(response.json()["code"], {"invalid_input", "limit_exceeded"})

    def test_deeply_nested_json_body_is_a_typed_refusal(self) -> None:
        body = b'{"yaml":' + b"[" * 1500 + b'"x"' + b"]" * 1500 + b"}"
        response = self.request(self.write_client, "POST", "/v1/validate", content=body, headers={"Content-Type": "application/json"})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "invalid_input"))

    def test_all_http_bodies_are_bounded_before_parsing_and_audited_when_writing(self) -> None:
        limits = ApiLimits(max_job_file_bytes=128)
        self.global_config = replace(self.global_config, api_limits=limits)
        client = self.client_for(job_executor(runner_factory=lambda *args: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False), allow_write=True)
        client = TestClient(client.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
        for method, path in (("POST", "/v1/queue"), ("POST", "/v1/executions/delete"), ("PUT", "/v1/jobs/fresh")):
            with self.subTest(path=path):
                response = self.request(client, method, path, content=b"x" * 1025, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 413, response.text)
                self.assertEqual((response.json()["code"], response.json()["limit"]), ("limit_exceeded", 1024))
        audit = self.request(client, "GET", "/v1/audit").json()["audit"]
        self.assertEqual([(row["action"], row["target"], row["outcome"]) for row in audit], [("create_job", None, "limit_exceeded"), ("delete_execution", None, "limit_exceeded"), ("submit", None, "limit_exceeded")])
        response = client.post("/v1/queue", content=b"x" * 1025, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(len(self.request(client, "GET", "/v1/audit").json()["audit"]), 3)

    def test_oversized_write_audit_uses_the_effective_overwrite_parameter(self) -> None:
        self.global_config = replace(self.global_config, api_limits=ApiLimits(max_job_file_bytes=128))
        client = self.client_for(job_executor(runner_factory=lambda *args: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False), allow_write=True)
        for query, action in (("overwrite=0&overwrite=1", "replace_job"), ("overwrite=1&overwrite=", "create_job")):
            response = self.request(client, "PUT", f"/v1/jobs/fresh?{query}", content=b"x" * 1025, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(self.request(client, "GET", "/v1/audit").json()["audit"][0]["action"], action)

    def test_a_chunked_body_is_bounded_without_a_content_length(self) -> None:
        self.global_config = replace(self.global_config, api_limits=ApiLimits(max_job_file_bytes=128))
        client = self.client_for(job_executor(runner_factory=lambda *args: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False), allow_write=True)

        async def chunks():
            yield b"x" * 800
            yield b"y" * 225

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://127.0.0.1:8765", headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                response = await http.post("/v1/queue", content=chunks(), headers={"Content-Type": "application/json"})
                self.assertEqual((response.status_code, response.json()["value"]), (413, 1025))

        asyncio.run(scenario())
        audit = self.request(client, "GET", "/v1/audit").json()["audit"]
        self.assertEqual((audit[0]["action"], audit[0]["outcome"], audit[0]["target"]), ("submit", "limit_exceeded", None))

    def test_existing_job_and_base_config_reads_accept_the_limit_and_refuse_one_byte_over(self) -> None:
        self.global_config = replace(self.global_config, api_limits=ApiLimits(max_job_file_bytes=512))
        client = self.client_for(job_executor(runner_factory=lambda *args: None, find_executable=lambda name: name, frame_extractor=lambda video, png: None, require_ffmpeg=lambda: "ffmpeg", handle_signals=False), allow_write=True)
        job = self.paths.jobs / "fresh.yaml"
        job.write_text(self.text + "\n#" + "x" * (512 - len(self.text.encode()) - 2), encoding="utf-8")
        self.assertEqual(job.stat().st_size, 512)
        self.assertEqual(self.request(client, "GET", "/v1/jobs/fresh.yaml").status_code, 200)
        job.write_text(job.read_text(encoding="utf-8") + "x", encoding="utf-8")
        response = self.request(client, "GET", "/v1/jobs/fresh.yaml")
        self.assertEqual((response.status_code, response.json()["code"]), (422, "limit_exceeded"))
        listing = self.request(client, "GET", "/v1/jobs").json()["jobs"]
        self.assertIn("limit", listing[0]["error"])

        job.write_text(self.text, encoding="utf-8")
        config = self.params / "base.yaml"
        config_text = config.read_text(encoding="utf-8")
        config_limit = 1024 * 1024
        config.write_text(config_text + "\n#" + "x" * (config_limit - len(config_text.encode()) - 2), encoding="utf-8")
        self.assertEqual(config.stat().st_size, config_limit)
        self.assertEqual(self.request(client, "GET", "/v1/jobs/fresh.yaml").status_code, 200)
        config.write_text(config.read_text(encoding="utf-8") + "x", encoding="utf-8")
        listing = self.request(client, "GET", "/v1/jobs").json()["jobs"]
        self.assertIn("limit", listing[0]["error"])
        response = self.request(client, "POST", "/v1/queue", json={"job": "fresh.yaml"})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "limit_exceeded"))

    def test_job_listing_and_validation_do_not_read_a_linked_base_configuration(self) -> None:
        outside = self.root / "outside.yaml"
        outside.write_text("model: sentinel-secret\n", encoding="utf-8")
        (self.params / "base.yaml").unlink()
        (self.params / "base.yaml").symlink_to(outside)
        response = self.request(self.write_client, "POST", "/v1/validate", json={"yaml": self.text})
        self.assertEqual((response.status_code, response.json()["field"]), (422, "config_file"))
        self.assertNotIn("sentinel-secret", response.text)

    def test_validation_refuses_a_linked_parameter_directory_before_lookup(self) -> None:
        outside = self.root / "outside-params"
        outside.mkdir()
        (outside / "base.yaml").write_text("model: sentinel-secret\n", encoding="utf-8")
        self.params.rename(self.root / "old-params")
        self.params.symlink_to(outside)
        response = self.request(self.write_client, "POST", "/v1/validate", json={"yaml": self.text})
        self.assertEqual((response.status_code, response.json()["field"]), (422, "config_file"))
        self.assertNotIn("sentinel-secret", response.text)

    def test_job_file_writes_refuse_linked_directories_and_ancestors(self) -> None:
        outside = self.root / "other-jobs"
        outside.mkdir()
        self.paths.jobs.rmdir()
        self.paths.jobs.symlink_to(outside)
        response = self.request(self.write_client, "PUT", "/v1/jobs/fresh", json={"yaml": self.text})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "conflict"))
        self.assertFalse((outside / "fresh.yaml").exists())
        self.paths.jobs.unlink()
        self.paths.jobs.mkdir()
        target = self.paths.jobs / "fresh.yaml"
        target.write_text(self.text, encoding="utf-8")
        checksum = hashlib.sha256(self.text.encode()).hexdigest()
        data = self.root / "data"
        real = self.root / "data-real"
        data.rename(real)
        data.symlink_to(real)
        refused = self.request(self.write_client, "DELETE", f"/v1/jobs/fresh?expected_sha256={checksum}")
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "conflict"))
        self.assertTrue((real / "jobs/fresh.yaml").exists())

    def test_an_existing_job_over_the_limit_is_refused_before_its_yaml_is_parsed(self) -> None:
        self.paths.jobs.joinpath("huge.yaml").write_text("not valid: [\n" + "x" * 65536, encoding="utf-8")
        response = self.request(self.write_client, "GET", "/v1/jobs/huge.yaml")
        self.assertEqual((response.status_code, response.json()["code"]), (422, "limit_exceeded"))
        listing = self.request(self.write_client, "GET", "/v1/jobs").json()["jobs"]
        self.assertEqual(listing[0]["valid"], False)
        self.assertIn("limit", listing[0]["error"])

    def test_job_detail_reports_invalid_utf8_with_the_original_file_hash(self) -> None:
        source = b"name: broken\ninvalid: \xff\n"
        (self.paths.jobs / "broken.yaml").write_bytes(source)
        response = self.request(self.write_client, "GET", "/v1/jobs/broken.yaml")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sha256"], hashlib.sha256(source).hexdigest())
        self.assertIn("\ufffd", response.json()["text"])
        self.assertIn("Job file is not valid UTF-8", response.json()["error"])

    def test_health_answers_while_a_write_waits_on_the_lock_and_validation_is_slow(self) -> None:
        context: ServerContext = cast(Any, self.write_client.app).state.context
        gate = threading.Event()
        original = routes_job_files.validate_job_text

        def slow_validate(*args: Any, **kwargs: Any) -> Any:
            gate.wait(2)
            return original(*args, **kwargs)

        async def scenario() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.write_client.app), base_url="http://127.0.0.1:8765", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
                context.submission_lock.acquire()
                fallback = threading.Timer(2, lambda: context.submission_lock.release() if context.submission_lock.locked() else None)
                fallback.start()
                try:
                    write = asyncio.create_task(client.put("/v1/jobs/fresh", json={"yaml": self.text}))
                    validation = asyncio.create_task(client.post("/v1/validate", json={"yaml": self.text}))
                    await asyncio.sleep(0.05)
                    start = time.monotonic()
                    health = await asyncio.wait_for(client.get("/v1/health"), timeout=1)
                    self.assertEqual(health.status_code, 200)
                    self.assertLess(time.monotonic() - start, 1)
                finally:
                    gate.set()
                    if context.submission_lock.locked():
                        context.submission_lock.release()
                    fallback.cancel()
                self.assertEqual((await write).status_code, 201)
                self.assertEqual((await validation).status_code, 200)

        with patch.object(routes_job_files, "validate_job_text", side_effect=slow_validate):
            asyncio.run(scenario())

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
