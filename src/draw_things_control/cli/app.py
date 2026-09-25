"""Typer interface and application wiring for Draw Things control."""

from __future__ import annotations

import shutil
import signal
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from draw_things_control.core.configuration import load_config
from draw_things_control.core.draw_things_arguments import DrawThingsGenerateArguments
from draw_things_control.core.draw_things_runner import DrawThingsProcessRunner
from draw_things_control.core.generation_service import ChildStartCallback, GenerationService
from draw_things_control.core.global_config import DEFAULT_GLOBAL_CONFIG, PROJECT_ROOT, GlobalConfig
from draw_things_control.core.process_output import MessageCallback, OutputProcessor
from draw_things_control.core.run_lock import EX_TEMPFAIL, RunLock, RunLockBusy, RunLockError
from draw_things_control.jobs.frame_extraction import extract_last_frame, require_ffmpeg
from draw_things_control.jobs.job_definition import JobDefinition
from draw_things_control.jobs.job_report import job_summary, plan_lines, read_settings, report_ignored_config
from draw_things_control.jobs.job_report import read_job as load_job_and_settings
from draw_things_control.jobs.job_service import JobService
from draw_things_control.jobs.video_color import tag_video_colors
from draw_things_control.state.history_import import import_history
from draw_things_control.state.recorder import ExecutionRecorder
from draw_things_control.state.store import StateError, Store

app = typer.Typer(help="Control Draw Things from the command line.", no_args_is_help=True)

DEFAULT_DATA_DIRECTORY = PROJECT_ROOT / "data"


@contextmanager
def invalid_input_exits() -> Iterator[None]:
    """Log a ValueError (invalid input, configuration, or job) and exit with code 2."""
    try:
        yield
    except ValueError as error:
        logger.error("{}", error)
        raise typer.Exit(code=2) from error


@contextmanager
def held_run_lock(command: str) -> Iterator[RunLock]:
    """Hold the machine-wide run lock for ``command``, or exit 75 if a run is in progress, 1 if it cannot be taken."""
    lock = RunLock(command)
    try:
        lock.acquire()
    except RunLockBusy as error:
        logger.error("{}", error)
        raise typer.Exit(code=EX_TEMPFAIL) from error
    except RunLockError as error:
        logger.error("{}", error)
        raise typer.Exit(code=1) from error
    try:
        yield lock
    finally:
        lock.release()


def open_store(settings: GlobalConfig) -> Store:
    """Open the state store, exiting with code 1 and the cause if it cannot be used."""
    try:
        return Store(retention_days=settings.history_retention_days)
    except (StateError, RunLockError, sqlite3.Error) as error:
        logger.error("{}", error)
        raise typer.Exit(code=1) from error


@contextmanager
def open_state(settings: GlobalConfig) -> Iterator[Store]:
    """Open the state store for a command and close it afterwards; a database failure exits with code 1."""
    store = open_store(settings)
    try:
        yield store
    except sqlite3.Error as error:
        logger.error("Cannot use the state database {}: {}", store.path, error)
        raise typer.Exit(code=1) from error
    finally:
        store.close()


def load_settings(global_config: Path) -> GlobalConfig:
    """Load the global configuration, exiting with code 2 if it is invalid."""
    with invalid_input_exits():
        return read_settings(global_config)


def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None, *, handle_signals: bool = True) -> DrawThingsProcessRunner:
    """Connect the generation use case to its process adapter; ``on_message`` receives each line the child prints, ``on_start`` its PID and executable name."""
    # Without an output file, draw-things-cli previews in the terminal, so it must inherit it.
    capture_output = arguments.output is not None and not arguments.terminal_image
    name = Path(arguments.executable).name
    on_pid = (lambda pid: on_start(pid, name)) if on_start is not None else None
    return DrawThingsProcessRunner(arguments, output_processor=OutputProcessor(callback=on_message), timeout_seconds=timeout, shutdown_grace_seconds=shutdown_grace, capture_output=capture_output, handle_signals=handle_signals, on_start=on_pid)


def create_job_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float, on_message: MessageCallback | None = None, on_start: ChildStartCallback | None = None) -> DrawThingsProcessRunner:
    """Create a run's runner; JobService owns signal handling and forwards signals to it."""
    return create_runner(arguments, timeout, shutdown_grace, on_message, on_start, handle_signals=False)


def create_job_service(*, handle_signals: bool = True) -> JobService:
    """The JobService every front end uses to run jobs with the real tools; ``handle_signals`` must be False for jobs run off the main thread."""
    return JobService(runner_factory=create_job_runner, find_executable=shutil.which, frame_extractor=extract_last_frame, require_ffmpeg=require_ffmpeg, video_tagger=tag_video_colors, handle_signals=handle_signals)


service = GenerationService(runner_factory=create_runner, find_executable=shutil.which, config_loader=load_config)
job_service = create_job_service()

JobFileArgument = Annotated[Path, typer.Argument(help="Job definition file, for example data/example-job.yaml.")]
GlobalConfigOption = Annotated[Path, typer.Option("--global-config", help="Global configuration file.")]
ExecutableOption = Annotated[str, typer.Option(help="Draw Things CLI executable.")]


def configure_logging() -> None:
    """Route child stdout to stdout and other Loguru messages to stderr."""
    logger.remove()
    logger.add(sys.stdout, format="{message}", level="INFO", filter=lambda record: record["extra"].get("child_stream") == "stdout", colorize=False)
    logger.add(sys.stderr, format="{message}", level="INFO", filter=lambda record: record["extra"].get("child_stream") != "stdout", colorize=False)


