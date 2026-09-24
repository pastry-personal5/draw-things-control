# draw-things-control

A Python application to control the Draw Things app programmatically.

## Overview

`draw-things-control` is a Python wrapper around the locally installed
`draw-things-cli`. Typer parses the commands, and Loguru reports process output
and status while the runner supervises the CLI process.

## Features

- Generate images or video from prompts, reference images, audio, and JSON overrides
- Run jobs: a YAML file describing a chain of image-to-image, text-to-video, or
  image-to-video runs, each starting from the previous run's output
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
uv run python main.py validate-config dt-config/image-to-video-wan-2-2.example.json

# Preview the image-to-video request from example-command.txt
uv run python main.py generate \
  --config-file dt-config/image-to-video-wan-2-2.example.json \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --dry-run

# Run the request (requires draw-things-cli on PATH)
uv run python main.py generate \
  --config-file dt-config/image-to-video-wan-2-2.example.json \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --timeout 3600

# Text-to-image uses no input image or configuration file
uv run python main.py generate \
  --model flux_2_klein_4b_q6p.ckpt \
  --prompt "a small red cube on a table" \
  --output cube.png
```

## Jobs

A job file describes a chain of generations: `batch_count` runs, each using
one of several named prompt pairs, where every run starts from the previous
run's output (the last frame, for video). See `data/example-job.yaml` for a
commented example and
[`docs/phase-1/milestone-01-job-definition-batch.md`](docs/phase-1/milestone-01-job-definition-batch.md)
for every rule.

First, create the global configuration with your input and output directories:

```bash
cp config/global-config.example.yaml config/global-config.yaml
# edit input_directory and output_directory
```

Then validate, preview, and run a job:

```bash
uv run python main.py validate-job data/example-job.yaml
uv run python main.py run-job data/example-job.yaml --dry-run
uv run python main.py run-job data/example-job.yaml
```

- `mode` is `i2i`, `t2v`, or `i2v`. `i2i` and `i2v` jobs need an `input`
  image in the global `input_directory`. Unless the job sets a desired size
  (below), the input must already be exactly the job's width and height.
  `t2v` jobs have no input; run 1 generates from text, and later runs continue
  from the previous last frame.
- `desired_input_width` and `desired_input_height` (optional, `i2i` and `i2v`
  only, 1 to 8192) resize the first input before run 1, and every run
  generates at the resulting size; `width` and `height` from `config_override`
  and `config_file` are then ignored (an INFO line says so). Each value is
  rounded down to a multiple of 64. The input is never stretched:
  - **One key:** the other is derived from the input's aspect ratio and
    rounded down, and the few pixels left over are cropped from the center.
    The job is refused if the crop is over `max_input_crop_percent` (default
    10). Example: 1920x1080 with `desired_input_width: 850` becomes 832x448
    (scaled to 832x468, 4.3% cropped).
  - **Both keys:** the input is scaled to fit inside and padded with black
    bars (letterbox).

  An input that already has the target's aspect ratio is just scaled, with
  no crop and no bars.

  An input whose EXIF orientation is not upright is always rotated upright
  first, and an embedded color profile (for example, Display P3) is
  converted to sRGB. Resizing is one Lanczos pass in floating point:
  downscaling in linear light with anti-ringing, so fine bright detail keeps
  its brightness and edges stay sharp, and upscaling in sRGB values. The resized copy is a temporary PNG, removed when run 1 ends;
  `--dry-run` shows it as `'<photo.jpg resized to 832x448>'`. See
  [`docs/phase-1/milestone-03-input-image-resize.md`](docs/phase-1/milestone-03-input-image-resize.md).
- `config_file` names a file in `dt-config/`, the base configuration.
  `config_override` changes `model`, `refiner_model`, `refiner_start`,
  `steps`, `guidance_scale`, `shift`, `width`, `height`, `frame_count`,
  `strength`, or `seed` for every run.
- Outputs go to `<output_directory>/<name>` unless `output.directory` is set,
  and are named `<name>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`; video runs also save
  `<name>-…-last-frame.png`. Nothing is ever overwritten.
- With `write_job_records: true` in the global configuration, each
  `run-job` also writes `<name>-<timestamp>-job.json`, a manifest of
  every run (batch, pair, prompts, seed, files, command, exit code, timing),
  and `<name>-<timestamp>-job.log`, the full log. Off by default.
- `cooldown_seconds` (0 to 3600) makes a job wait that long after each
  successful run except the last, so long chains do not overheat the
  machine. Set it in the global configuration as the default for every job
  (the example sets 900, 15 minutes), or in a job file to override it; a
  job's `cooldown_seconds: 0` turns the wait off. With neither, there is no
  wait. `validate-job`, `--dry-run`, the log, and the manifest show the value
  and where it came from (`job`, `global_config`, or `default`). Ctrl-C
  during a cooldown ends it at once and stops the job (exit code 130).
- A failed, timed-out, or interrupted run stops the job, keeps any partial
  output, and exits with that run's exit code. Video jobs need `ffmpeg` on
  `PATH` to extract last frames.
- Pass `--executable /path/to/draw-things-cli` if the CLI is not on `PATH`,
  and `--global-config PATH` to use another global configuration.

## Project Structure

```
.
├── main.py            # Typer commands and dependency wiring
├── configuration.py   # JSON override loading
├── generation_service.py  # Preparation and execution use case
├── draw_things_arguments.py  # Validated generation options and argv building
├── draw_things_runner.py  # Process and signal supervision
├── process_output.py  # Output classification, progress, and Loguru logging
├── global_config.py   # Global input and output directories
├── job_definition.py  # Job file loading and validation
├── job_service.py     # Running and chaining a job's runs
├── generation_config.py  # dt-config/ lookup and override merging
├── input_size.py      # Input image reading, size check, and resize planning
├── input_resize.py    # Writing the resized first input image
├── output_naming.py   # Timestamped output names
├── frame_extraction.py  # Last-frame extraction with ffmpeg
├── job_manifest.py    # Job manifest (JSON)
├── job_log.py         # Job log file
├── config/            # Global configuration example
├── data/              # Job files (example-job.yaml)
├── dt-config/         # Draw Things base configurations
├── docs/research/     # Saved upstream help and the original example command
├── docs/phase-1/      # Phase and milestone plans, and the changelog
├── tests/             # Unit and CLI tests
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
`docs/research/draw-things-cli-generate-help.txt` for the saved upstream help. Use
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

Without `--output`, or with `--terminal-image`, the child inherits the
terminal instead of having its output captured, so `draw-things-cli` can
preview the image inline.

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | A job run exited with 0 but wrote no output, or last-frame extraction failed |
| 2 | Invalid input: options, configuration, or job file |
| 124 | A run exceeded `--timeout` or `run_timeout_seconds` |
| 128 + N | Stopped by signal N: 130 for Ctrl-C, 143 for `SIGTERM`, 129 for `SIGHUP` |
| other | The exit code of `draw-things-cli` |

The command inputs from `docs/research/example-command.txt` and the saved upstream help are
represented by `DrawThingsGenerateArguments`. The runner receives that typed
object, which builds the argument vector without invoking a shell. Unset
options are omitted so Draw Things can apply its own recommended settings.

```python
from pathlib import Path

from draw_things_arguments import DrawThingsGenerateArguments
from draw_things_runner import DrawThingsProcessRunner

arguments = DrawThingsGenerateArguments(
    model="wan_v2.2_a14b_hne_i2v_i8x.ckpt",
    config_file=Path("dt-config/image-to-video-wan-2-2.example.json"),
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
