"""Typer interface for Draw Things control: the commands, their options, and how an error ends one."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from draw_things_control.cli.api_client import AllowRemoteServerOption, ServerUrlOption, TokenFileOption
from draw_things_control.cli.context import CliServices, errors_exit, services_of
from draw_things_control.cli.history_app import history_app
from draw_things_control.cli.queue_app import queue_app
from draw_things_control.core.client_config import DEFAULT_SERVER_URL, check_server_host
from draw_things_control.core.draw_things_config import load_config
from draw_things_control.core.errors import InputError
from draw_things_control.core.exit_codes import EXIT_INVALID_INPUT, EXIT_STATE_UNAVAILABLE
from draw_things_control.core.generation import GenerateRequest
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.network import is_loopback_host
from draw_things_control.core.paths import DEFAULT_PATHS, ProjectPaths
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.files import read_job, read_settings
from draw_things_control.jobs.text import job_summary, report_ignored_config
from draw_things_control.services.toolkit import Toolkit
from draw_things_control.state.history_import import import_history
from draw_things_control.state.store import StateError, Store, StoreMode

app = typer.Typer(help="Control Draw Things from the command line.", no_args_is_help=True)
app.add_typer(queue_app, name="queue")
app.add_typer(history_app, name="history")


@contextmanager
def open_state(paths: ProjectPaths, settings: GlobalConfig) -> Iterator[Store]:
    """Open the state store for a command and close it afterwards; a database failure exits with code 1."""
    with errors_exit():
        try:
            store = Store.open(paths.database, mode=StoreMode.RUN, retention_days=settings.history_retention_days)
        except sqlite3.Error as error:
            raise StateError(f"Cannot use the state database {paths.database}: {error}") from error
    try:
        yield store
    except sqlite3.Error as error:
        logger.error("Cannot use the state database {}: {}", store.path, error)
        raise typer.Exit(code=EXIT_STATE_UNAVAILABLE) from error
    finally:
        store.close()


def read_settings_or_exit(global_config: Path | None, paths: ProjectPaths) -> GlobalConfig:
    """Load the global configuration (the project's own without a path), exiting with code 2 if it is invalid."""
    with errors_exit():
        return read_settings(global_config or paths.global_config, paths)


JobFileArgument = Annotated[Path, typer.Argument(help="Job definition file, for example data/example-job.yaml.")]
GlobalConfigOption = Annotated[Path | None, typer.Option("--global-config", help="Global configuration file; default: config/global-config.yaml in the project.")]
ExecutableOption = Annotated[str, typer.Option(help="Draw Things CLI executable.")]


def configure_logging() -> None:
    """Route child stdout to stdout and other Loguru messages to stderr."""
    logger.remove()
    logger.add(sys.stdout, format="{message}", level="INFO", filter=lambda record: record["extra"].get("child_stream") == "stdout", colorize=False)
    logger.add(sys.stderr, format="{message}", level="INFO", filter=lambda record: record["extra"].get("child_stream") != "stdout", colorize=False)


@app.command()
def generate(
    ctx: typer.Context,
    models_dir: Annotated[Path | None, typer.Option(help="Models directory.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model reference; may also come from a configuration.")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="Prompt text.")] = None,
    prompt_file: Annotated[str | None, typer.Option(help="Prompt file, or - for stdin.")] = None,
    negative_prompt: Annotated[str | None, typer.Option()] = None,
    negative_prompt_file: Annotated[str | None, typer.Option()] = None,
    steps: Annotated[int | None, typer.Option()] = None,
    cfg: Annotated[float | None, typer.Option()] = None,
    width: Annotated[int | None, typer.Option()] = None,
    height: Annotated[int | None, typer.Option()] = None,
    frames: Annotated[int | None, typer.Option()] = None,
    strength: Annotated[float | None, typer.Option()] = None,
    seed: Annotated[int | None, typer.Option("--seed", "-s")] = None,
    config_json: Annotated[str | None, typer.Option(help="Inline JSON configuration override.")] = None,
    config_file: Annotated[Path | None, typer.Option("--config-file", "--config", help="YAML or JSON configuration file; a YAML file is passed inline with --config-json.")] = None,
    image: Annotated[list[Path] | None, typer.Option("--image", help="Repeat for ordered reference images.")] = None,
    audio: Annotated[Path | None, typer.Option()] = None,
    audio_encoder_file: Annotated[str | None, typer.Option()] = None,
    avc: Annotated[bool, typer.Option("--avc")] = False,
    segment_frames: Annotated[int | None, typer.Option()] = None,
    cond_frames: Annotated[int | None, typer.Option()] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    video_format: Annotated[str | None, typer.Option()] = None,
    terminal_image: Annotated[bool, typer.Option("--terminal-image")] = False,
    terminal_image_protocol: Annotated[str | None, typer.Option()] = None,
    download_missing: Annotated[bool | None, typer.Option("--download-missing/--no-download-missing")] = None,
    disable_preview: Annotated[bool, typer.Option("--disable-preview")] = False,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    remote: Annotated[bool, typer.Option("--remote")] = False,
    remote_url: Annotated[str | None, typer.Option()] = None,
    remote_port: Annotated[int | None, typer.Option()] = None,
    remote_tls: Annotated[bool | None, typer.Option("--remote-tls/--no-remote-tls")] = None,
    remote_shared_secret: Annotated[str | None, typer.Option()] = None,
    cloud_compute: Annotated[bool, typer.Option("--cloud-compute")] = False,
    api_key: Annotated[str | None, typer.Option()] = None,
    cloud_api_base_url: Annotated[str | None, typer.Option()] = None,
    executable: Annotated[str, typer.Option(help="Draw Things CLI executable.")] = "draw-things-cli",
    shutdown_grace: Annotated[float, typer.Option(help="Seconds before forcing shutdown.")] = 10.0,
    timeout: Annotated[float | None, typer.Option(help="Maximum generation runtime in seconds.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the command without running it.")] = False,
) -> None:
    """Generate an image or video with Draw Things."""
    services = services_of(ctx)
    # Typer has converted these callback values; its context still holds raw strings.
    options = locals().copy()
    for wrapper_option in ("ctx", "services", "dry_run", "timeout", "shutdown_grace"):
        options.pop(wrapper_option)
    service = services.toolkit.generation_service()
    with errors_exit():
        arguments = service.prepare(GenerateRequest(**{**options, "image": tuple(options["image"] or ())}))
        if dry_run:
            outcome = service.execute(arguments, dry_run=True, timeout=timeout, shutdown_grace=shutdown_grace)
        else:
            with RunLock("generate", directory=services.paths.state) as lock:
                outcome = service.execute(arguments, dry_run=False, timeout=timeout, shutdown_grace=shutdown_grace, on_start=lock.record_child)
    if outcome.command_preview is not None:
        typer.echo(outcome.command_preview)
    if outcome.timed_out:
        logger.error("Generation timed out after {} seconds", timeout)
    elif outcome.termination_signal is not None:
        logger.warning("Generation stopped after {}; exit code: {}", outcome.termination_signal.name, outcome.exit_code)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


