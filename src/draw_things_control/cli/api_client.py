"""The HTTP client of the commands that call ``dtc serve``'s API (``dtc queue``, and, from Milestone 06, ``dtc history``):
its options, and how an unreachable server, a refused token, or an API error ends a command. ``httpx`` is imported
only inside the functions, so a command that never calls the API does not load it."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from loguru import logger

from draw_things_control.cli.context import services_of
from draw_things_control.core.client_config import check_server_host, read_client_token
from draw_things_control.core.exit_codes import EXIT_CODES_BY_ERROR_CODE, EXIT_INVALID_INPUT, EXIT_STATE_UNAVAILABLE
from draw_things_control.state.ids import QUEUE_LETTER, parse_typed_id, queue_id_text

if TYPE_CHECKING:
    import httpx

CALLER_HEADER = "X-Dtc-Caller"
ServerUrlOption = Annotated[str, typer.Option("--server-url", help="The dtc serve HTTP API; refused unless loopback, or --allow-remote-server is given.")]
TokenFileOption = Annotated[Path | None, typer.Option("--token-file", help="The API's bearer token file; default: config/server-token in the project.")]
AllowRemoteServerOption = Annotated[bool, typer.Option("--allow-remote-server", help="Allow --server-url beyond loopback; the token then crosses the network in plain HTTP.")]
QueueIdArgument = Annotated[str, typer.Argument(help="A queue entry's ID (Q0007).")]


def checked_queue_id(value: str) -> str:
    number = parse_typed_id(value, QUEUE_LETTER)
    if number is None:
        logger.error("'queue_id' must be a queue entry ID such as Q0007")
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    return queue_id_text(number)


def api_client(ctx: typer.Context, server_url: str, token_file: Path | None, allow_remote_server: bool) -> "httpx.Client":
    import httpx

    services = services_of(ctx)
    check_server_host(server_url, allow_remote_server=allow_remote_server)
    token = read_client_token(token_file or services.paths.server_token)
    return httpx.Client(base_url=server_url, transport=services.http_transport, headers={"Authorization": f"Bearer {token}", CALLER_HEADER: "cli"}, timeout=30.0)


def api_request(client: "httpx.Client", method: str, url: str, **kwargs: Any) -> "httpx.Response":
    import httpx

    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        logger.error("Cannot reach the server at {}: {}; is 'dtc serve' running?", client.base_url, error)
        raise typer.Exit(code=EXIT_STATE_UNAVAILABLE) from error
    if response.status_code == 401:
        logger.error("The server at {} refused the token; is 'dtc serve' running with the same --token-file?", client.base_url)
        raise typer.Exit(code=EXIT_STATE_UNAVAILABLE)
    if response.is_error:
        exit_on_api_error(response)
    return response


def exit_on_api_error(response: "httpx.Response") -> NoReturn:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("code", "error")
    message = body.get("message") or response.text or f"HTTP {response.status_code}"
    field = body.get("field")
    logger.error("{}", f"'{field}': {message}" if field else message)
    raise typer.Exit(code=EXIT_CODES_BY_ERROR_CODE.get(code, EXIT_INVALID_INPUT))
