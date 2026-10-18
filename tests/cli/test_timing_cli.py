"""Tests for `fc timing show`, against a fake ``httpx`` transport standing in for fc serve."""

from __future__ import annotations

import httpx
from loguru import logger
from typer.testing import CliRunner

from framecontrol.cli.app import app
from framecontrol.cli.context import CliServices
from framecontrol.services.toolkit import Toolkit
from tests.fixtures import JobTestCase

TOKEN = "a" * 64


def stage(name: str, source: str, start: float, end: float, total: float, *, phase: str | None = None, done: int | None = None, units: int | None = None, unit: str | None = None, uncertain: bool = False) -> dict[str, object]:
    counted = start >= 0
    return {"stage": name, "phase": phase, "source": source, "start": start, "end": end, "duration": end - start, "share": round((end - start) / total, 6) if counted else None, "done": done, "total": units, "unit": unit, "marks": [], "uncertain": uncertain}


RUN_ONE = {"execution_id": "E0047", "run": 1, "recorded": True, "backend": "h3-metal-cli", "pipeline": "r2av", "fidelity": "live", "complete": True, "workload": {}, "environment": {}, "seconds": 100.0, "stages": [stage("prepare_input", "framecontrol", -2.0, -1.0, 100.0), stage("prepare", "framecontrol", 0.0, 0.5, 100.0), stage("tokenize", "child", 0.5, 0.7, 100.0, phase="tokenizer", done=1, units=1), stage("denoise", "child", 0.7, 90.7, 100.0, phase="denoise enqueue", done=30, units=30, unit="steps", uncertain=True), stage("correct", "framecontrol", 90.7, 100.0, 100.0)]}
RUN_TWO = {**RUN_ONE, "run": 2, "recorded": False, "stages": []}


class TimingCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.token_path = self.root / "token"
        self.token_path.write_text(TOKEN, encoding="ascii")
        self.requests: list[str] = []
        self.errors: list[str] = []
        sink = logger.add(lambda message: self.errors.append(str(message).rstrip("\n")), format="{message}", level="ERROR")
        self.addCleanup(logger.remove, sink)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.raw_path.decode())
        path = request.url.path
        if path == "/v1/executions/E0047":
            return httpx.Response(200, json={"execution_id": "E0047", "runs": [{"number": 1}, {"number": 2}]})
        if path == "/v1/executions/E0047/runs/1/stages":
            return httpx.Response(200, json=RUN_ONE)
        if path == "/v1/executions/E0047/runs/2/stages":
            return httpx.Response(200, json={**RUN_TWO, "stages": [], "recorded": False})
        if path == "/v1/executions/E0047/runs/3/stages":
            return httpx.Response(200, json={**RUN_ONE, "run": 3, "backend": None, "pipeline": None, "fidelity": None, "complete": False})
        return httpx.Response(404, json={"code": "not_found", "message": "No execution " + path.split("/")[3]})

    def invoke(self, *arguments: str):
        services = CliServices(self.paths, Toolkit(), http_transport=httpx.MockTransport(self.handle))
        return self.runner.invoke(app, ["timing", "show", *arguments, "--token-file", str(self.token_path)], obj=services)

    def test_a_run_is_a_table_of_its_stages_with_start_duration_share_and_units(self) -> None:
        result = self.invoke("E0047", "--run", "1")
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "E0047 run 1: h3-metal-cli r2av, live, complete, 100.0 s")
        header, *rows = lines[1:]
        self.assertEqual(header.split(), ["stage", "phase", "source", "start", "duration", "share", "units"])
        by_stage = {row.split()[0].rstrip("*"): row for row in rows if row.strip()}
        self.assertIn("-2.0s", by_stage["prepare_input"])
        self.assertNotIn("%", by_stage["prepare_input"])
        self.assertTrue(by_stage["tokenize"].split()[-1] == "1/1")
        self.assertIn("90.0s", by_stage["denoise"])
        self.assertIn("90.0%", by_stage["denoise"])
        self.assertTrue(by_stage["denoise"].rstrip().endswith("30/30 steps"))
        self.assertIn("denoise enqueue", by_stage["denoise"])
        self.assertIn("9.3%", by_stage["correct"])
        self.assertEqual(self.requests, ["/v1/executions/E0047/runs/1/stages"])

    def test_a_run_with_stages_but_no_timing_row_says_what_it_does_not_know_instead_of_none(self) -> None:
        result = self.invoke("E0047", "--run", "3")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout.splitlines()[0], "E0047 run 3: backend unknown, fidelity unknown, incomplete, 100.0 s")
        self.assertNotIn("None", result.stdout)

    def test_an_uncertain_first_reading_is_marked_and_explained(self) -> None:
        result = self.invoke("E0047", "--run", "1")
        self.assertIn("denoise*", result.stdout)
        self.assertIn("* the first reading of the stage is estimated from the log", result.stdout)

    def test_without_a_run_every_run_of_the_execution_is_shown_and_one_without_timing_says_so(self) -> None:
        result = self.invoke("E0047")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("E0047 run 1: h3-metal-cli r2av", result.stdout)
        self.assertIn("E0047 run 2: no timing recorded", result.stdout)
        self.assertEqual(self.requests, ["/v1/executions/E0047?brief=1", "/v1/executions/E0047/runs/1/stages", "/v1/executions/E0047/runs/2/stages"])

    def test_an_id_that_is_not_an_execution_is_refused_before_a_request(self) -> None:
        result = self.invoke("Q0001")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.errors, ["'execution_id' must be an execution ID such as E0047"])

    def test_an_execution_the_server_does_not_have_ends_with_its_error(self) -> None:
        result = self.invoke("E0099", "--run", "1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(any("No execution" in error for error in self.errors))


class TimingImportCliTests(JobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self.global_path = self.root / "global-config.yaml"
        self.global_path.write_text(f"version: 1\ninput_directory: {self.input_directory}\noutput_directory: {self.output_directory}\n", encoding="utf-8")
        self.services = CliServices(self.paths, Toolkit())

    def seed(self, *, with_log: bool) -> None:
        from datetime import datetime

        from framecontrol.state.executions import ExecutionSettings, NewExecution, NewRun
        from framecontrol.state.store import Store
        from tests.jobs.timing.test_log_import import job_log
        from tests.jobs.timing.transcripts import draw_things

        log = self.root / "walk.log"
        if with_log:
            log.write_text(job_log(draw_things(), lagged=False), encoding="utf-8")
        self.paths.state.mkdir(parents=True, exist_ok=True)
        store = Store.open(self.paths.database)
        try:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            execution = store.executions.start(NewExecution(job_name="walk", job_file="walk.yaml", mode="i2v", started_at=now, settings=ExecutionSettings(output_directory="/out"), log_path=str(log)))
            store.executions.start_run(execution, 1, NewRun(pair="p", positive="t", started_at=now))
            store.executions.finish_run(execution, 1, status="succeeded", exit_code=0, seconds=1.0, output="a.mov", last_frame=None)
            store.executions.finish(execution, status="succeeded", exit_code=0, signal=None, finished_at=now)
        finally:
            store.close()

    def invoke(self):
        return self.runner.invoke(app, ["timing", "import", "--global-config", str(self.global_path)], obj=self.services)

    def test_it_reports_what_it_imported_and_is_safe_to_repeat(self) -> None:
        self.seed(with_log=True)
        first = self.invoke()
        self.assertEqual((first.exit_code, first.stdout), (0, "Imported timing for 1 run of 1 execution from their logs, skipped 0\n"))
        again = self.invoke()
        self.assertEqual(again.stdout, "  E0001: it already has timing\nImported timing for 0 runs of 0 executions from their logs, skipped 1\n")

    def test_it_names_each_execution_it_skips_and_why(self) -> None:
        self.seed(with_log=False)
        result = self.invoke()
        self.assertEqual(result.stdout, "  E0001: its log no longer exists\nImported timing for 0 runs of 0 executions from their logs, skipped 1\n")

    def test_fc_history_says_it_only_deletes(self) -> None:
        result = self.runner.invoke(app, ["history", "--help"], obj=self.services)
        self.assertIn("only deletes", " ".join(result.stdout.split()))
