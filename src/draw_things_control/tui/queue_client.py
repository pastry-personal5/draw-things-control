"""The TUI's own async HTTP client for the queue commands (``/queue add``, ``/apply``, ``/queue cancel``,
``/queue resume``, ``c``, and, from Milestone 05, ``/queue park``, ``unpark``, ``hold``, and ``release``, ``p``, and
``u``), and, from Milestone 06, ``/delete`` and ``d``: submits, cancels, resumes, parks, holds, and deletes through the API, exactly as ``dtc queue`` does over its own
synchronous client (``cli/queue_app.py``) -- a small amount of the same shape duplicated once per front end, since
front ends never import each other (``tests/test_architecture.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from draw_things_control.core.client_config import read_client_token
from draw_things_control.core.errors import DtcError
from draw_things_control.services.queue_hold import HoldState
from draw_things_control.tui.client import CALLER, CALLER_HEADER

if TYPE_CHECKING:
    import httpx


class ApiError(Exception):
    """A request the API refused, or could not be reached at all; ``text`` is what a message should show."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


async def _client(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None") -> "httpx.AsyncClient":
    import httpx

    try:
        token = read_client_token(token_file)
    except DtcError as error:
        raise ApiError(str(error)) from error
    return httpx.AsyncClient(base_url=server_url, transport=transport, headers={"Authorization": f"Bearer {token}", CALLER_HEADER: CALLER}, timeout=30.0)


async def _request(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    import httpx

    async with await _client(server_url, token_file, transport) as client:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise ApiError(f"Cannot reach the server at {server_url}: {error}; is 'dtc serve' running?") from error
    if response.status_code == 401:
        raise ApiError(f"The server at {server_url} refused the token; is 'dtc serve' running with the same --token-file?")
    if response.is_error:
        raise ApiError(_error_text(response))
    return response.json()


def _error_text(response: "httpx.Response") -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = body.get("message") or response.text or f"HTTP {response.status_code}"
    field = body.get("field")
    return f"'{field}': {message}" if field else message


async def submit(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", job: str) -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", "/v1/queue", json={"job": job})


async def cancel(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", queue_id: str) -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", f"/v1/queue/{queue_id}/cancel")


async def resume(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", queue_id: str) -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", f"/v1/queue/{queue_id}/resume")


async def show(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", queue_id: str) -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "GET", f"/v1/queue/{queue_id}")


async def park(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", queue_id: str) -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", f"/v1/queue/{queue_id}/park")


async def unpark(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", queue_id: str) -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", f"/v1/queue/{queue_id}/unpark")


async def hold(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None") -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", "/v1/queue/hold")


async def release(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None") -> dict[str, Any]:
    return await _request(server_url, token_file, transport, "POST", "/v1/queue/release")


async def list_queue(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", *, limit: int = 5) -> tuple[list[dict[str, Any]], HoldState]:
    """The entries, and the queue's hold beside them."""
    body = await _request(server_url, token_file, transport, "GET", "/v1/queue", params={"limit": limit})
    return body["queue"], HoldState.from_body(body)


async def delete_executions(server_url: str, token_file: Path, transport: "httpx.AsyncBaseTransport | None", execution_ids: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    """One ``POST /v1/executions/delete`` of up to 200 IDs; ``dry_run`` says what it would do now."""
    return await _request(server_url, token_file, transport, "POST", "/v1/executions/delete", json={"executions": execution_ids, "dry_run": dry_run})
