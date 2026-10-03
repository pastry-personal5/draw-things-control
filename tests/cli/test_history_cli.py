"""Tests for `dtc history delete` (Milestone 06), against a fake ``httpx`` transport standing in for dtc serve."""

from __future__ import annotations

import json

import httpx
from loguru import logger
from typer.testing import CliRunner

from draw_things_control.cli.app import app
from draw_things_control.cli.context import CliServices
from draw_things_control.services.toolkit import Toolkit
from tests.fixtures import JobTestCase

TOKEN = "a" * 64
# The fake server's page size, so a filter over five executions reads three pages.
PAGE = 2


class FakeHistoryServer:
    """``GET /v1/executions`` (paged, filtered) and ``POST /v1/executions/delete`` over a list of executions."""

    def __init__(self, count: int) -> None:
        self.executions = [{"execution_id": f"E{number:04d}", "job_name": "walk" if number % 2 else "run", "status": "failed" if number % 2 else "succeeded", "total_runs": 7, "succeeded": 3, "first_run": 1, "started_at": "2026-09-28T14:03:00+00:00"} for number in range(count, 0, -1)]
        self.refused: dict[str, str] = {}
        self.ended: dict[str, list[str]] = {}
        self.kept: dict[str, str] = {}
        self.calls: list[str] = []
        self.down = False
        # How many real deletions succeed before the server drops (None: never).
        self.deletes_before_down: int | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        if request.method == "POST" and not json.loads(request.content)["dry_run"] and self.deletes_before_down is not None:
            if self.deletes_before_down == 0:
                raise httpx.ReadError("reset")
            self.deletes_before_down -= 1
        if request.method == "GET" and request.url.path == "/v1/executions":
            self.calls.append("GET")
            params = request.url.params
            rows = [row for row in self.executions if params.get("status") in (None, row["status"]) and params.get("name", "") in row["job_name"]]
            offset = int(params.get("cursor", "0"))
            page = rows[offset : offset + PAGE]
            return httpx.Response(200, json={"executions": page, "cursor": str(offset + PAGE) if offset + PAGE < len(rows) else None})
        assert request.method == "POST" and request.url.path == "/v1/executions/delete"
        body = json.loads(request.content)
        assert len(body["executions"]) <= 200
        self.calls.append("dry run" if body["dry_run"] else "delete")
        known = {row["execution_id"] for row in self.executions}
        deleted = [execution for execution in body["executions"] if execution in known and execution not in self.refused]
        if not body["dry_run"]:
            self.executions = [row for row in self.executions if row["execution_id"] not in deleted]
        return httpx.Response(
            200,
            json={
                "dry_run": body["dry_run"],
                "deleted": deleted,
                "refused": [{"execution_id": execution, "reason": self.refused[execution]} for execution in body["executions"] if execution in self.refused],
                "missing": [execution for execution in body["executions"] if execution not in known],
                "resumes_ended": [{"execution_id": execution, "queue_ids": self.ended[execution]} for execution in deleted if execution in self.ended],
                "manifests_kept": [] if body["dry_run"] else [{"execution_id": execution, "path": self.kept[execution]} for execution in deleted if execution in self.kept],
            },
        )


