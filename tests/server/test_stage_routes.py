"""``GET /v1/executions/{id}/runs/{run}/stages`` and ``?stages=1`` on the run: what each stage of a run took."""

from __future__ import annotations

from framecontrol.jobs.timing.model import StageSpan
from tests.server.test_read_routes import ReadRoutesTestCase


class StageRouteTests(ReadRoutesTestCase):
    def add_timed_execution(self) -> tuple[int, str]:
        execution_id = self.add_execution()
        number = self.store.executions.number_of(execution_id)
        timing = self.store.timing
        timing.start(execution_id, 1, "draw-things-cli", "i2v", {"width": 832, "height": 448, "steps": 40}, {"model": "m.ckpt", "load_start": [1.0, 0.5, 0.25]})
        timing.add_stage(execution_id, 1, StageSpan("prepare_input", None, "framecontrol", -2.0, -1.0))
        timing.add_stage(execution_id, 1, StageSpan("prepare", None, "framecontrol", 0.0, 1.0))
        timing.add_stage(execution_id, 1, StageSpan("sampling", "Sampling", "child", 1.0, 91.0, 40, 40, "steps", ((1.0, 0), (91.0, 40))))
        timing.add_stage(execution_id, 1, StageSpan("verify", None, "framecontrol", 91.0, 99.0))
        timing.finish(execution_id, 1, complete=True, environment={"load_end": [2.0, 1.0, 0.5]})
        return execution_id, f"E{number:04d}"

    def test_a_run_gives_its_stages_with_their_shares_and_what_it_was_judged_by(self) -> None:
        _row, label = self.add_timed_execution()
        response = self.get(f"/v1/executions/{label}/runs/1/stages", params={"marks": "1"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["execution_id"], body["run"], body["recorded"], body["backend"], body["pipeline"], body["fidelity"], body["complete"], body["seconds"]), (label, 1, True, "draw-things-cli", "i2v", "live", True, 99.0))
        self.assertEqual(body["workload"], {"width": 832, "height": 448, "steps": 40})
        self.assertEqual(body["environment"], {"model": "m.ckpt", "load_start": [1.0, 0.5, 0.25], "load_end": [2.0, 1.0, 0.5]})
        self.assertEqual([stage["stage"] for stage in body["stages"]], ["prepare_input", "prepare", "sampling", "verify"])
        before, prepare, sampling, verify = body["stages"]
        self.assertEqual((before["start"], before["end"], before["duration"], before["share"]), (-2.0, -1.0, 1.0, None))
        self.assertEqual((prepare["share"], sampling["share"], verify["share"]), (round(1 / 99, 6), round(90 / 99, 6), round(8 / 99, 6)))
        self.assertEqual((sampling["phase"], sampling["source"], sampling["done"], sampling["total"], sampling["unit"], sampling["marks"], sampling["uncertain"]), ("Sampling", "child", 40, 40, "steps", [[1.0, 0], [91.0, 40]], False))

    def test_marks_come_only_when_asked_for_so_an_agents_answer_stays_small(self) -> None:
        _row, label = self.add_timed_execution()
        for path, params in ((f"/v1/executions/{label}/runs/1/stages", {}), (f"/v1/executions/{label}/runs/1", {"stages": "1"})):
            with self.subTest(path=path):
                body = self.get(path, params=params).json()
                stages = body.get("timing", body)["stages"]
                self.assertTrue(stages)
                self.assertTrue(all("marks" not in stage for stage in stages))
        asked = self.get(f"/v1/executions/{label}/runs/1", params={"stages": "1", "marks": "1"}).json()["timing"]
        sampling = next(stage for stage in asked["stages"] if stage["stage"] == "sampling")
        self.assertEqual(sampling["marks"], [[1.0, 0], [91.0, 40]])

    def test_another_value_of_marks_is_refused(self) -> None:
        _row, label = self.add_timed_execution()
        for value in ("0", "true", ""):
            response = self.get(f"/v1/executions/{label}/runs/1/stages", params={"marks": value})
            self.assertEqual((response.status_code, response.json()["code"], response.json()["field"]), (422, "invalid_input", "marks"), value)

    def test_a_run_with_no_timing_says_so_instead_of_failing(self) -> None:
        execution_id = self.add_execution()
        number = self.store.executions.number_of(execution_id)
        body = self.get(f"/v1/executions/E{number:04d}/runs/1/stages").json()
        self.assertEqual((body["recorded"], body["stages"]), (False, []))

    def test_the_run_gives_the_same_stages_when_asked_and_not_otherwise(self) -> None:
        _row, label = self.add_timed_execution()
        plain = self.get(f"/v1/executions/{label}/runs/1").json()
        asked = self.get(f"/v1/executions/{label}/runs/1", params={"stages": "1", "marks": "1"}).json()
        stages = self.get(f"/v1/executions/{label}/runs/1/stages", params={"marks": "1"}).json()
        self.assertNotIn("timing", plain)
        self.assertEqual({key: value for key, value in asked.items() if key != "timing"}, plain)
        self.assertEqual(asked["timing"], {key: value for key, value in stages.items() if key not in ("execution_id", "run")})

    def test_another_value_of_stages_is_refused(self) -> None:
        _row, label = self.add_timed_execution()
        for value in ("0", "true", ""):
            response = self.get(f"/v1/executions/{label}/runs/1", params={"stages": value})
            self.assertEqual((response.status_code, response.json()["code"], response.json()["field"]), (422, "invalid_input", "stages"), value)

    def test_a_missing_execution_or_run_is_not_found_a_bad_number_invalid_and_a_token_needed(self) -> None:
        _row, label = self.add_timed_execution()
        self.assertEqual(self.get("/v1/executions/E9999/runs/1/stages").status_code, 404)
        self.assertEqual(self.get(f"/v1/executions/{label}/runs/7/stages").json()["code"], "not_found")
        response = self.get(f"/v1/executions/{label}/runs/0/stages")
        self.assertEqual((response.status_code, response.json()["field"]), (422, "run"))
        self.assertEqual(self.client.get(f"/v1/executions/{label}/runs/1/stages").status_code, 401)