@app.command("validate-config")
def validate_config(config: Annotated[Path, typer.Argument(help="YAML configuration file to validate.")]) -> None:
    """Validate a Draw Things YAML configuration file."""
    with errors_exit():
        settings = load_config(config.expanduser())
    typer.echo(f"Valid configuration: {config} (model: {settings.get('model', '(not set)')})")


def read_job_or_exit(job_file: Path, global_config: Path | None, paths: ProjectPaths, *, decode_input: bool = True) -> tuple[JobDefinition, GlobalConfig]:
    """Load the global configuration and the job, exiting with code 2 if either is invalid."""
    with errors_exit():
        return read_job(job_file, global_config or paths.global_config, paths, decode_input=decode_input)


@app.command("validate-job")
def validate_job(ctx: typer.Context, job_file: JobFileArgument, global_config: GlobalConfigOption = None) -> None:
    """Validate a job file without running anything."""
    job, _settings = read_job_or_exit(job_file, global_config, services_of(ctx).paths)
    report_ignored_config(job)
    typer.echo(f"Valid job: {job.path}")
    for label, value in job_summary(job):
        typer.echo(f"  {label}: {value}")


@app.command("import-history")
def import_history_command(
    ctx: typer.Context,
    directory: Annotated[Path | None, typer.Option(help="Directory to search for job manifests; default: the configured output directory.")] = None,
    global_config: GlobalConfigOption = None,
) -> None:
    """Import phase 1 job manifests into the execution history; safe to repeat."""
    paths = services_of(ctx).paths
    settings = read_settings_or_exit(global_config, paths)
    search = (directory or settings.output_directory).expanduser()
    if not search.is_dir():
        logger.error("Not a directory: {}", search)
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    with open_state(paths, settings) as store:
        report = import_history(store, search)
    for given, recorded, manifest in report.given:
        typer.echo(f"  {given}: {manifest}{f' (its manifest says {recorded})' if recorded is not None else ''}")
    typer.echo(f"Imported {report.imported}, skipped {report.skipped} already imported, {report.expired} older than the retention period, {report.unreadable} unreadable, from {search}")


