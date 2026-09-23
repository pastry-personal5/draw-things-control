"""Typer interface and application wiring for Draw Things control."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from configuration import load_config
from draw_things_arguments import DrawThingsGenerateArguments
from draw_things_runner import DrawThingsProcessRunner
from generation_service import GenerationService

app = typer.Typer(help="Control Draw Things from the command line.", no_args_is_help=True)


def create_runner(arguments: DrawThingsGenerateArguments, timeout: float | None, shutdown_grace: float) -> DrawThingsProcessRunner:
    """Connect the generation use case to its process adapter."""
    return DrawThingsProcessRunner(arguments, timeout_seconds=timeout, shutdown_grace_seconds=shutdown_grace)


service = GenerationService(runner_factory=create_runner, find_executable=shutil.which, config_loader=load_config)


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
    try:
        arguments = service.prepare(options)
        outcome = service.execute(arguments, dry_run=dry_run, timeout=timeout, shutdown_grace=shutdown_grace)
    except ValueError as error:
        logger.error("{}", error)
        raise typer.Exit(code=2) from error
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
    try:
        settings = load_config(config.expanduser())
    except ValueError as error:
        logger.error("{}", error)
        raise typer.Exit(code=2) from error
    typer.echo(f"Valid configuration: {config} (model: {settings.get('model', '(not set)')})")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Typer app and preserve its command exit status."""
    configure_logging()
    try:
        app(args=list(argv) if argv is not None else None, prog_name="main.py")
    except SystemExit as error:
        return int(error.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
