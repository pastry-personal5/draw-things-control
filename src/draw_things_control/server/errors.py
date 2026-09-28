"""Maps a ``DtcError``'s code to an HTTP status, as ``core/exit_codes.py``'s ``EXIT_CODES_BY_ERROR_CODE`` maps it to
an exit code, and turns one into the API's stable JSON error shape: a ``code``, a ``message``, and, when there is
one, the offending ``field``."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from draw_things_control.core.errors import DtcError, LimitExceededError

STATUS_BY_ERROR_CODE = {
    "invalid_input": 422,
    "timeout_required": 422,
    "outside_directory": 422,
    "limit_exceeded": 422,
    "not_found": 404,
    "invalid_state": 409,
    "busy": 409,
    "tool_missing": 503,
    "state_unavailable": 503,
}
DEFAULT_STATUS = 503


def status_for_error(error: DtcError) -> int:
    return STATUS_BY_ERROR_CODE.get(error.code, DEFAULT_STATUS)


def error_body(error: DtcError) -> dict[str, object]:
    """The response body: ``code`` and ``message`` always; ``field`` when the error names one, and, for a
    ``limit_exceeded`` error, the ``limit`` and the job's own ``value``."""
    body: dict[str, object] = {"code": error.code, "message": str(error)}
    field = getattr(error, "field", None)
    if field:
        body["field"] = field
    if isinstance(error, LimitExceededError):
        body["limit"] = error.limit
        body["value"] = error.value
    return body


def install_error_handler(app: FastAPI) -> None:
    """Register the handler that turns any ``DtcError`` raised by a route into the API's JSON error shape."""

    @app.exception_handler(DtcError)
    async def handle_dtc_error(_request: Request, error: DtcError) -> JSONResponse:
        return JSONResponse(status_code=status_for_error(error), content=error_body(error))
