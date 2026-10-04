"""One-off generation route boundaries."""

from __future__ import annotations

import json

from draw_things_control.core.errors import InputError
from draw_things_control.services.generation_submit import GenerationSnapshot
from tests.server.test_queue_routes import QueueRoutesTestCase


class GenerationRoutesTests(QueueRoutesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.output_directory.mkdir()

    def test_preview_validates_without_queueing_or_auditing(self) -> None:
        response = self.request("post", "/v1/generations/preview", json={"model": "model.ckpt", "prompt": "cube", "output": "cube.png", "timeout": 60})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("draw-things-cli generate", response.json()["command"])
        self.assertEqual(self.store.queue.list(), [])
        self.assertEqual(self.store.audit.page(limit=10), [])

    def test_submit_is_a_tagged_one_run_entry_and_audits_the_canonical_output(self) -> None:
        response = self.request("post", "/v1/generations", json={"model": "model.ckpt", "prompt": "cube", "output": "nested/cube.png", "timeout": 60})
        self.assertEqual(response.status_code, 422)
        (self.output_directory / "nested").mkdir()
        response = self.request("post", "/v1/generations", json={"model": "model.ckpt", "prompt": "cube", "output": "nested/cube.png", "timeout": 60})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["kind"], body["job_path"], body["total_runs"], body["generation"]), ("generate", None, 1, {"model": "model.ckpt", "output": "nested/cube.png"}))
        entry = self.store.queue.by_number(1)
        assert entry is not None
        self.assertEqual(entry.kind, "generate")
        audit = self.store.audit.page(limit=10)
        self.assertEqual((audit[0].action, audit[0].target, audit[0].outcome), ("generate", "nested/cube.png", "ok"))

    def test_mov_uses_the_established_prores_default_and_the_snapshot_is_strictly_tagged(self) -> None:
        response = self.request("post", "/v1/generations/preview", json={"model": "model.ckpt", "output": "cube.mov", "timeout": 60})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("--video-format prores4444", response.json()["command"])
        snapshot = GenerationSnapshot(model="model.ckpt", output="cube.png", output_path=str(self.output_directory / "cube.png"), timeout=60)
        invalid = json.loads(snapshot.to_json())
        invalid["kind"] = "job"
        with self.assertRaises(InputError):
            GenerationSnapshot.from_json(json.dumps(invalid))

    def test_paths_and_unknown_or_secret_fields_are_refused_and_audited_without_a_target(self) -> None:
        for body in ({"model": "m", "output": "../cube.png", "timeout": 60}, {"model": "m", "output": "cube.png", "timeout": 60, "api_key": "secret"}):
            with self.subTest(body=body):
                response = self.request("post", "/v1/generations", json=body)
                self.assertEqual(response.status_code, 422, response.text)
        audits = self.store.audit.page(limit=10)
        self.assertEqual([(row.action, row.target, row.outcome) for row in audits], [("generate", None, "invalid_input"), ("generate", None, "outside_directory")])
