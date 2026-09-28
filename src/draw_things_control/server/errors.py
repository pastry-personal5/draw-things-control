"""Maps a ``DtcError``'s code to an HTTP status, as ``core/exit_codes.py``'s ``EXIT_CODES_BY_ERROR_CODE`` maps it to
an exit code, and turns one into the API's stable JSON error shape: a ``code``, a ``message``, and, when there is
one, the offending ``field``."""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.errors import DtcError, InputError, LimitExceededError
from draw_things_control.server.caller import DEFAULT_CALLER, resolve_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import is_authenticated

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


def validation_input_error(error: RequestValidationError) -> InputError:
    """A request FastAPI itself refused (a missing body field, ``?limit=x``) as the ``InputError`` it amounts to:
    the first problem's message, and the field it names (its location, less ``body``/``query``). A body that is not
    JSON at all names no field: its location's second part is a character offset, not a key."""
    problems = error.errors()
    first = problems[0] if problems else {}
    location = [] if first.get("type") == "json_invalid" else [str(part) for part in first.get("loc", ())[1:]]
    field = ".".join(location) or None
    message = f"'{field}': {first.get('msg', 'invalid')}" if field else str(first.get("msg", "invalid request"))
    return InputError(message, field=field)


def _audit_unparsed_submission(request: Request, error: InputError) -> None:
    """``POST /v1/queue``'s body (``{"job": ...}``) is the one audited endpoint whose validation FastAPI can refuse
    before the route body -- and so before its own ``audited()`` block -- ever runs (a path parameter like
    ``{queue_id}`` is always a plain string at this level; whatever it names is checked, and audited, inside the
    route body itself). Recorded here instead: headers are read independently of the body, so a valid
    ``X-Dtc-Caller`` is still recorded as itself, falling back to the documented default ``api`` only when that
    header is itself missing or unknown (as ``routes_queue.py``'s own ``_resolve_caller`` does). Never for an
    unauthenticated request, as ``require_auth``'s own 401 is never recorded either -- and FastAPI does not guarantee
    body validation runs after it, so this checks the token itself rather than assuming that order."""
    if request.method != "POST" or request.url.path != "/v1/queue":
        return
    if not is_authenticated(request, request.headers.get("authorization")):
        return
    context: ServerContext = request.app.state.context
    try:
        caller = resolve_caller(request.headers.get("x-dtc-caller"))
    except InputError:
        caller = DEFAULT_CALLER
    context.store.audit.record(action="submit", target=None, outcome=error.code, caller=caller, at=local_timestamp(datetime.now()))


def install_error_handler(app: FastAPI) -> None:
    """Register the handlers that turn any ``DtcError`` raised by a route, or a request FastAPI's own validation
    refused, into the API's JSON error shape."""

    @app.exception_handler(DtcError)
    async def handle_dtc_error(_request: Request, error: DtcError) -> JSONResponse:
        return JSONResponse(status_code=status_for_error(error), content=error_body(error))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        input_error = validation_input_error(error)
        _audit_unparsed_submission(request, input_error)
        return await handle_dtc_error(request, input_error)
