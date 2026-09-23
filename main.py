"""Command-line helpers for the Draw Things CLI."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

DEFAULT_CONFIG = Path("dt-config/image-to-video-wan-2-2.json")


def load_config(path: Path) -> dict[str, Any]:
    """Load a Draw Things JSON configuration and verify its basic shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Configuration file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(
            "Configuration is not valid JSON: "
            f"{path} ({error.msg} on line {error.lineno})"
        ) from error
    if not isinstance(data, dict):
        raise ValueError("Configuration must contain a JSON object.")
    return data


def build_generate_command(args: argparse.Namespace) -> list[str]:
    """Build the Draw Things command without invoking a shell."""
    config = args.config.expanduser().resolve()
    settings = load_config(config)
    image = args.image.expanduser().resolve()
    if not image.is_file():
        raise ValueError(f"Input image does not exist or is not a file: {image}")
    output = args.output.expanduser()
    if not output.parent.exists():
        raise ValueError(f"Output directory does not exist: {output.parent}")

    command = [args.executable, "generate", "--config-file", str(config)]
    command.extend(["--image", str(image), "--output", str(output)])
    model = args.model or settings.get("model")
    if model:
        command.extend(["--model", str(model)])
    return command


def format_command(command: Sequence[str]) -> str:
    """Return a shell-safe representation intended for display only."""
    import shlex

    return shlex.join(command)


def generate(args: argparse.Namespace) -> int:
    """Validate inputs and run one Draw Things generation request."""
    command = build_generate_command(args)
    if args.dry_run:
        print(format_command(command))
        return 0
    if shutil.which(args.executable) is None:
        raise ValueError(
            f"Could not find '{args.executable}' on PATH. "
            "Install Draw Things CLI or pass --executable with its path."
        )
    try:
        completed = subprocess.run(command, check=False, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"Generation timed out after {args.timeout} seconds.", file=sys.stderr)
        return 124
    return completed.returncode


def validate_config(args: argparse.Namespace) -> int:
    """Check that a configuration is readable JSON."""
    settings = load_config(args.config.expanduser())
    model = settings.get("model", "(not set)")
    print(f"Valid configuration: {args.config} (model: {model})")
    return 0


def create_parser() -> argparse.ArgumentParser:
    """Create the application argument parser."""
    parser = argparse.ArgumentParser(
        description="Control Draw Things from the command line."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser(
        "generate", help="Generate a video from an image."
    )
    generate_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Draw Things JSON configuration (default: {DEFAULT_CONFIG}).",
    )
    generate_parser.add_argument(
        "--image", required=True, type=Path, help="Source image."
    )
    generate_parser.add_argument(
        "--output", required=True, type=Path, help="Output video path."
    )
    generate_parser.add_argument(
        "--model", help="Override the model from the configuration."
    )
    generate_parser.add_argument(
        "--executable", default="draw-things-cli", help="Draw Things CLI executable."
    )
    generate_parser.add_argument(
        "--timeout", type=float, help="Stop the request after this many seconds."
    )
    generate_parser.add_argument(
        "--dry-run", action="store_true", help="Print the command without running it."
    )
    generate_parser.set_defaults(handler=generate)
    validate_parser = subparsers.add_parser(
        "validate-config", help="Validate a Draw Things JSON configuration."
    )
    validate_parser.add_argument(
        "config", type=Path, help="Configuration file to validate."
    )
    validate_parser.set_defaults(handler=validate_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and turn input errors into concise messages."""
    args = create_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
