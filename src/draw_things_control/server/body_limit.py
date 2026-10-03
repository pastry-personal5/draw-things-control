"""Bound HTTP bodies before routing or parsing, including streamed requests."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from draw_things_control.core.clock import local_timestamp
from draw_things_control.core.errors import LimitExceededError
from draw_things_control.server.caller import audit_caller
from draw_things_control.server.context import ServerContext
from draw_things_control.server.errors import error_body, write_action
from draw_things_control.server.token_file import token_matches


class BodyLimit:
    def __init__(self, app: ASGIApp, context: ServerContext) -> None:
        self.app = app
        self.context = context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = 8 * self.context.global_config.api_limits.max_job_file_bytes
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        length = headers.get(b"content-length", b"").decode("ascii", errors="ignore")
        if length.isdecimal():
            digits = length.lstrip("0") or "0"
            declared = int(digits) if len(digits) <= len(str(limit)) else limit + 1
            if declared > limit:
                await self._refuse(scope, send, limit, declared, headers)
                return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            size += len(part)
            if size > limit:
                await self._refuse(scope, send, limit, size, headers)
                return
            chunks.append(part)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        first = True

        async def replay() -> Message:
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _refuse(self, scope: Scope, send: Send, limit: int, size: int, headers: dict[bytes, bytes]) -> None:
        error = LimitExceededError(f"request body is over the limit of {limit} bytes", key="max_job_file_bytes", limit=limit, value=size)
        action = write_action(scope["method"], scope["path"], scope.get("query_string", b""), self.context.allow_write)
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        presented = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        if action is not None and token_matches(self.context.token, presented):
            from datetime import datetime

            caller, _error = audit_caller(headers.get(b"x-dtc-caller", b"").decode("latin-1") if b"x-dtc-caller" in headers else None)
            self.context.store.audit.record(action=action, target=None, outcome=error.code, caller=caller, at=local_timestamp(datetime.now()))
        response = JSONResponse(status_code=413, content=error_body(error))
        await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}
