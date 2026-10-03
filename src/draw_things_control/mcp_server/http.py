"""``dtc mcp --transport streamable-http`` (Milestone 13): the same MCP server over Streamable HTTP at ``/mcp``, for a
client that cannot spawn a process on this machine, such as an agent in a virtual machine. It adds no tool and no
authority: every request still ends in ``dtc serve``'s API, with the token in the server's own token file. What it adds is
a listener, so every request must carry that token, as ``dtc serve``'s own routes require it."""

from __future__ import annotations

import hmac
import json
import socket
from pathlib import Path

import uvicorn
from loguru import logger
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from draw_things_control.mcp_server.api import ToolError, read_token

MCP_PATH = "/mcp"
UNAUTHORIZED_BODY = json.dumps({"code": "unauthorized", "message": "A valid bearer token is required"}, separators=(",", ":")).encode("ascii")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_HTTP_BODY_BYTES = 8 * 1024 * 1024


class BindError(OSError):
    """The listening address could not be bound (taken, not this machine's, or not resolvable)."""


class BearerAuth:
    """Pure ASGI: every HTTP request must carry ``Authorization: Bearer <the token in the token file>``, else it is
    answered 401 before the MCP app sees it. The file is read again for each request, so a token ``dtc serve``
    regenerated is the one that counts, as for the API client. The answer says nothing of why (the reason, which can
    name a path, goes to the log): a caller without the token learns nothing from it."""

    def __init__(self, app: ASGIApp, token_path: Path) -> None:
        self._app = app
        self._token_path = token_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._permitted(scope):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("ascii")), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})
            return
        await self._app(scope, receive, send)

    def _permitted(self, scope: Scope) -> bool:
        supplied = next((value.decode("latin-1") for name, value in scope["headers"] if name == b"authorization"), "")
        scheme, _, credentials = supplied.partition(" ")
        if scheme.lower() != "bearer" or not credentials:
            return False
        try:
            expected = read_token(self._token_path)
        except ToolError as error:
            logger.warning("Refusing an MCP request: {}", error)
            return False
        return hmac.compare_digest(credentials.strip().encode("utf-8"), expected.encode("utf-8"))


class McpBodyLimit:
    """Refuse oversized JSON-RPC bodies before the SDK sees any part of them."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        length = next((value for name, value in scope.get("headers", []) if name.lower() == b"content-length"), b"")
        if length.isdigit():
            digits = length.lstrip(b"0") or b"0"
            declared = int(digits) if len(digits) <= len(str(MAX_HTTP_BODY_BYTES)) else MAX_HTTP_BODY_BYTES + 1
            if declared > MAX_HTTP_BODY_BYTES:
                await self._refuse(scope, send, declared)
                return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            size += len(part)
            if size > MAX_HTTP_BODY_BYTES:
                await self._refuse(scope, send, size)
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

        await self._app(scope, replay, send)

    async def _refuse(self, scope: Scope, send: Send, size: int) -> None:
        body = json.dumps({"code": "limit_exceeded", "message": "MCP request body is over the 8 MiB limit", "limit": MAX_HTTP_BODY_BYTES, "value": size}, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})


def http_app(server: Server, token_path: Path, host: str) -> ASGIApp:
    """The SDK's Streamable HTTP app of ``server``, behind ``BearerAuth``. A loopback bind keeps the SDK's own check of
    the ``Host`` and ``Origin`` headers; beyond loopback, the clients' addresses are not known here, and the token is
    what keeps a caller out."""
    security = None if host in LOOPBACK_HOSTS else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return BearerAuth(McpBodyLimit(server.streamable_http_app(streamable_http_path=MCP_PATH, host=host, transport_security=security, max_request_body_size=MAX_HTTP_BODY_BYTES)), token_path)


def bind(host: str, port: int) -> socket.socket:
    """The listening socket, bound here so that a taken port or an address this machine lacks is a ``BindError``, not
    uvicorn's exit."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        return socket.create_server((host, port), family=family)
    except OSError as error:
        raise BindError(f"Cannot listen on {host}:{port}: {error.strerror or error}") from error


async def serve_http(server: Server, token_path: Path, host: str, port: int) -> None:
    """Serve ``server`` at ``http://host:port/mcp`` until a signal stops it (uvicorn handles them). ``ws="none"``, so
    the app sees only HTTP and the lifespan."""
    sock = bind(host, port)
    config = uvicorn.Config(http_app(server, token_path, host), log_config=None, ws="none")
    logger.info("MCP over Streamable HTTP at http://{}:{}{}", f"[{host}]" if ":" in host else host, port, MCP_PATH)
    try:
        await uvicorn.Server(config).serve(sockets=[sock])
    finally:
        sock.close()
