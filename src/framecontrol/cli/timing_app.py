"""``fc timing``: what each stage of a run took, read through fc serve's API, and the timing rebuilt from job logs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from loguru import logger

from framecontrol.cli.api_client import AllowRemoteServerOption, ServerUrlOption, TokenFileOption, api_client, api_request
from framecontrol.cli.context import errors_exit, services_of
from framecontrol.core.client_config import DEFAULT_SERVER_URL
from framecontrol.core.exit_codes import EXIT_INVALID_INPUT
from framecontrol.services import timing_knobs
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
    backend: Annotated[str | None, typer.Option("--backend", help="Only the runs of this backend (draw-things-cli or h3-metal-cli); default: both.")] = None,
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


knobs_app = typer.Typer(help="The forecast's timing knobs: the tunables and the per-backend calibration under data/timing-data-analysis/.")
timing_app.add_typer(knobs_app, name="knobs")


@knobs_app.command("show")
def knobs_show(ctx: typer.Context, file: Annotated[str | None, typer.Argument(help="Only the values of this file (tuning.yaml, draw-things-cli.yaml, h3-metal-cli.yaml).", show_default=False)] = None, source: Annotated[str | None, typer.Option("--source", help="Only values from this source: factory, provisional, learned, pinned, backup or file.")] = None) -> None:
    """Every effective value with where it came from: the factory, a provisional or validated calibration, a value you pinned by editing the file, or a compatible backup standing in for a file that cannot be read."""
    rows = [row for row in timing_knobs.show(timing_knobs.knob_files(services_of(ctx).paths)) if (file is None or row.file == file) and (source is None or row.source == source)]
    for row in rows:
        approval = f"; approved against {row.approved_against}" if row.approved_against is not None else ""
        typer.echo(f"{row.file}  {row.path} = {row.value}  [{row.source}{approval}]")
    if not rows:
        typer.echo("No values match")


@knobs_app.command("check")
def knobs_check(ctx: typer.Context) -> None:
    """Read the knob files as the forecast does and report unsupported schemas, invalid keys, hand-edit pins, and updates that are deferred. Exits 1 when a file cannot be used."""
    report = timing_knobs.check(timing_knobs.knob_files(services_of(ctx).paths))
    for issue in report.issues:
        typer.echo(f"{issue.level}: {issue}")
    for name, pins in report.pins.items():
        for path, value in pins.items():
            typer.echo(f"pinned: {name} {path} = {value}")
    for line in report.deferred:
        typer.echo(f"deferred: {line}")
    for group, result in report.gates.items():
        typer.echo(f"gate: {group}: {result}")
    if report.search is not None:
        typer.echo(f"search: {report.search}")
    typer.echo(f"Files present: {', '.join(report.files) or 'none (the factory knobs apply)'}")
    if report.suspended:
        raise typer.Exit(code=1)


@knobs_app.command("update")
def knobs_update(ctx: typer.Context) -> None:
    """Run one calibration update now from the retained runs, including an eligible post-checkpoint held-out gate. It needs no server and waits for the same lock."""
    with errors_exit():
        report = timing_knobs.update_now(services_of(ctx).paths)
    _print_update_report(report)


def _print_update_report(report: timing_knobs.UpdateReport) -> None:
    typer.echo(f"Added {report.runs_added} run{'s' if report.runs_added != 1 else ''} to the ring")
    for group, checkpoint in sorted(report.fitted.items()):
        typer.echo(f"  {group}: provisional calibration at {checkpoint} run{'s' if checkpoint != 1 else ''}")
    for group, verdict in sorted(report.gates.items()):
        typer.echo(f"  {group}: held-out gate {verdict}")
    if report.search is not None:
        typer.echo(f"  tunables search: {report.search}")
    for name in report.written:
        typer.echo(f"  wrote {name}")
    for name in report.suspended:
        typer.echo(f"  {name}: automatic updates are suspended; repair it, roll it back, or reset it")
    for reason in report.rejected:
        typer.echo(f"  rejected: {reason}")


@knobs_app.command("tune")
def knobs_tune(ctx: typer.Context) -> None:
    """Run one guarded tunables search now, using the same evidence and lock as the background updater."""
    with errors_exit():
        report = timing_knobs.tune_now(services_of(ctx).paths)
    _print_update_report(report)
    if report.search == "insufficient_evidence":
        typer.echo("No group currently has enough held-out evidence for a tunables search")


@knobs_app.command("rollback")
def knobs_rollback(ctx: typer.Context, count: Annotated[int, typer.Argument(min=1, help="How many updates back: 1 undoes the latest.")] = 1) -> None:
    """Put the knob files back as they were before the latest update (or the one N back), as a set. The restored values are recorded as the machine's, so they are not read as your edits."""
    with errors_exit():
        restored = timing_knobs.rollback(timing_knobs.knob_files(services_of(ctx).paths), count)
    typer.echo(f"Restored {', '.join(restored) or 'nothing'}")


@knobs_app.command("reset")
def knobs_reset(ctx: typer.Context, backend: Annotated[str | None, typer.Argument(help="One backend (draw-things-cli or h3-metal-cli); default: both.", show_default=False)] = None) -> None:
    """Move a backend's calibration file to the backups so the factory knobs apply, and start its learning over from the ring."""
    with errors_exit():
        moved = timing_knobs.reset(timing_knobs.knob_files(services_of(ctx).paths), backend)
    typer.echo(f"Moved {', '.join(moved)} to the backups" if moved else "Nothing to reset: there was no calibration file")


@knobs_app.command("export-defaults")
def knobs_export_defaults(ctx: typer.Context, directory: Annotated[Path | None, typer.Option("--directory", help="Where to write the example files; default: data/timing-data-analysis in the project.")] = None) -> None:
    """Write the factory knobs as tuning.example.yaml, draw-things-cli.example.yaml and h3-metal-cli.example.yaml."""
    target = directory or services_of(ctx).paths.timing_data
    with errors_exit():
        for path in timing_knobs.export_defaults(target):
            typer.echo(f"Wrote {path}")


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
