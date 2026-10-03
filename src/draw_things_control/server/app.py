"""The FastAPI app: authentication, the ``Host`` header check, routes, and the error-code-to-status table
(Milestone 02). Imported only inside the ``dtc serve`` command, as ``dtc tui`` imports Textual only inside its own
command (``cli/app.py``)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from draw_things_control.server import routes_audit, routes_executions, routes_health, routes_inputs, routes_job_files, routes_jobs, routes_queue
from draw_things_control.server.body_limit import BodyLimit
from draw_things_control.server.context import ServerContext
from draw_things_control.server.dependencies import require_auth
from draw_things_control.server.errors import UnexpectedErrorBoundary, install_error_handler
from draw_things_control.server.host_check import host_header_is_allowed


def create_app(context: ServerContext) -> FastAPI:
    """Build the app for one ``dtc serve`` process. The OpenAPI schema is served at ``/v1/openapi.json``, behind
    the token; the interactive docs are off."""
    app = FastAPI(title="draw-things-control", openapi_url=None, docs_url=None, redoc_url=None)
    app.state.context = context
    install_error_handler(app)
    _install_host_check(app, context)
    app.add_middleware(BodyLimit, context=context)
    app.add_middleware(UnexpectedErrorBoundary)
    for router in (routes_health.health_router, routes_health.capabilities_router, routes_jobs.router, routes_job_files.validation_router, routes_inputs.router, routes_executions.router, routes_audit.router, routes_queue.router):
        app.include_router(router)
    if context.allow_write:
        app.include_router(routes_job_files.write_router)

    @app.get("/v1/openapi.json", dependencies=[Depends(require_auth)])
    def openapi_schema() -> dict[str, object]:
        return app.openapi()

    return app


def _install_host_check(app: FastAPI, context: ServerContext) -> None:
    @app.middleware("http")
    async def check_host(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        header = request.headers.get("host")
        if not host_header_is_allowed(header, bound_host=context.bound_host, bound_port=context.bound_port):
            return JSONResponse(status_code=400, content={"code": "invalid_input", "message": "The Host header does not name this server"})
        return await call_next(request)
