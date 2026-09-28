"""``dtc queue``: a client of the HTTP API (Milestone 03), replacing `run-job`'s own direct execution now that
`dtc serve`'s worker is the only thing that ever starts `draw-things-cli`. The HTTP client lives here, importing
``httpx`` only inside the commands, beside the gRPC client ``add --wait`` uses (``cli/queue_wait.py``)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from loguru import logger

from draw_things_control.cli.context import errors_exit, services_of
from draw_things_control.core.client_config import DEFAULT_SERVER_URL, check_server_host, job_argument, read_client_token
from draw_things_control.core.exit_codes import EXIT_CODES_BY_ERROR_CODE, EXIT_INVALID_INPUT, EXIT_STATE_UNAVAILABLE

if TYPE_CHECKING:
    import httpx

queue_app = typer.Typer(help="Submit and watch jobs through the queue; dtc serve is what actually runs them.")

CALLER_HEADER = "X-Dtc-Caller"
ServerUrlOption = Annotated[str, typer.Option("--server-url", help="The dtc serve HTTP API; refused unless loopback, or --allow-remote-server is given.")]
TokenFileOption = Annotated[Path | None, typer.Option("--token-file", help="The API's bearer token file; default: config/server-token in the project.")]
AllowRemoteServerOption = Annotated[bool, typer.Option("--allow-remote-server", help="Allow --server-url beyond loopback; the token then crosses the network in plain HTTP.")]
QueueIdArgument = Annotated[str, typer.Argument(help="A queue entry's ID (Q0007).")]


def _client(ctx: typer.Context, server_url: str, token_file: Path | None, allow_remote_server: bool) -> "httpx.Client":
    import httpx

    services = services_of(ctx)
    check_server_host(server_url, allow_remote_server=allow_remote_server)
    token = read_client_token(token_file or services.paths.server_token)
    return httpx.Client(base_url=server_url, transport=services.http_transport, headers={"Authorization": f"Bearer {token}", CALLER_HEADER: "cli"}, timeout=30.0)


def _request(client: "httpx.Client", method: str, url: str, **kwargs: Any) -> "httpx.Response":
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
        _exit_on_api_error(response)
    return response


def _exit_on_api_error(response: "httpx.Response") -> NoReturn:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("code", "error")
    message = body.get("message") or response.text or f"HTTP {response.status_code}"
    field = body.get("field")
    logger.error("{}", f"'{field}': {message}" if field else message)
    raise typer.Exit(code=EXIT_CODES_BY_ERROR_CODE.get(code, EXIT_INVALID_INPUT))


def _runs_text(row: dict[str, Any]) -> str:
    total = row.get("total_runs")
    return "-" if total is None else f"{row['succeeded']}/{total}"


@queue_app.command("add")
def queue_add(
    ctx: typer.Context,
    job: Annotated[str, typer.Argument(help="A job ID, a file name in data/jobs/, or a path directly in data/jobs/.")],
    wait: Annotated[bool, typer.Option("--wait", help="Watch the entry until it finishes, printing each run's start and its outcome.")] = False,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Submit a job to the queue; dtc serve runs it."""
    services = services_of(ctx)
    with errors_exit():
        client = _client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = _request(client, "POST", "/v1/queue", json={"job": job_argument(job, services.paths)}).json()
        typer.echo(f"{entry['queue_id']} queued: {Path(entry['job_path']).name}")
        if not wait:
            return
        from draw_things_control.cli.queue_wait import wait_for_entry

        exit_code = wait_for_entry(client, entry["queue_id"], services.grpc_stub_factory)
    if exit_code:
        raise typer.Exit(code=exit_code)


@queue_app.command("list")
def queue_list(
    ctx: typer.Context,
    state: Annotated[str | None, typer.Option("--state", help="Filter by state: queued, running, succeeded, failed, cancelled, or interrupted.")] = None,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """List the queue's entries: running and queued first, then finished ones, newest first."""
    with errors_exit():
        client = _client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entries = _request(client, "GET", "/v1/queue", params={"state": state} if state is not None else None).json()["queue"]
    if not entries:
        typer.echo("No queue entries.")
        return
    rows = [("ID", "STATE", "RUNS", "JOB"), *((row["queue_id"], row["state"], _runs_text(row), Path(row["job_path"]).name) for row in entries)]
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for row in rows:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))


@queue_app.command("show")
def queue_show(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Show one queue entry: its state, execution, current run, and whether it can be resumed."""
    with errors_exit():
        client = _client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = _request(client, "GET", f"/v1/queue/{queue_id}").json()
    typer.echo(f"{entry['queue_id']}: {entry['state']} ({Path(entry['job_path']).name})")
    if entry.get("execution_id"):
        typer.echo(f"  execution: {entry['execution_id']}")
    typer.echo(f"  runs: {_runs_text(entry)}" + (f", run {entry['current_run']} in progress" if entry.get("current_run") is not None else ""))
    if entry.get("cooldown_until") is not None:
        typer.echo(f"  cooldown until: {entry['cooldown_until']}")
    if entry.get("error"):
        typer.echo(f"  error: {entry['error']}")
    typer.echo(f"  resumable: from run {entry['resume_from_run']}" if entry.get("resumable") else f"  resumable: no ({entry.get('resume_refused_reason')})")


@queue_app.command("cancel")
def queue_cancel(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Cancel a queued or running entry; a running one stops at once, losing its current run."""
    with errors_exit():
        client = _client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = _request(client, "POST", f"/v1/queue/{queue_id}/cancel").json()
    typer.echo(f"{entry['queue_id']} {entry['state']}")


@queue_app.command("resume")
def queue_resume(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Resume an interrupted, failed, or cancelled entry from its last succeeded run, as a new queued entry."""
    with errors_exit():
        client = _client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = _request(client, "POST", f"/v1/queue/{queue_id}/resume").json()
    typer.echo(f"{entry['queue_id']} queued (resumed from {queue_id}): {Path(entry['job_path']).name}")
