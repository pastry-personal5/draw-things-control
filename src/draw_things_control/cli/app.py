"""Typer interface for Draw Things control: the commands, their options, and how an error ends one."""

from __future__ import annotations

import signal
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from draw_things_control.core.draw_things_config import load_config
from draw_things_control.core.errors import DtcError
from draw_things_control.core.exit_codes import EXIT_INVALID_INPUT, EXIT_STATE_UNAVAILABLE, exit_code_for_error
from draw_things_control.core.generation import GenerateRequest
from draw_things_control.core.global_config import GlobalConfig
from draw_things_control.core.paths import DEFAULT_PATHS, ProjectPaths
from draw_things_control.core.run_lock import RunLock
from draw_things_control.jobs.definition import JobDefinition
from draw_things_control.jobs.files import read_job as load_job_and_settings
from draw_things_control.jobs.files import read_settings
from draw_things_control.jobs.text import job_summary, plan_lines, report_ignored_config
from draw_things_control.services.job_runs import JobRunSession
from draw_things_control.services.toolkit import Toolkit
from draw_things_control.state.history_import import import_history
from draw_things_control.state.store import StateError, Store, StoreMode

app = typer.Typer(help="Control Draw Things from the command line.", no_args_is_help=True)


@dataclass(frozen=True)
class CliServices:
    """What every command needs from the machine: where the project's files are, and the real tools. ``main`` builds it once; a
    test passes its own with ``CliRunner.invoke(..., obj=...)``."""

    paths: ProjectPaths
    toolkit: Toolkit


def services_of(ctx: typer.Context) -> CliServices:
    """The context's services, or the project's own when a command runs without ``main``."""
    return ctx.obj if isinstance(ctx.obj, CliServices) else CliServices(DEFAULT_PATHS, Toolkit())


@contextmanager
def errors_exit() -> Iterator[None]:
    """Log an error that stops a command (invalid input, a busy run lock, an unusable state store) and exit with its code."""
    try:
        yield
    except DtcError as error:
        logger.error("{}", error)
        raise typer.Exit(code=exit_code_for_error(error)) from error
    except ValueError as error:
        logger.error("{}", error)
        raise typer.Exit(code=EXIT_INVALID_INPUT) from error


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


def load_settings(global_config: Path | None, paths: ProjectPaths) -> GlobalConfig:
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


def read_job(job_file: Path, global_config: Path | None, paths: ProjectPaths, *, decode_input: bool = True) -> tuple[JobDefinition, GlobalConfig]:
    """Load the global configuration and the job, exiting with code 2 if either is invalid."""
    with errors_exit():
        return load_job_and_settings(job_file, global_config or paths.global_config, paths, decode_input=decode_input)


@app.command("validate-job")
def validate_job(ctx: typer.Context, job_file: JobFileArgument, global_config: GlobalConfigOption = None) -> None:
    """Validate a job file without running anything."""
    job, _settings = read_job(job_file, global_config, services_of(ctx).paths)
    report_ignored_config(job)
    typer.echo(f"Valid job: {job.path}")
    for label, value in job_summary(job):
        typer.echo(f"  {label}: {value}")


@app.command("run-job")
def run_job(
    ctx: typer.Context,
    job_file: JobFileArgument,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and print every command without running anything.")] = False,
    executable: ExecutableOption = "draw-things-cli",
    shutdown_grace: Annotated[float, typer.Option(help="Seconds before forcing shutdown of a run.")] = 10.0,
    global_config: GlobalConfigOption = None,
) -> None:
    """Run every generation in a job, chaining each output into the next run."""
    services = services_of(ctx)
    # A real run decodes the input when it writes run 1's copy, so it skips the validation decode.
    job, settings = read_job(job_file, global_config, services.paths, decode_input=dry_run)
    executor = services.toolkit.job_executor()
    with errors_exit():
        if dry_run:
            report_ignored_config(job)
            preview = executor.preview(job, executable=executable)
            for line in plan_lines(job, preview):
                typer.echo(line)
            return
        try:
            outcome = JobRunSession(services.paths, executor, settings).run(job, holder="run-job", executable=executable, shutdown_grace=shutdown_grace)
        except StateError as error:
            logger.error("{}; the job was not started", error)
            raise typer.Exit(code=EXIT_STATE_UNAVAILABLE) from error
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


@app.command("import-history")
def import_history_command(
    ctx: typer.Context,
    directory: Annotated[Path | None, typer.Option(help="Directory to search for job manifests; default: the configured output directory.")] = None,
    global_config: GlobalConfigOption = None,
) -> None:
    """Import phase 1 job manifests into the execution history; safe to repeat."""
    paths = services_of(ctx).paths
    settings = load_settings(global_config, paths)
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
    executable: ExecutableOption = "draw-things-cli",
    shutdown_grace: Annotated[float, typer.Option(help="Seconds before forcing shutdown of a run.")] = 10.0,
    global_config: GlobalConfigOption = None,
) -> None:
    """Browse, run, and watch the jobs in the data directory in a terminal UI."""
    if shutdown_grace < 0:
        logger.error("--shutdown-grace must not be negative")
        raise typer.Exit(code=EXIT_INVALID_INPUT)
    services = services_of(ctx)
    settings = load_settings(global_config, services.paths)
    # Imported here, so the other commands do not load Textual.
    from draw_things_control.tui.app import DrawThingsApp

    # Jobs run on a worker thread, where signal handlers cannot be installed; the app handles signals itself.
    tui_executor = services.toolkit.job_executor(handle_signals=False)
    tui = DrawThingsApp(settings=settings, paths=services.paths, data_directory=(data_dir or services.paths.jobs).expanduser(), executable=executable, job_executor=tui_executor, shutdown_grace=shutdown_grace)
    # The app owns the terminal, so the stdout and stderr sinks main() installed must not write into it until it exits.
    logger.remove()
    try:
        tui.run()
    finally:
        # A backstop: the app stops a running job when it unmounts; this does nothing when no job runs.
        tui_executor.cancel(signal.SIGINT)
        configure_logging()
    # Textual sets a nonzero return code when the app ends on an error, after printing the traceback.
    if tui.return_code:
        raise typer.Exit(code=tui.return_code)


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
