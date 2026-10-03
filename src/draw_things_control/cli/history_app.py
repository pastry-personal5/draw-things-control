"""``dtc history``: deleting executions from the history (Milestone 06), through dtc serve's API, as the TUI does. The
selection is read in full, and checked with a dry run of the same endpoint, before anything is deleted."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any

import typer
from loguru import logger

from draw_things_control.cli.api_client import AllowRemoteServerOption, ServerUrlOption, TokenFileOption, api_client, api_request
from draw_things_control.cli.context import errors_exit, services_of
from draw_things_control.core.client_config import DEFAULT_SERVER_URL
from draw_things_control.core.exit_codes import EXIT_INVALID_INPUT
from draw_things_control.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id

if TYPE_CHECKING:
    import httpx

history_app = typer.Typer(help="Delete executions from the history; dtc serve's API does the deleting.")

# The most IDs one request to POST /v1/executions/delete may name (the server's MAX_LIMIT).
DELETE_BATCH = 200


@history_app.command("delete")
def history_delete(
    ctx: typer.Context,
    executions: Annotated[list[str] | None, typer.Argument(help="Execution IDs (E0012).", show_default=False)] = None,
    status: Annotated[str | None, typer.Option("--status", help="Every execution with this status: succeeded, failed, interrupted, parked, or running.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Every execution whose job name or file name contains TEXT, as the TUI's /filter name matches.", metavar="TEXT")] = None,
    all_executions: Annotated[bool, typer.Option("--all", help="The whole history.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Delete without asking.")] = False,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Delete executions: their rows, runs, logs, and manifests; their outputs stay. Asks first, unless --yes."""
    # An empty filter would match every execution: the whole history needs --all written out.
    for option, value in (("--status", status), ("--name", name)):
        if value is not None and not value.strip():
            logger.error("{} must not be empty; use --all to delete the whole history", option)
            raise typer.Exit(code=EXIT_INVALID_INPUT)
    chosen = [bool(executions), status is not None or name is not None, all_executions]
    if sum(chosen) != 1:
        logger.error("{}", "Give execution IDs, filters (--status, --name), or --all, not a mix" if any(chosen) else "Nothing to delete: give execution IDs, --status or --name, or --all")
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        summaries: dict[str, dict[str, Any]] = {}
        if executions:
            # Written as the server answers (E0012), so a listing line and its answer match.
            ids = list(dict.fromkeys(_normalized(execution) for execution in executions))
        else:
            summaries = {row["execution_id"]: row for row in _read_every_page(client, status, name)}
            ids = list(summaries)
            if not ids:
                typer.echo("No executions match the filter" if not all_executions else "The history is empty")
                return
        plan = _delete_in_batches(client, ids, dry_run=True)
        unable = _unable_lines(plan)
        for line in unable:
            typer.echo(line)
        if not plan["deleted"]:
            typer.echo("Nothing was deleted")
            raise typer.Exit(code=EXIT_INVALID_INPUT)
        ended = _resumes_ended(plan)
        for execution_id in plan["deleted"]:
            typer.echo(_listing_line(execution_id, summaries.get(execution_id), ended.get(execution_id, [])))
        count = len(plan["deleted"])
        if not yes:
            if not services_of(ctx).stdin_is_terminal():
                logger.error("Not asking without a terminal; give --yes to delete")
                raise typer.Exit(code=EXIT_INVALID_INPUT)
            if not typer.confirm(f"Delete {count} execution{'s' if count != 1 else ''}?", default=False):
                typer.echo("Nothing was deleted")
                return
        report = _empty_report()
        try:
            _delete_in_batches(client, plan["deleted"], dry_run=False, merged=report)
        except typer.Exit:
            # A request that failed partway: what the ones before it deleted, and what that ended, still stands.
            if report["deleted"]:
                _say_deleted(report)
            raise
    _report(report, unable)


def _normalized(text: str) -> str:
    number = parse_typed_id(text, EXECUTION_LETTER)
    if number is None:
        logger.error("'execution_id' must be an execution ID such as E0012")
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    return execution_id_text(number)


def _read_every_page(client: "httpx.Client", status: str | None, name: str | None) -> list[dict[str, Any]]:
    """Every execution the filters match, every page read before anything is deleted: paging by offset while
    deleting would skip rows."""
    params: dict[str, Any] = {key: value for key, value in (("status", status), ("name", name)) if value is not None}
    rows: list[dict[str, Any]] = []
    while True:
        body = api_request(client, "GET", "/v1/executions", params=params).json()
        rows.extend(body["executions"])
        if body.get("cursor") is None:
            return rows
        params["cursor"] = body["cursor"]


def _empty_report() -> dict[str, Any]:
    return {"deleted": [], "refused": [], "missing": [], "resumes_ended": [], "manifests_kept": []}


def _delete_in_batches(client: "httpx.Client", ids: Sequence[str], *, dry_run: bool, merged: dict[str, Any] | None = None) -> dict[str, Any]:
    """One request per ``DELETE_BATCH`` IDs, their answers merged into ``merged`` as each comes, so a request that
    fails leaves the ones before it counted."""
    merged = merged if merged is not None else _empty_report()
    for start in range(0, len(ids), DELETE_BATCH):
        body = api_request(client, "POST", "/v1/executions/delete", json={"executions": list(ids[start : start + DELETE_BATCH]), "dry_run": dry_run}).json()
        for key in merged:
            merged[key].extend(body[key])
    return merged


def _resumes_ended(body: dict[str, Any]) -> dict[str, list[str]]:
    return {row["execution_id"]: row["queue_ids"] for row in body["resumes_ended"]}


def _unable_lines(body: dict[str, Any]) -> list[str]:
    return [f"Not deleted: {row['reason']}" for row in body["refused"]] + [f"No execution {execution_id}" for execution_id in body["missing"]]


def _listing_line(execution_id: str, summary: dict[str, Any] | None, ended: list[str]) -> str:
    parts = [execution_id]
    if summary is not None:
        total = summary.get("total_runs")
        succeeded = (summary.get("first_run") or 1) - 1 + summary.get("succeeded", 0)
        parts += [summary["job_name"], summary["status"], f"{succeeded}/{total if total is not None else '?'} runs", summary["started_at"]]
    line = "  ".join(str(part) for part in parts)
    return line + "".join(f"; {queue_id} can no longer be resumed" for queue_id in ended)


def _say_deleted(report: dict[str, Any]) -> None:
    """How many were deleted, the resumes that ended, and the manifests that stayed."""
    count = len(report["deleted"])
    typer.echo(f"Deleted {count} execution{'s' if count != 1 else ''}")
    ended = sorted({queue_id for queue_ids in _resumes_ended(report).values() for queue_id in queue_ids})
    for queue_id in ended:
        typer.echo(f"{queue_id} can no longer be resumed")
    for row in report["manifests_kept"]:
        typer.echo(f"{row['execution_id']}'s manifest could not be deleted: {row['path']}; `dtc import-history` would bring it back under a new number")


def _report(report: dict[str, Any], unable: list[str]) -> None:
    """What was deleted, the resumes that ended, the manifests that stayed, and the executions the server refused or
    no longer had since the dry run; exits 2 when any execution asked for was not deleted."""
    _say_deleted(report)
    late = _unable_lines(report)
    for line in late:
        typer.echo(line)
    if unable or late:
        raise typer.Exit(code=EXIT_INVALID_INPUT)
