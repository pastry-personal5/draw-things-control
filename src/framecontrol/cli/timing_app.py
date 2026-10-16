"""``fc timing``: what each stage of a run took, read through fc serve's API, and the timing rebuilt from job logs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

import typer
from loguru import logger

from framecontrol.cli.api_client import AllowRemoteServerOption, ServerUrlOption, TokenFileOption, api_client, api_request
from framecontrol.cli.context import errors_exit, services_of
from framecontrol.core.client_config import DEFAULT_SERVER_URL
from framecontrol.core.exit_codes import EXIT_INVALID_INPUT
from framecontrol.services.timing_replay import ESTIMATOR_NAMES, replay_database
from framecontrol.state.ids import EXECUTION_LETTER, execution_id_text, parse_typed_id

if TYPE_CHECKING:
    import httpx

timing_app = typer.Typer(help="Show what each stage of a run took, and rebuild the timing of runs whose job logs still exist.")

COLUMNS = ("stage", "phase", "source", "start", "duration", "share", "units")
ESTIMATE_NOTE = "* the first reading of the stage is estimated from the log"


@timing_app.command("show")
def timing_show(
    ctx: typer.Context,
    execution: Annotated[str, typer.Argument(help="An execution's ID (E0047).", show_default=False)],
    run: Annotated[int | None, typer.Option("--run", min=1, help="Only this run's number in the chain; default: every run of the execution.")] = None,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
) -> None:
    """Each stage of a run: where it started, how long it took, its share of the run, and the steps or frames it counted.
    The table is how to check a measurement against the job log."""
    number = parse_typed_id(execution, EXECUTION_LETTER)
    if number is None:
        logger.error("'execution_id' must be an execution ID such as {}", execution_id_text(47))
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    execution_id = execution_id_text(number)
    with errors_exit():
        client = api_client(ctx, server_url, token_file, allow_remote_server)
    with client:
        runs = [run] if run is not None else _run_numbers(client, execution_id)
        for index, number in enumerate(runs):
            body = api_request(client, "GET", f"/v1/executions/{execution_id}/runs/{number}/stages").json()
            if index:
                typer.echo("")
            for line in timing_lines(execution_id, number, body):
                typer.echo(line)


@timing_app.command("replay")
def timing_replay(
    ctx: typer.Context,
    backend: Annotated[str | None, typer.Option("--backend", help="Only the runs of this backend (h3 or draw-things-cli); default: both.")] = None,
    estimator: Annotated[str, typer.Option("--estimator", help=f"What to replay the runs through: {', '.join(ESTIMATOR_NAMES)}.")] = "forecast",
) -> None:
    """Replay the retained runs through an estimator as if they were running, leaving out the execution each is scored
    on, and print how close its remaining time was at 5 to 90% of each run and 10 to 90% of each job, how often its
    range held the truth, and how soon it gave a number. It only reads the state database, with no server needed."""
    if estimator not in ESTIMATOR_NAMES:
        logger.error("--estimator must be one of {}", ", ".join(ESTIMATOR_NAMES))
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    with errors_exit():
        lines = replay_database(services_of(ctx).paths.database, estimator, backend=backend)
    for line in lines:
        typer.echo(line)


def _run_numbers(client: "httpx.Client", execution_id: str) -> list[int]:
    body = api_request(client, "GET", f"/v1/executions/{execution_id}", params={"brief": "1"}).json()
    return [run["number"] for run in body["runs"]]


def timing_lines(execution_id: str, run: int, body: dict[str, Any]) -> list[str]:
    """One run's stages as a table, under a line that says what the run was and whether its timing is whole."""
    if not body["recorded"]:
        return [f"{execution_id} run {run}: no timing recorded"]
    pipeline = f" {body['pipeline']}" if body["pipeline"] else ""
    # A run whose spans were stored but whose timing row was not (the recorder skips what it cannot store) has no backend or fidelity.
    backend, fidelity = body["backend"] or "backend unknown", body["fidelity"] or "fidelity unknown"
    headline = f"{execution_id} run {run}: {backend}{pipeline}, {fidelity}, {'complete' if body['complete'] else 'incomplete'}, {body['seconds']:.1f} s"
    rows = [COLUMNS, *(_row(stage) for stage in body["stages"])]
    widths = [max(len(row[index]) for row in rows) for index in range(len(COLUMNS))]
    # Text columns are left-aligned and number columns right-aligned; the last column is not padded.
    numeric = {3, 4, 5}
    table = ["  ".join((cell.rjust(width) if index in numeric else cell.ljust(width)) for index, (cell, width) in enumerate(zip(row, widths, strict=True))).rstrip() for row in rows]
    lines = [headline, *table]
    if any(stage["uncertain"] for stage in body["stages"]):
        lines.append(ESTIMATE_NOTE)
    return lines


def _row(stage: dict[str, Any]) -> tuple[str, ...]:
    name = stage["stage"] + ("*" if stage["uncertain"] else "")
    share = f"{stage['share'] * 100:.1f}%" if stage["share"] is not None else ""
    units = ""
    if stage["total"] is not None:
        units = f"{stage['done']}/{stage['total']}" + (f" {stage['unit']}" if stage["unit"] else "")
    return (name, stage["phase"] or "", stage["source"], f"{stage['start']:.1f}s", f"{stage['duration']:.1f}s", share, units)