@app.command()
def generate(
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
    config_file: Annotated[Path | None, typer.Option("--config-file", "--config", help="JSON configuration override file.")] = None,
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
    # Typer has converted these callback values; its context still holds raw strings.
    options = locals().copy()
    for wrapper_option in ("dry_run", "timeout", "shutdown_grace"):
        options.pop(wrapper_option)
    with invalid_input_exits():
        arguments = service.prepare(options)
        if dry_run:
            outcome = service.execute(arguments, dry_run=True, timeout=timeout, shutdown_grace=shutdown_grace)
        else:
            with held_run_lock("generate") as lock:
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
def validate_config(config: Annotated[Path, typer.Argument(help="JSON configuration file to validate.")]) -> None:
    """Validate a Draw Things JSON override file."""
    with invalid_input_exits():
        settings = load_config(config.expanduser())
    typer.echo(f"Valid configuration: {config} (model: {settings.get('model', '(not set)')})")


def read_job(job_file: Path, global_config: Path, *, decode_input: bool = True) -> tuple[JobDefinition, GlobalConfig]:
    """Load the global configuration and the job, exiting with code 2 if either is invalid."""
    with invalid_input_exits():
        return load_job_and_settings(job_file, global_config, decode_input=decode_input)


@app.command("validate-job")
def validate_job(job_file: JobFileArgument, global_config: GlobalConfigOption = DEFAULT_GLOBAL_CONFIG) -> None:
    """Validate a job file without running anything."""
    job, _settings = read_job(job_file, global_config)
    report_ignored_config(job)
    typer.echo(f"Valid job: {job.path}")
    for label, value in job_summary(job):
        typer.echo(f"  {label}: {value}")


@app.command("run-job")
def run_job(
    job_file: JobFileArgument,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and print every command without running anything.")] = False,
    executable: ExecutableOption = "draw-things-cli",
    shutdown_grace: Annotated[float, typer.Option(help="Seconds before forcing shutdown of a run.")] = 10.0,
    global_config: GlobalConfigOption = DEFAULT_GLOBAL_CONFIG,
) -> None:
    """Run every generation in a job, chaining each output into the next run."""
    # A real run decodes the input when it writes run 1's copy, so it skips the validation decode.
    job, settings = read_job(job_file, global_config, decode_input=dry_run)
    with invalid_input_exits():
        if dry_run:
            report_ignored_config(job)
            preview = job_service.preview(job, executable=executable)
            for line in plan_lines(job, preview):
                typer.echo(line)
            return
        with held_run_lock("run-job") as lock, open_state(settings) as store:
            # Holding the lock proves no runner is alive, so any row still 'running' is a crash.
            store.sweep_interrupted()
            outcome = job_service.run(job, executable=executable, shutdown_grace=shutdown_grace, write_records=settings.write_job_records, observer=ExecutionRecorder(store), on_child_start=lock.record_child)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


@app.command("import-history")
def import_history_command(
    directory: Annotated[Path | None, typer.Option(help="Directory to search for job manifests; default: the configured output directory.")] = None,
    global_config: GlobalConfigOption = DEFAULT_GLOBAL_CONFIG,
) -> None:
    """Import phase 1 job manifests into the execution history; safe to repeat."""
    settings = load_settings(global_config)
    search = (directory or settings.output_directory).expanduser()
    if not search.is_dir():
        logger.error("Not a directory: {}", search)
        raise typer.Exit(code=2)
    with open_state(settings) as store:
        report = import_history(store, search)
    typer.echo(f"Imported {report.imported}, skipped {report.skipped} already imported, {report.expired} older than the retention period, {report.unreadable} unreadable, from {search}")


@app.command("tui")
def tui_command(
    data_dir: Annotated[Path, typer.Option("--data-dir", help="Directory of job files.")] = DEFAULT_DATA_DIRECTORY,
    executable: ExecutableOption = "draw-things-cli",
    shutdown_grace: Annotated[float, typer.Option(help="Seconds before forcing shutdown of a run.")] = 10.0,
    global_config: GlobalConfigOption = DEFAULT_GLOBAL_CONFIG,
) -> None:
    """Browse, run, and watch the jobs in the data directory in a terminal UI."""
    if shutdown_grace < 0:
        logger.error("--shutdown-grace must not be negative")
        raise typer.Exit(code=2)
    settings = load_settings(global_config)
    # Imported here, so the other commands do not load Textual.
    from draw_things_control.tui.app import DrawThingsApp

    # Jobs run on a worker thread, where signal handlers cannot be installed; the app handles signals itself.
    tui_service = create_job_service(handle_signals=False)
    tui = DrawThingsApp(settings=settings, data_directory=data_dir.expanduser(), executable=executable, job_service=tui_service, shutdown_grace=shutdown_grace)
    # The app owns the terminal, so the stdout and stderr sinks main() installed must not write into it until it exits.
    logger.remove()
    try:
        tui.run()
    finally:
        # A backstop: the app stops a running job when it unmounts; this does nothing when no job runs.
        tui_service.cancel(signal.SIGINT)
        configure_logging()
    # Textual sets a nonzero return code when the app ends on an error, after printing the traceback.
    if tui.return_code:
        raise typer.Exit(code=tui.return_code)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Typer app and preserve its command exit status."""
    configure_logging()
    try:
        app(args=list(argv) if argv is not None else None, prog_name="dtc")
    except SystemExit as error:
        return int(error.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
