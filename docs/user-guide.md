# User Guide

How to use the `draw-things-control` command line. Every command is run from
the project root as `uv run python main.py <command>`.

## Contents

- [Before you start](#before-you-start)
- [Commands at a glance](#commands-at-a-glance)
- [Generate one image or video](#generate-one-image-or-video)
- [Check a configuration file](#check-a-configuration-file)
- [Jobs: chained batch runs](#jobs-chained-batch-runs)
- [Job file reference](#job-file-reference)
- [Where outputs go](#where-outputs-go)
- [Stopping, failures, and exit codes](#stopping-failures-and-exit-codes)
- [Troubleshooting](#troubleshooting)

## Before you start

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and the
[Draw Things CLI](https://github.com/drawthingsai/draw-things-community)
installed locally. Video jobs also need `ffmpeg` on your `PATH` to extract
last frames.

```bash
uv sync
uv run python main.py --help
```

If `draw-things-cli` is not on your `PATH`, add
`--executable /path/to/draw-things-cli` to `generate` or `run-job`.

For jobs, create the global configuration once:

```bash
cp config/global-config.example.yaml config/global-config.yaml
```

Then edit it. Paths must be absolute (a leading `~` is fine).

| Key | Required | Meaning |
|-----|----------|---------|
| `input_directory` | yes | Where a job's `input` image is looked up; must exist |
| `output_directory` | yes | Root for job outputs; created if missing |
| `write_job_records` | no | `true` also saves a manifest and a log per run (default `false`) |
| `cooldown_seconds` | no | Default wait between a job's runs, 0 to 3600 (default 0) |

Use `--global-config PATH` with `run-job` or `validate-job` to read a
different file.

## Commands at a glance

| Command | Purpose |
|---------|---------|
| `generate` | Generate one image or video |
| `validate-config FILE` | Check a Draw Things JSON configuration |
| `validate-job FILE` | Check a job file; runs nothing |
| `run-job FILE` | Run every generation in a job, chained |

Add `--help` to any command for its full option list.

## Generate one image or video

Text to image needs only a model, a prompt, and an output:

```bash
uv run python main.py generate \
  --model flux_2_klein_4b_q6p.ckpt \
  --prompt "a small red cube on a table" \
  --output cube.png
```

Image to video with a bundled configuration:

```bash
uv run python main.py generate \
  --config-file dt-config/image-to-video-wan-2-2.example.json \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --timeout 3600
```

Always preview first with `--dry-run`: it prints the exact command, with
credentials redacted, and starts nothing.

Common options:

| Option | Meaning |
|--------|---------|
| `-m`, `--model` | Model file; may come from the configuration instead. An explicit `--model` wins |
| `-p`, `--prompt`, `--negative-prompt` | Prompt text |
| `--prompt-file`, `--negative-prompt-file` | Read from a file, or `-` for stdin (only one may use stdin) |
| `--config-file` (alias `--config`) | JSON configuration file. None is used by default |
| `--image` | Reference image; repeat for several, in order |
| `--steps`, `--cfg`, `--width`, `--height`, `--frames`, `--strength`, `-s/--seed` | Generation settings; left out, Draw Things picks its recommended values |
| `-o`, `--output` | Output file. Without it, the image previews in the terminal |
| `--remote`, `--cloud-compute` and their related options | Choose remote or cloud generation instead of local |
| `--timeout SECONDS` | Stop the run if it takes longer |
| `--shutdown-grace SECONDS` | Wait this long after asking to stop before forcing it (default 10) |

## Check a configuration file

```bash
uv run python main.py validate-config dt-config/image-to-video-wan-2-2.example.json
```

The files in `dt-config/` are yours: this tool reads them and never changes
them.

## Jobs: chained batch runs

A job is a YAML file in `data/` describing a chain of generations. It runs
`batch_count` times, and every run starts from the previous run's output (the
last frame, for video). Each run uses one of your named prompt pairs.

The workflow is always the same three steps:

```bash
uv run python main.py validate-job data/example-job.yaml
uv run python main.py run-job data/example-job.yaml --dry-run
uv run python main.py run-job data/example-job.yaml
```

1. `validate-job` reports any problem, naming the field, and shows the
   settings the job resolves to, including the cooldown and where it came
   from.
2. `run-job --dry-run` validates again and prints every command without
   running anything.
3. `run-job` runs the chain.

Start from `data/example-job.yaml`, which is commented line by line. Invalid
jobs are rejected before any generation starts.

## Job file reference

| Key | Meaning |
|-----|---------|
| `version` | Format version, `1` |
| `name` | Base of output file names; `a-z`, `0-9`, `-` only |
| `mode` | `i2i` (image to image), `t2v` (text to video), or `i2v` (image to video) |
| `input` | First input image, looked up in `input_directory`. Required for `i2i` and `i2v`; not allowed for `t2v` |
| `batch_count` | Total number of runs |
| `prompt_pairs` | Named positive/negative prompts; see below |
| `config_file` | A file name in `dt-config/`, the base configuration |
| `config_override` | Settings applied to every run on top of `config_file` |
| `output` | `directory` and `extension` (`mov` or `mp4` for video) |
| `run_timeout_seconds` | Limit for each run |
| `cooldown_seconds` | Wait after each successful run except the last; overrides the global value; `0` turns it off |
| `desired_input_width`, `desired_input_height` | Resize the first input; see below |
| `max_input_crop_percent` | With one desired size, refuse a larger crop (default 10) |

### Prompt pairs

Each pair has a `name`, a `positive` prompt, an optional `negative` prompt,
and either `batches: [1, 3, 5]` (the runs that use it) or `default: true`
(every run no other pair lists). A pair with no `negative` leaves Draw
Things' recommended one in effect.

### Configuration overrides

`config_override` may set `model`, `refiner_model`, `refiner_start`, `steps`,
`guidance_scale`, `shift`, `width`, `height`, `frame_count`, `strength`, and
`seed`. Anything left out comes from `config_file`, then from the model's
recommended settings. With no `seed` in either, one random seed is chosen for
the whole job.

### Input size

For `i2i` and `i2v`, the input image must already be exactly the job's
`width` and `height`, unless you set a desired size. `t2v` jobs have no input:
run 1 generates from text, and later runs continue from the previous last
frame.

`desired_input_width` and `desired_input_height` (1 to 8192, either or both)
resize the first input before run 1. Every run then generates at the
resulting size, and `width` and `height` from the configuration are ignored
(an INFO line says so). Each value is rounded down to a multiple of 64. The
input is never stretched:

- **One key:** the other is derived from the input's aspect ratio, and the few
  leftover pixels are cropped from the center. The job is refused if the crop
  exceeds `max_input_crop_percent`. Example: a 1920x1080 input with
  `desired_input_width: 850` becomes 832x448.
- **Both keys:** the input is scaled to fit and padded with black bars.

A rotated photo is turned upright first, and an embedded color profile is
converted to sRGB. `--dry-run` shows the resized copy as
`'<photo.jpg resized to 832x448>'`. The full rules are in the
[input resize milestone](phase-1/milestone-03-input-image-resize.md).

### Cooldown

Long chains, especially video, can overheat the machine. `cooldown_seconds`
(0 to 3600) makes the job wait after each successful run except the last. Set
it in the global configuration as the default for all jobs, and override it
in a job file; `0` in the job turns the wait off. With neither set, there is no
wait. Ctrl-C ends a wait at once and stops the job.

## Where outputs go

- Files go to `<output_directory>/<name>/`, or `output.directory` under the
  global `output_directory` if the job sets it.
- Names are `<name>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`. Video runs also save
  `<name>-…-last-frame.png`. Nothing is ever overwritten.
- With `write_job_records: true`, each `run-job` also writes
  `<name>-<timestamp>-job.json` (a manifest of every run: prompts, seed,
  files, command, exit code, timing) and `<name>-<timestamp>-job.log`.

## Stopping, failures, and exit codes

Press Ctrl-C to stop. The current run is asked to stop, then forced after the
shutdown grace period. A failed, timed-out, or interrupted run stops the job,
keeps any partial output, and exits with that run's exit code.

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | A run exited with 0 but wrote no output, or last-frame extraction failed |
| 2 | Invalid input: options, configuration, or job file |
| 124 | A run exceeded `--timeout` or `run_timeout_seconds` |
| 130 | Stopped with Ctrl-C (`128 + signal`; 143 for `SIGTERM`, 129 for `SIGHUP`) |
| other | The exit code of `draw-things-cli` |

## Troubleshooting

- **Exit code 2 and a field name:** fix that field; the message names it.
- **`draw-things-cli` not found:** pass `--executable /path/to/draw-things-cli`.
- **Input size refused:** resize the image to the job's `width` and `height`,
  or set `desired_input_width` and/or `desired_input_height`.
- **Last-frame extraction failed:** install `ffmpeg` and make sure it is on
  your `PATH`.
- **Unsure what will run:** add `--dry-run`.