class HistoryCliTests(JobTestCase):
    def test_an_invalid_execution_id_is_refused_before_a_request(self) -> None:
        result = self.invoke("E0001/outputs", "--yes")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.server.calls, [])

    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.token_path = self.root / "token"
        self.token_path.write_text(TOKEN, encoding="ascii")
        self.server = FakeHistoryServer(5)
        self.terminal = True
        self.errors: list[str] = []
        sink = logger.add(lambda message: self.errors.append(str(message).rstrip("\n")), format="{message}", level="ERROR")
        self.addCleanup(logger.remove, sink)

    def invoke(self, *arguments: str, answer: str | None = None):
        services = CliServices(self.paths, Toolkit(), http_transport=httpx.MockTransport(self.server.handle), stdin_is_terminal=lambda: self.terminal)
        return self.runner.invoke(app, ["history", "delete", *arguments, "--token-file", str(self.token_path)], obj=services, input=answer)

    def remaining(self) -> list[str]:
        return [row["execution_id"] for row in self.server.executions]

    def test_ids_confirmed_are_deleted_after_a_listing_with_the_resumes_they_end(self) -> None:
        self.server.ended["E0003"] = ["Q0007"]
        result = self.invoke("E0003", "e1", answer="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.remaining(), ["E0005", "E0004", "E0002"])
        self.assertIn("E0003; Q0007 can no longer be resumed\n", result.stdout)
        self.assertIn("Delete 2 executions? [y/N]", result.stdout)
        self.assertTrue(result.stdout.endswith("Deleted 2 executions\nQ0007 can no longer be resumed\n"), result.stdout)
        self.assertEqual(self.server.calls, ["dry run", "delete"])

    def test_answering_no_deletes_nothing(self) -> None:
        result = self.invoke("E0003", answer="n\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(result.stdout.endswith("Nothing was deleted\n"))
        self.assertEqual(len(self.remaining()), 5)

    def test_without_a_terminal_it_refuses_unless_yes(self) -> None:
        self.terminal = False
        result = self.invoke("E0003")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(self.errors, ["Not asking without a terminal; give --yes to delete"])
        self.assertEqual(len(self.remaining()), 5)
        result = self.invoke("E0003", "--yes")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(self.remaining()), 4)

    def test_a_filter_reads_every_page_before_deleting_and_lists_each_one(self) -> None:
        result = self.invoke("--status", "failed", "--name", "wal", "--yes")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.remaining(), ["E0004", "E0002"])
        self.assertIn("E0005  walk  failed  3/7 runs  2026-09-28T14:03:00+00:00\n", result.stdout)
        self.assertEqual(self.server.calls, ["GET", "GET", "dry run", "delete"])

    def test_all_deletes_the_whole_history_after_reading_it_in_full(self) -> None:
        result = self.invoke("--all", "--yes")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(self.server.calls, ["GET", "GET", "GET", "dry run", "delete"])

    def test_a_filter_matching_nothing_deletes_nothing(self) -> None:
        result = self.invoke("--name", "nothing")
        self.assertEqual((result.exit_code, result.stdout), (0, "No executions match the filter\n"))

    def test_a_mixed_or_empty_selection_is_refused(self) -> None:
        for arguments in (("E0001", "--all"), ("--status", "failed", "--all"), ("E0001", "--name", "walk"), ()):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, "--yes")
                self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(self.errors[-1], "Nothing to delete: give execution IDs, --status or --name, or --all")
        self.assertEqual(set(self.errors[:-1]), {"Give execution IDs, filters (--status, --name), or --all, not a mix"})
        self.assertEqual((len(self.remaining()), self.server.calls), (5, []))

    def test_an_empty_filter_is_refused_rather_than_selecting_everything(self) -> None:
        for arguments in (("--name", ""), ("--status", " ")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, "--yes")
                self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(self.errors, ["--name must not be empty; use --all to delete the whole history", "--status must not be empty; use --all to delete the whole history"])
        self.assertEqual((len(self.remaining()), self.server.calls), (5, []))

    def test_refused_and_missing_ones_are_named_and_the_rest_deleted_with_exit_code_2(self) -> None:
        self.server.refused["E0004"] = "E0004 is running; it cannot be deleted"
        self.server.kept["E0003"] = "/out/walk.json"
        result = self.invoke("E0004", "E0003", "E0099", "--yes")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(self.remaining(), ["E0005", "E0004", "E0002", "E0001"])
        self.assertIn("Not deleted: E0004 is running; it cannot be deleted\nNo execution E0099\n", result.stdout)
        self.assertIn("E0003's manifest could not be deleted: /out/walk.json; `dtc import-history` would bring it back under a new number", result.stdout)

    def test_nothing_deletable_asks_nothing(self) -> None:
        result = self.invoke("E0099")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertTrue(result.stdout.endswith("Nothing was deleted\n"))

    def test_a_request_failing_partway_still_names_what_went_before_it(self) -> None:
        self.server = FakeHistoryServer(205)
        self.server.kept = {"E0205": "/out/walk.json"}
        self.server.ended = {"E0204": ["Q0007"]}
        self.server.deletes_before_down = 1
        result = self.invoke("--all", "--yes")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(len(self.remaining()), 5)
        self.assertIn("Deleted 200 executions", result.stdout)
        self.assertIn("Q0007 can no longer be resumed", result.stdout)
        self.assertIn("E0205's manifest could not be deleted: /out/walk.json", result.stdout)

    def test_the_server_down_deletes_nothing(self) -> None:
        self.server.down = True
        result = self.invoke("E0001", "--yes")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("is 'dtc serve' running?", self.errors[0])
