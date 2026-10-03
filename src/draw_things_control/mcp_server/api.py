"""The MCP server's HTTP client of ``dtc serve``'s API (Milestone 10): the bearer token, read and read again, the caller
header, the rule for an argument that becomes part of a URL path, and turning a transport failure, a 401, or an API
error into a tool's error, in the API's own error shape. ``mcp_server/`` imports nothing from the package, so reading
the token is a copy of ``core/client_config.py``'s ``read_client_token`` (a test checks the two agree)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

CALLER_HEADER = "X-Dtc-Caller"
# server/caller.py's name for this front end: the audit log records it, and the API keeps it off people's entries.
CALLER = "mcp"
REQUEST_TIMEOUT_SECONDS = 30.0


class ToolError(Exception):
    """A tool's failure, answered as an error result whose body is the API's error shape: ``code`` and ``message``,
    and, when there are some, ``field``, ``limit``, ``value``, and ``current_sha256``."""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("message", "")))
        self.body = body

    @property
    def code(self) -> str:
        return str(self.body.get("code", "error"))


@dataclass(frozen=True)
class ApiRequest:
    """One request to the API, built in full, path checks included, before anything is sent. ``params`` with a None
    value are left out; query values never go into the URL's text."""

    method: str
    path: str
    params: dict[str, Any] | None = None
    body: Any = None


def invalid_input(field: str, message: str) -> ToolError:
    """An argument refused before any request is made, naming it as the API would."""
    return ToolError({"code": "invalid_input", "message": message, "field": field})


def path_segment(argument: str, value: str) -> str:
    """``value`` as one segment of a URL path, so no argument changes which endpoint is called: refused when empty or
    only dots (``quote`` keeps dots, and ``httpx`` removes dot segments) or when it holds a slash (an ASGI server
    decodes ``%2F`` before it routes, so ``Q0001/watch`` would reach the watch), none of which an ID or a job file name
    can be; percent-encoded otherwise, ``?``, ``#``, and ``%`` included."""
    if not value.strip(".") or "/" in value:
        raise invalid_input(argument, f"'{argument}' must not be empty, only dots, or hold a slash")
    return quote(value, safe="")


def read_token(path: Path) -> str:
    """The bearer token at ``path``: the file's ASCII text, stripped, as ``read_client_token`` reads it; refused, as
    ``unauthorized`` naming the file and ``dtc serve``, when it is missing, unreadable, or empty."""
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        reason = error.strerror if isinstance(error, OSError) else "it is not ASCII text"
        raise ToolError({"code": "unauthorized", "message": f"Cannot read the server token file {path}: {reason}; is 'dtc serve' running?"}) from error
    if not token:
        raise ToolError({"code": "unauthorized", "message": f"The server token file {path} is empty; is 'dtc serve' running?"})
    return token


def _reason(error: Exception) -> str:
    return str(error) or type(error).__name__


class ApiClient:
    """One ``httpx`` client, open while a client of the MCP server is connected. The token is read at the first request, and again after
    any failure (a refused token, a connection error, a token file that cannot be read), so a regenerated token or a
    restarted server needs no restart of ``dtc mcp``. It is never logged or returned."""

    def __init__(self, server_url: str, token_path: Path, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.server_url = server_url
        self._token_path = token_path
        self._token: str | None = None
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.server_url, transport=self._transport, timeout=REQUEST_TIMEOUT_SECONDS, headers={CALLER_HEADER: CALLER})
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client and its connections, if open; the next request opens another."""
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    def auth_headers(self) -> dict[str, str]:
        """The ``Authorization`` header, reading the token when it is not held."""
        if self._token is None:
            self._token = read_token(self._token_path)
        return {"Authorization": f"Bearer {self._token}"}

    async def send(self, request: ApiRequest, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        """The API's JSON answer to ``request``; a ``ToolError`` for a failure. A request sent but not answered within
        ``timeout`` is ``server_timeout``, not ``server_unreachable``: it may have taken effect."""
        headers = self.auth_headers()
        query = {name: value for name, value in (request.params or {}).items() if value is not None}
        try:
            response = await self._http().request(request.method, request.path, params=query, json=request.body, headers=headers, timeout=timeout)
        except httpx.ConnectTimeout as error:
            raise self.unreachable(_reason(error)) from error
        except httpx.TimeoutException as error:
            raise self.timed_out(request, timeout) from error
        except httpx.TransportError as error:
            raise self.unreachable(_reason(error)) from error
        return self.answer(response)

    def timed_out(self, request: ApiRequest, timeout: float) -> ToolError:
        """``server_timeout``: a request sent and not answered. A read changes nothing; anything else may have taken
        effect."""
        if request.method == "GET":
            return ToolError({"code": "server_timeout", "message": f"dtc serve at {self.server_url} did not answer a read of {request.path} within {timeout:g} seconds; nothing else was sent"})
        return ToolError({"code": "server_timeout", "message": f"dtc serve at {self.server_url} did not answer within {timeout:g} seconds; the request may have taken effect, so read the queue or the job again before you retry"})

    async def get(self, path: str, params: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        return await self.send(ApiRequest("GET", path, params), timeout=timeout)

    @asynccontextmanager
    async def stream(self, path: str, *, silence: float) -> AsyncIterator[AsyncIterator[str]]:
        """The lines of a streamed ``GET path`` (the SSE watch). One that cannot open is the API's own answer; one that
        drops, or sends nothing for ``silence`` seconds (its read timeout, keep-alives included), is
        ``server_unreachable``."""
        headers = self.auth_headers()
        timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, read=silence)
        try:
            async with self._http().stream("GET", path, headers=headers, timeout=timeout) as response:
                if response.status_code != 200:
                    await response.aread()
                    self.answer(response)
                yield response.aiter_lines()
        except httpx.ReadTimeout as error:
            raise self.unreachable(f"it sent nothing on the watch for {silence:g} seconds") from error
        except httpx.TransportError as error:
            raise self.unreachable(_reason(error)) from error

    def answer(self, response: httpx.Response) -> dict[str, Any]:
        """The JSON body of a successful response; a ``ToolError`` with the API's own error body otherwise."""
        if response.status_code == 401:
            raise self.refused()
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_error:
            if isinstance(body, dict) and "code" in body:
                raise ToolError(body)
            raise ToolError({"code": "error", "message": f"dtc serve answered HTTP {response.status_code}"})
        if not isinstance(body, dict):
            raise ToolError({"code": "error", "message": "dtc serve answered something other than a JSON object"})
        return body

    def unreachable(self, reason: str) -> ToolError:
        """``server_unreachable``: the connection failed, dropped, or went silent, for ``reason``. The token is read again
        at the next request."""
        self._token = None
        return ToolError({"code": "server_unreachable", "message": f"Cannot reach dtc serve at {self.server_url} (--server-url): {reason}; is 'dtc serve' running?"})

    def refused(self) -> ToolError:
        """``unauthorized``: the API refused the token. It is read again at the next request."""
        self._token = None
        return ToolError({"code": "unauthorized", "message": f"dtc serve at {self.server_url} refused the token in {self._token_path}; is 'dtc serve' running with that token file?"})
