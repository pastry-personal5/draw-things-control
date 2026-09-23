# draw-things-control

A Python application to control the Draw Things app programmatically.

## Overview

`draw-things-control` is a small, safe wrapper around the locally installed
`draw-things-cli`. It validates the JSON configuration and source image before
starting a generation request.

## Features

- Generate video from an image with a Draw Things JSON configuration
- Validate a configuration before starting a slow generation run
- Preview the exact command with `--dry-run`
- Override the configured model for an individual request

## Getting Started

### Prerequisites

- Python 3.12 or later
- [Draw Things](https://github.com/daudlix/draw-things) running locally or accessible via API

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

# Preview a request without calling Draw Things
uv run python main.py generate \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --dry-run

# Run the request (requires draw-things-cli on PATH)
uv run python main.py generate \
  --image /path/to/source.png \
  --output /path/to/output.mov
```

## Project Structure

```
.
├── main.py            # CLI implementation
├── dt-config/         # Example Draw Things configurations
├── tests/             # Standard-library unit tests
├── pyproject.toml     # Project metadata and dependencies
├── uv.lock            # Lockfile for deterministic installs
├── README.md          # This file
└── AGENTS.md          # Agent instructions for this repository
```

## Configuration

Pass `--config` to choose a JSON configuration; by default the image-to-video
WAN 2.2 example is used. `--model` overrides its `model` setting for one run.
Use `--executable /path/to/draw-things-cli` if the CLI is not on `PATH`.

## Development

```bash
make check
```

## License

Apache-2.0
