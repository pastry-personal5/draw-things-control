# draw-things-control

A Python application to control the Draw Things app programmatically.

## Overview

`draw-things-control` is a Python wrapper around the locally installed
`draw-things-cli`. Typer parses the commands, and Loguru reports process output
and status while the runner supervises the CLI process.

## Features

- Generate images or video from prompts, reference images, audio, and JSON overrides
- Validate a configuration before starting a slow generation run
- Preview the command with `--dry-run` (credentials are redacted)
- Select local, remote, or cloud generation options

## Getting Started

### Prerequisites

- Python 3.12 or later
- [Draw Things CLI](https://github.com/drawthingsai/draw-things-community) installed locally

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd draw-things-control

# Install the project environment
uv sync
```

### Running

```bash
# Inspect CLI options
uv run python main.py --help

# Check a bundled configuration
uv run python main.py validate-config dt-config/image-to-video-wan-2-2.json

# Preview the image-to-video request from example-command.txt
uv run python main.py generate \
  --config-file dt-config/image-to-video-wan-2-2.json \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --dry-run

# Run the request (requires draw-things-cli on PATH)
uv run python main.py generate \
  --config-file dt-config/image-to-video-wan-2-2.json \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --timeout 3600

# Text-to-image uses no input image or configuration file
uv run python main.py generate \
  --model flux_2_klein_4b_q6p.ckpt \
  --prompt "a small red cube on a table" \
  --output cube.png
```

## Project Structure

```
.
├── main.py            # Typer commands and dependency wiring
├── configuration.py   # JSON override loading
├── generation_service.py  # Preparation and execution use case
├── draw_things_arguments.py  # Validated generation options and argv building
├── draw_things_runner.py  # Process and signal supervision
├── process_output.py  # Output classification, progress, and Loguru logging
├── dt-config/         # Example Draw Things configurations
├── draw-things-cli-generate-help.txt  # Saved upstream generate help
├── example-command.txt  # Original image-to-video command
├── tests/             # CLI, argument, and runner tests
├── pyproject.toml     # Project metadata and dependencies
├── uv.lock            # Lockfile for deterministic installs
├── README.md          # This file
└── AGENTS.md          # Agent instructions for this repository
```

## Configuration

Use `--config-file` (or its wrapper alias `--config`) for a JSON override. No
configuration is selected by default. Pass `--model`, or use a configuration
whose `model` setting supplies it. Explicit `--model` takes precedence. Repeat
`--image` to provide ordered reference images. `--prompt-file -` or
`--negative-prompt-file -` reads from stdin; only one may use stdin per run.
`--output` is optional when the underlying CLI can preview in the terminal.
Run `uv run python main.py generate --help` for the wrapper options, or see
`draw-things-cli-generate-help.txt` for the saved upstream help. Use
`--executable /path/to/draw-things-cli` if the CLI is not on `PATH`.

## Architecture

`main.py` handles terminal input and display. `GenerationService` validates
paths, resolves the model and configuration, and decides whether to preview or
run. `DrawThingsGenerateArguments` owns option validation and command
serialization. `DrawThingsProcessRunner` handles process lifetime and signals;
`OutputProcessor` handles the two output streams. The service receives its
runner and executable lookup as dependencies, so its behavior can be tested
without starting a process.

## Process supervision

Generation runs through `DrawThingsProcessRunner`, which starts
`draw-things-cli` in a separate process group. Its stdout and stderr are read
concurrently, labelled, forwarded live, and retained as timestamped messages
for programmatic consumers. Progress-shaped output such as `12/40` is exposed
as structured progress data. Loguru routes child stdout to stdout and child
stderr, warnings, and status messages to stderr.

`SIGHUP`, `SIGINT`, and `SIGTERM` request graceful shutdown: the runner sends
`SIGTERM` to the entire child process group and waits 10 seconds by default.
Set `--shutdown-grace SECONDS` to change that window. If the child remains
alive after the grace period, the runner sends `SIGKILL` to the group. `SIGKILL`
cannot itself be caught or handled by a process, so it is used only as the
forced final fallback. `--timeout SECONDS` bounds total generation time and
uses the same graceful-then-forced shutdown sequence.

The command inputs from `example-command.txt` and the saved upstream help are
represented by `DrawThingsGenerateArguments`. The runner receives that typed
object, which builds the argument vector without invoking a shell. Unset
options are omitted so Draw Things can apply its own recommended settings.

```python
from pathlib import Path

from draw_things_arguments import DrawThingsGenerateArguments
from draw_things_runner import DrawThingsProcessRunner

arguments = DrawThingsGenerateArguments(
    model="wan_v2.2_a14b_hne_i2v_i8x.ckpt",
    config_file=Path("dt-config/image-to-video-wan-2-2.json"),
    image=Path("/path/to/source.png"),
    output=Path("output.mov"),
)
result = DrawThingsProcessRunner(arguments, timeout_seconds=3600).run()
```

## Development

```bash
make check
```

## License

Apache-2.0
