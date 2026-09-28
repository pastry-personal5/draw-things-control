"""FastAPI dependencies shared by the routes: bearer-token authentication and pagination. The ``X-Dtc-Caller``
header is resolved by each audited route itself (``routes_queue.py``), not a shared dependency here: an unknown
value must still be recorded (Milestone 02's audit fix, phase-3 changelog 2026-09-28), which means raising it inside
the route's own ``audited()`` block rather than as a dependency that fails before that block ever opens."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from draw_things_control.server.context import ServerContext
from draw_things_control.server.pagination import clamp_limit, decode_cursor
from draw_things_control.server.token_file import token_matches


def get_context(request: Request) -> ServerContext:
    context: ServerContext = request.app.state.context
    return context


def is_authenticated(request: Request, authorization: str | None) -> bool:
    """Whether ``authorization`` (the raw ``Authorization`` header) is a valid ``Bearer <token>`` for this server,
    checked in constant time. Shared by ``require_auth`` and ``errors.py``'s own pre-route audit of a validation
    failure that never reached ``require_auth`` as a dependency: that audit must still never record an
    unauthenticated request, exactly as ``require_auth``'s own 401 is never recorded."""
    context: ServerContext = request.app.state.context
    presented = authorization.removeprefix("Bearer ") if authorization is not None and authorization.startswith("Bearer ") else None
    return token_matches(context.token, presented)


def require_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Every endpoint but ``GET /health`` requires ``Authorization: Bearer <token>``, checked in constant time.
    Missing or wrong is 401 with no detail. FastAPI never reads a header dependency from the query string, so a
    token there is never accepted."""
    if not is_authenticated(request, authorization):
        raise HTTPException(status_code=401)


@dataclass(frozen=True)
class Page:
    offset: int
    limit: int


def get_page(limit: int | None = None, cursor: str | None = None) -> Page:
    return Page(offset=decode_cursor(cursor), limit=clamp_limit(limit))