@app.command("tui")
def tui_command(
    ctx: typer.Context,
    data_dir: Annotated[Path | None, typer.Option("--data-dir", help="Directory of job files; default: data/jobs in the project.")] = None,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
    global_config: GlobalConfigOption = None,
) -> None:
    """Browse the jobs in the data directory, and dtc serve's queue and history, in a terminal UI. dtc serve is the
    only thing that ever runs a job now; this reads and submits to it, over the same three flags dtc queue takes."""
    services = services_of(ctx)
    settings = read_settings_or_exit(global_config, services.paths)
    with errors_exit():
        check_server_host(server_url, allow_remote_server=allow_remote_server)
    # Imported here, so the other commands do not load Textual.
    from draw_things_control.tui.app import DrawThingsApp

    tui = DrawThingsApp(settings=settings, paths=services.paths, data_directory=(data_dir or services.paths.jobs).expanduser(), server_url=server_url, token_file=token_file or services.paths.server_token, allow_remote_server=allow_remote_server, toolkit=services.toolkit)
    # The app owns the terminal, so the stdout and stderr sinks main() installed must not write into it until it exits.
    logger.remove()
    try:
        tui.run()
    finally:
        configure_logging()
    # Textual sets a nonzero return code when the app ends on an error, after printing the traceback.
    if tui.return_code:
        raise typer.Exit(code=tui.return_code)


@app.command("serve")
def serve_command(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Address for the HTTP API and the gRPC service; refused unless loopback or --allow-remote-bind is given.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP API port.")] = 8765,
    grpc_port: Annotated[int, typer.Option("--grpc-port", help="gRPC monitoring service port.")] = 8766,
    executable: ExecutableOption = "draw-things-cli",
    shutdown_grace: Annotated[float, typer.Option(help="Seconds before forcing shutdown of a run.")] = 10.0,
    global_config: GlobalConfigOption = None,
    allow_write: Annotated[bool, typer.Option("--allow-write", help="Allow authenticated API clients to create, replace, and trash job files, and agents to delete executions through MCP.")] = False,
    allow_remote_bind: Annotated[bool, typer.Option("--allow-remote-bind", help="Allow --host beyond loopback; the token then crosses the network in plain HTTP (an SSH tunnel is the safer way in from elsewhere).")] = False,
) -> None:
    """Run the HTTP API and the gRPC monitoring service: the only thing that ever starts draw-things-cli."""
    if shutdown_grace < 0:
        logger.error("--shutdown-grace must not be negative")
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    services = services_of(ctx)
    settings = read_settings_or_exit(global_config, services.paths)
    # Imported here, so the other commands do not load FastAPI, uvicorn, or grpc.
    from draw_things_control.server.serve import ServeOptions
    from draw_things_control.server.serve import run as run_server

    options = ServeOptions(host=host, port=port, grpc_port=grpc_port, executable=executable, shutdown_grace=shutdown_grace, allow_remote_bind=allow_remote_bind, allow_write=allow_write)
    with errors_exit():
        run_server(services.paths, settings, services.toolkit, options)


