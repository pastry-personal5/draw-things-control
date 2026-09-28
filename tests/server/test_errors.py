"""Tests for the DtcError-to-HTTP-status table and the API's JSON error shape."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from draw_things_control.core.errors import BusyError, InputError, LimitExceededError, NotFoundError, StateUnavailableError, TimeoutRequiredError, ToolMissingError
from draw_things_control.server.errors import error_body, install_error_handler, status_for_error
from draw_things_control.services.queue_cancel import CancelRefusedError
from draw_things_control.services.queue_resume import ResumeRefusedError


class StatusForErrorTests(unittest.TestCase):
    def test_the_table_matches_the_milestone_documents_error_section(self) -> None:
        cases = {
            InputError("bad"): 422,
            TimeoutRequiredError("no timeout"): 422,
            LimitExceededError("over", key="max_job_runs", limit=1, value=2): 422,
            NotFoundError("missing"): 404,
            CancelRefusedError("cannot cancel"): 409,
            ResumeRefusedError("cannot resume"): 409,
            BusyError("busy"): 409,
            ToolMissingError("no cli"): 503,
            StateUnavailableError("db down"): 503,
        }
        for error, status in cases.items():
            with self.subTest(type(error).__name__):
                self.assertEqual(status_for_error(error), status)


class ErrorBodyTests(unittest.TestCase):
    def test_a_plain_error_has_a_code_and_message_only(self) -> None:
        body = error_body(BusyError("The dtc server holds the lock"))
        self.assertEqual(body, {"code": "busy", "message": "The dtc server holds the lock"})

    def test_a_field_error_includes_the_field(self) -> None:
        body = error_body(TimeoutRequiredError("required", field="run_timeout_seconds"))
        self.assertEqual(body, {"code": "timeout_required", "message": "required", "field": "run_timeout_seconds"})

    def test_a_limit_error_includes_the_limit_and_value(self) -> None:
        body = error_body(LimitExceededError("over", key="max_job_runs", limit=100, value=101))
        self.assertEqual(body, {"code": "limit_exceeded", "message": "over", "field": "max_job_runs", "limit": 100, "value": 101})


class ErrorHandlerTests(unittest.TestCase):
    def test_a_raised_dtc_error_becomes_its_mapped_status_and_body(self) -> None:
        app = FastAPI()
        install_error_handler(app)

        @app.get("/boom")
        def boom() -> None:
            raise NotFoundError("No job file 'x.yaml'")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/boom")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"code": "not_found", "message": "No job file 'x.yaml'"})


if __name__ == "__main__":
    unittest.main()
