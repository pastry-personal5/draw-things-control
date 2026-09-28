"""FastAPI dependencies shared by the routes: bearer-token authentication, the caller header, and pagination."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from draw_things_control.server.caller import resolve_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.pagination import clamp_limit, decode_cursor
from draw_things_control.server.token_file import token_matches


def get_context(request: Request) -> ServerContext:
    context: ServerContext = request.app.state.context
    return context


def require_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Every endpoint but ``GET /health`` requires ``Authorization: Bearer <token>``, checked in constant time.
    Missing or wrong is 401 with no detail. FastAPI never reads a header dependency from the query string, so a
    token there is never accepted."""
    context: ServerContext = request.app.state.context
    presented = authorization.removeprefix("Bearer ") if authorization is not None and authorization.startswith("Bearer ") else None
    if not token_matches(context.token, presented):
        raise HTTPException(status_code=401)


def get_caller(x_dtc_caller: str | None = Header(default=None, alias="X-Dtc-Caller")) -> str:
    """The caller name the audit log stores, from ``X-Dtc-Caller``; only used on the endpoints the audit log
    covers, so a plain read never fails a ``GET``."""
    return resolve_caller(x_dtc_caller)


@dataclass(frozen=True)
class Page:
    offset: int
    limit: int


def get_page(limit: int | None = None, cursor: str | None = None) -> Page:
    return Page(offset=decode_cursor(cursor), limit=clamp_limit(limit))
