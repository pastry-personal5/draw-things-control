"""Maps a ``DtcError``'s code to an HTTP status, as ``core/exit_codes.py``'s ``EXIT_CODES_BY_ERROR_CODE`` maps it to
an exit code, and turns one into the API's stable JSON error shape: a ``code``, a ``message``, and, when there is
one, the offending ``field``."""

from __future__ import annotations

import traceback
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.errors import DtcError, InputError, LimitExceededError
from draw_things_control.server.caller import audit_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import is_authenticated

STATUS_BY_ERROR_CODE = {
    "invalid_input": 422,
    "timeout_required": 422,
    "outside_directory": 422,
    "limit_exceeded": 422,
    "not_permitted": 403,
    "not_found": 404,
    "invalid_state": 409,
    "conflict": 409,
    "busy": 409,
    "tool_missing": 503,
    "state_unavailable": 503,
    "internal_error": 500,
}
DEFAULT_STATUS = 503
# Audited POST endpoints with body fields FastAPI can refuse before the route runs.
AUDITED_BODY_ACTIONS = {"/v1/queue": "submit", "/v1/generations": "generate", "/v1/executions/delete": "delete_execution"}
QUEUE_CONTROL_ACTIONS = {"hold": "hold", "release": "release", "cancel": "cancel", "resume": "resume", "park": "park", "unpark": "unpark"}


def write_action(method: str, path: str, query: bytes, allow_write: bool) -> str | None:
    path = path.removesuffix("/") if path != "/" else path
    if method == "POST":
        action = AUDITED_BODY_ACTIONS.get(path)
        if action is not None:
            return action
        parts = path.split("/")
        if parts[:3] == ["", "v1", "queue"]:
            if len(parts) == 4 and parts[3] in {"hold", "release"}:
                return QUEUE_CONTROL_ACTIONS[parts[3]]
            if len(parts) == 5 and parts[3] and parts[4] in {"cancel", "resume", "park", "unpark"}:
                return QUEUE_CONTROL_ACTIONS[parts[4]]
    if allow_write and path.startswith("/v1/jobs/") and path.count("/") == 3:
        if method == "PUT":
            return "replace_job" if parse_qs(query.decode("latin-1"), keep_blank_values=True).get("overwrite", [])[-1:] == ["1"] else "create_job"
        if method == "DELETE":
            return "delete_job"
    return None


def log_unexpected(error: Exception) -> None:
    frames = traceback.extract_tb(error.__traceback__)
    location = " -> ".join(f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames[-8:])
    logger.error("Unhandled {} at {}", type(error).__name__, location)


class UnexpectedErrorBoundary:
    """Catch route failures inside Starlette's server wrapper so uvicorn never logs their messages."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        completed = False

        async def mark_started(message: Message) -> None:
            nonlocal started, completed
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
            await send(message)

        try:
            await self.app(scope, receive, mark_started)
        except Exception as error:
            log_unexpected(error)
            if not started:
                response = JSONResponse(status_code=500, content={"code": "internal_error", "message": "Internal error; see the server log"})
                await response(scope, receive, send)
            elif not completed:
                await send({"type": "http.response.body", "body": b"", "more_body": False})


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
    current_sha256 = getattr(error, "current_sha256", None)
    if current_sha256 is not None:
        body["current_sha256"] = current_sha256
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


def _audit_unparsed_body(request: Request, error: InputError) -> None:
    """Record an authenticated write that FastAPI refuses before its route's own audit block runs. Read the
    caller from the header, without trusting body validation to have checked authentication first."""
    action = write_action(request.method, request.url.path, request.scope.get("query_string", b""), request.app.state.context.allow_write)
    if action is None:
        return
    if not is_authenticated(request, request.headers.get("authorization")):
        return
    context: ServerContext = request.app.state.context
    caller, _error = audit_caller(request.headers.get("x-dtc-caller"))
    context.store.audit.record(action=action, target=None, outcome=error.code, caller=caller, at=local_timestamp(datetime.now()))


def install_error_handler(app: FastAPI) -> None:
    """Register the handlers that turn any ``DtcError`` raised by a route, or a request FastAPI's own validation
    refused, into the API's JSON error shape."""

    @app.exception_handler(DtcError)
    async def handle_dtc_error(_request: Request, error: DtcError) -> JSONResponse:
        return JSONResponse(status_code=status_for_error(error), content=error_body(error))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        input_error = validation_input_error(error)
        _audit_unparsed_body(request, input_error)
        return await handle_dtc_error(request, input_error)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, error: StarletteHTTPException) -> JSONResponse:
        if error.status_code == 400:
            input_error = InputError("Invalid request body")
            _audit_unparsed_body(request, input_error)
            return JSONResponse(status_code=400, content=error_body(input_error), headers=error.headers)
        if error.status_code == 405:
            writes_off = request.method in {"PUT", "DELETE"} and request.url.path.startswith("/v1/jobs/") and not request.app.state.context.allow_write
            return JSONResponse(status_code=405, content={"code": "writes_off" if writes_off else "method_not_allowed", "message": "Writes to data/jobs/ require dtc serve --allow-write" if writes_off else "Method not allowed"}, headers=error.headers)
        if error.status_code == 404:
            return JSONResponse(status_code=404, content={"code": "not_found", "message": "Not found"})
        if error.status_code == 401:
            return JSONResponse(status_code=401, content={"code": "unauthorized", "message": "A valid bearer token is required"}, headers=error.headers)
        return JSONResponse(status_code=error.status_code, content={"code": "http_error", "message": "Request refused"}, headers=error.headers)