MCP_TRANSPORTS = ("stdio", "streamable-http")
DEFAULT_MCP_PORT = 8767


@app.command("mcp")
def mcp_command(
    ctx: typer.Context,
    server_url: ServerUrlOption = DEFAULT_SERVER_URL,
    token_file: TokenFileOption = None,
    allow_remote_server: AllowRemoteServerOption = False,
    transport: Annotated[str, typer.Option(help="stdio, for a client that starts this command, or streamable-http, which listens at http://HOST:PORT/mcp for a client that cannot, such as an agent in a virtual machine.")] = "stdio",
    host: Annotated[str | None, typer.Option(help="Address to listen on, with --transport streamable-http; default 127.0.0.1, and refused unless loopback or --allow-remote-bind is given.")] = None,
    port: Annotated[int | None, typer.Option(help=f"Port to listen on, with --transport streamable-http; default {DEFAULT_MCP_PORT}.")] = None,
    allow_remote_bind: Annotated[bool, typer.Option("--allow-remote-bind", help="Allow --host beyond loopback; the token then crosses the network in plain HTTP (an SSH tunnel is the safer way in from elsewhere).")] = False,
) -> None:
    """Run an MCP server that gives AI agents typed tools over dtc serve's API: on stdio, or over Streamable HTTP, where
    every request needs the server token as a bearer token. It starts whether or not dtc serve is up, reads the token at
    the first call, and acts only through the API."""
    services = services_of(ctx)
    with errors_exit():
        if transport not in MCP_TRANSPORTS:
            raise InputError(f"--transport {transport} is not one of {', '.join(MCP_TRANSPORTS)}", field="transport")
        if transport == "stdio" and (host is not None or port is not None or allow_remote_bind):
            raise InputError("--host, --port, and --allow-remote-bind apply only with --transport streamable-http", field="transport")
        listen_host = host or "127.0.0.1"
        if transport == "streamable-http" and not allow_remote_bind and not is_loopback_host(listen_host):
            raise InputError(f"--host {listen_host} is not loopback; pass --allow-remote-bind to bind beyond it (the token then crosses the network in plain HTTP)", field="host")
        check_server_host(server_url, allow_remote_server=allow_remote_server)
    # Imported here, so the other commands do not load the MCP SDK.
    from draw_things_control.mcp_server.app import BindError
    from draw_things_control.mcp_server.app import run as run_mcp
    from draw_things_control.mcp_server.app import run_http as run_mcp_http

    # Stdout carries the protocol over stdio and nothing else: main()'s sinks send child output there, so only stderr remains.
    logger.remove()
    logger.add(sys.stderr, format="{message}", level="INFO", colorize=False)
    token_path = token_file or services.paths.server_token
    if transport == "stdio":
        run_mcp(server_url, token_path)
        return
    listen_port = DEFAULT_MCP_PORT if port is None else port
    if allow_remote_bind:
        logger.warning("--allow-remote-bind: the bearer token crosses the network in plain HTTP; prefer an SSH tunnel")
    with errors_exit():
        try:
            run_mcp_http(server_url, token_path, listen_host, listen_port)
        except BindError as error:
            raise InputError(str(error), field="port") from error


def main(argv: Sequence[str] | None = None, *, services: CliServices | None = None) -> int:
    """Run the Typer app and preserve its command exit status."""
    configure_logging()
    try:
        app(args=list(argv) if argv is not None else None, prog_name="dtc", obj=services or CliServices(DEFAULT_PATHS, Toolkit()))
    except SystemExit as error:
        return int(error.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
