"""``dtc queue``: a client of the HTTP API (Milestone 03), replacing `run-job`'s own direct execution now that
`dtc serve`'s worker is the only thing that ever starts `draw-things-cli`. The HTTP client is ``cli/api_client.py``,
shared with ``dtc history`` (Milestone 06); the gRPC client ``add --wait`` uses is ``cli/queue_wait.py``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from draw_things_control.cli.api_client import AllowRemoteServerOption, QueueIdArgument, ServerUrlOption, TokenFileOption, api_client, api_request
from draw_things_control.cli.context import errors_exit, services_of
from draw_things_control.core.client_config import DEFAULT_SERVER_URL, job_argument
from draw_things_control.services.queue_hold import HoldState, hold_outcome_text, hold_text, release_outcome_text
from draw_things_control.services.queue_park_text import park_outcome_text, queue_state_text, unpark_outcome_text

queue_app = typer.Typer(help="Submit and watch jobs through the queue; dtc serve is what actually runs them.")


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
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = api_request(client, "POST", "/v1/queue", json={"job": job_argument(job, services.paths)}).json()
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
    state: Annotated[str | None, typer.Option("--state", help="Filter by state: queued, running, succeeded, failed, cancelled, interrupted, or parked.")] = None,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """List the queue's entries: running and queued first, then finished ones, newest first."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        body = api_request(client, "GET", "/v1/queue", params={"state": state} if state is not None else None).json()
    entries = body["queue"]
    hold = HoldState.from_body(body)
    if hold.held:
        typer.echo(hold_text(hold))
    if not entries:
        typer.echo("No queue entries.")
        return
    rows = [("ID", "STATE", "RUNS", "JOB"), *((row["queue_id"], queue_state_text(row), _runs_text(row), Path(row["job_path"]).name) for row in entries)]
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for row in rows:
        typer.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


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
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = api_request(client, "GET", f"/v1/queue/{queue_id}").json()
    typer.echo(f"{entry['queue_id']}: {queue_state_text(entry)} ({Path(entry['job_path']).name})")
    if entry.get("execution_id"):
        typer.echo(f"  execution: {entry['execution_id']}")
    typer.echo(f"  runs: {_runs_text(entry)}" + (f", run {entry['current_run']} in progress" if entry.get("current_run") is not None else ""))
    if entry.get("cooldown_until") is not None:
        typer.echo(f"  cooldown until: {entry['cooldown_until']}")
    if entry.get("park_requested"):
        typer.echo("  park reservation: yes")
    if entry.get("error"):
        typer.echo(f"  error: {entry['error']}")
    typer.echo(f"  resumable: from run {entry['resume_from_run']}" if entry.get("resumable") else f"  resumable: no ({entry.get('resume_refused_reason')})")
    hold = HoldState.from_body(entry)
    if hold.held:
        typer.echo(f"  {hold_text(hold)}")


@queue_app.command("cancel")
def queue_cancel(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Cancel a queued or running entry; a running one stops at once, losing its current run ('dtc queue park' keeps it)."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = api_request(client, "POST", f"/v1/queue/{queue_id}/cancel").json()
    typer.echo(f"{entry['queue_id']} {entry['state']}")


@queue_app.command("resume")
def queue_resume(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Resume an interrupted, failed, cancelled, or parked entry from its last succeeded run, as a new queued entry."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = api_request(client, "POST", f"/v1/queue/{queue_id}/resume").json()
    typer.echo(f"{entry['queue_id']} queued (resumed from {queue_id}): {Path(entry['job_path']).name}")


@queue_app.command("park")
def queue_park(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Park a running entry: it ends once its current run finishes, keeping every run, and the queue is held."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = api_request(client, "POST", f"/v1/queue/{queue_id}/park").json()
    typer.echo(park_outcome_text(entry, "dtc queue release"))


@queue_app.command("unpark")
def queue_unpark(
    ctx: typer.Context,
    queue_id: QueueIdArgument,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Withdraw a running entry's park reservation: it runs on, and the hold its reservation made is released."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        entry = api_request(client, "POST", f"/v1/queue/{queue_id}/unpark").json()
    typer.echo(unpark_outcome_text(entry))


@queue_app.command("hold")
def queue_hold(
    ctx: typer.Context,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Hold the queue: a running job is not stopped, and nothing else starts until 'dtc queue release'."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        body = api_request(client, "POST", "/v1/queue/hold").json()
    typer.echo(hold_outcome_text(body))


@queue_app.command("release")
def queue_release(
    ctx: typer.Context,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """End the hold: the oldest queued entry starts at once."""
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        body = api_request(client, "POST", "/v1/queue/release").json()
    typer.echo(release_outcome_text(body))
