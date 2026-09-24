# Milestone 01: Job Definition and Chained Batch Runs

**Phase:** [Phase 1: Basic Functionalities](README.md)
**Status:** done
**Depends on:** [Milestone 02: Runner fixes](milestone-02-runner-fixes.md), done first

## Goal

Let the app read a YAML job definition from `data/` and run every generation it
describes: `batch_count` runs in total, where each run uses one of several named
prompt pairs according to a batch schedule, as one continuous chain. Each job
declares its mode: image-to-image (`i2i`), text-to-video (`t2v`), or
image-to-video (`i2v`). For `i2i` and `i2v`, the input file seeds run 1; for
`t2v`, run 1 generates from text alone. Each run's output (or the last frame of
a video output) becomes the input of the next run.

## Scope

In scope:

- Global configuration file `config/global-config.yaml` (below)
- YAML job file format, version 1 (below), including the job `mode`, a base
  `config_file` from `dt-config/`, and `config_override` settings applied to
  every run
- Timestamped output file names (below)
- Loading and validating a job before anything runs
- Expanding the job into an ordered list of runs
- Chaining outputs to inputs, including last-frame extraction for video
- `run-job` and `validate-job` CLI commands, with `--dry-run`. Both take the
  job file's path (for example, `data/example-job.yaml`), not a bare job name
- A job manifest recording every run (below)
- An example job at `data/example-job.yaml`

Out of scope:

- Resuming a failed job partway through
- Running prompt pairs as independent (unchained) branches
- Batch ranges or patterns (`1-5`, `every 2nd`); batches are listed explicitly
- Per-run overrides beyond prompts and seed
- Text-to-image jobs (`t2i`)
- Resizing input images, including sizing a job from its input image
  (`size_from_input`, `max_pixels`); planned for a later milestone. Until then
  the first input must already match the job's width and height
- Remote and cloud generation (jobs run locally; `--executable` can name the
  `draw-things-cli` binary)
- Changes to the one-off `generate` command: modes and automatic output names
  apply to jobs only
- Runner bug fixes: these are [Milestone 02](milestone-02-runner-fixes.md)

## Global configuration

The app reads `config/global-config.yaml` at startup. For this milestone it
holds the default input and output directories, as absolute paths, and
whether jobs save records:

```yaml
version: 1
input_directory: /Users/me/draw-things/input     # default location of job input files
output_directory: /Users/me/draw-things/output   # default root for job outputs
write_job_records: false                         # optional; true saves the job manifest and log
```

Rules:

- Both directories must be absolute paths. A leading `~` is expanded first,
  so `~/draw-things/input` is accepted. A relative path is a validation error.
- `version`, `input_directory`, and `output_directory` are required, and
  unknown keys are errors.
- `write_job_records` is optional and must be `true` or `false`; it defaults
  to `false`. Only when it is `true` does `run-job` write the job manifest and
  job log (below). Otherwise the output directory holds only generated files.
- `input_directory` must already exist. `output_directory` is created if
  missing.
- `config/global-config.yaml` is not committed (it is git-ignored). The
  repository provides `config/global-config.example.yaml`; copy it to
  `config/global-config.yaml` and edit the paths.
- If the file is missing or invalid, every command that needs it fails with
  exit code 2 and names the file and field. A missing file's message tells the
  user to copy the example.
- `run-job` and `validate-job` accept `--global-config PATH` to use a
  different file; tests use it too.

## Job definition format

```yaml
version: 1                             # required; must be 1
name: sunset-walk                      # required; job name, the base of output file names; [a-z0-9-]
mode: i2v                              # required; i2i, t2v, or i2v
input: first-frame.png                 # i2i/i2v: required, run 1 only; t2v: not allowed
batch_count: 5                         # total runs (batches) in the job, >= 1

prompt_pairs:                          # at least one
  - name: walk                         # unique; [a-z0-9-]
    positive: "a woman walks along the beach at sunset"
    negative: "blurry, distorted"      # optional
    batches: [1, 3, 5]                 # optional; batch numbers that use this pair
  - name: wave
    positive: "she turns and waves at the camera"
    batches: [2, 4]
  - name: idle                         # the default pair (at most one)
    positive: "she stands still, gentle breeze"
    default: true                      # used by every batch not listed above

output:
  directory: sunset-walk               # optional; created if missing; omitted: <output_directory>/<name>
  extension: mov                       # optional; i2i: png; t2v/i2v: mov (default) or mp4

config_file: image-to-video-wan-2-2.example.json   # required; file name only, found in dt-config/; the base configuration

config_override:                       # optional; applied on top of config_file for every run
  model: wan_v2.2_a14b_hne_i2v_i8x.ckpt          # required here or in config_file
  refiner_model: wan_v2.2_a14b_lne_i2v_i8x.ckpt
  refiner_start: 0.1                   # 0..1; fraction of steps before the refiner takes over
  steps: 40
  guidance_scale: 5.0
  shift: 3.99
  width: 832                           # pixels, multiple of 64
  height: 448                          # pixels, multiple of 64
  frame_count: 17                      # video modes only
  strength: 1.0                        # 0..1
  seed: 829023514                      # omitted: config_file's seed, else one random seed for the whole job

run_timeout_seconds: 3600              # optional; limit for each run, not the whole job
```

Rules:

- Path resolution (`~` is expanded, and absolute paths are used as-is):
  - `input`: relative to the global `input_directory`. Leading and trailing
    whitespace is stripped first; a value that is only whitespace is an error.
  - `output.directory`: relative to the global `output_directory`. If
    omitted, it is `<output_directory>/<name>`, so each job gets its own
    folder.
- Unknown keys are errors, so typos fail early.
- **Job name:** `name` is required. It is 1 to 64 characters of lowercase
  letters, digits, and hyphens, starting and ending with a letter or digit
  (`^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$`), so it is safe in file names on
  every platform. It is the base of every output file name and of the
  manifest and log names, and the default output directory.
- **Base configuration:** `config_file` is a file name only, such as
  `image-to-video-wan-2-2.example.json`. The app looks for it in the `dt-config/`
  directory of this repository (next to `main.py`), whatever the current
  directory or the job file's location. A value containing a path separator
  or `..`, a file that is not in `dt-config/`, or a file that does not hold a
  JSON object is a validation error; the message for a missing file lists the
  `.json` files that are in `dt-config/`. `config_file` is required, and the
  file's settings are the base for every run.
  In `i2v` jobs the file's `batchCount` is ignored (the job's `batch_count`
  sets the number of runs); when it is present, an INFO message says so in
  `validate-job`, `run-job --dry-run`, and `run-job` (and its log file).
- **Configuration overrides:** every key in `config_override` overrides the
  base configuration for all runs; a prompt pair cannot override them. Keys
  are lowercase snake_case, say what they mean rather than copying abbreviated
  CLI flags or Draw Things' camelCase keys, and carry no units in the value
  (units are in the name or documented here). Only the keys below are
  accepted; to change any other Draw Things setting (sampler, LoRAs, and so
  on), use a different file in `dt-config/`.

  | `config_override` key | Overrides config key | Passed as | Validation |
  |-----------------------|----------------------|-----------|------------|
  | `model` | `model` | `--model` | non-empty; required unless `config_file` sets `model` |
  | `refiner_model` | `refinerModel` | `--config-json` | non-empty string |
  | `refiner_start` | `refinerStart` | `--config-json` | `0 <= x <= 1`; requires a refiner model here or in `config_file` |
  | `steps` | `steps` | `--steps` | integer `>= 1` |
  | `guidance_scale` | `guidanceScale` | `--cfg` | number `>= 0` |
  | `shift` | `shift` | `--config-json` | number `> 0` |
  | `width`, `height` | `width`, `height` | `--width`, `--height` | positive multiple of 64; `i2i`/`i2v`: must equal the input image's size (see below) |
  | `frame_count` | `numFrames` | `--frames` | integer `>= 1`; not allowed in `i2i` jobs |
  | `strength` | `strength` | `--strength` | `0 <= x <= 1` |
  | `seed` | `seed` | `--seed` | integer from 0 to 4294967295 (UInt32) |

  Values that are flags are validated by `DrawThingsGenerateArguments`. Keys
  set in neither place fall back to the model's recommended settings.
- **Configuration merge:** the app reads `config_file`, applies the
  `config_override` keys that have no `draw-things-cli` flag (`refiner_model`,
  `refiner_start`, `shift`), and passes the result as one `--config-json` (it
  does not pass `--config-file`). The other overrides are passed as flags.
  Later layers win:
  1. the model's recommended settings (applied by `draw-things-cli`)
  2. `config_file`
  3. `config_override`
- **Named prompt pairs:** each entry of `prompt_pairs` has a user-chosen,
  unique `name` (lowercase letters, digits, and hyphens), a required
  `positive` prompt, an optional `negative` prompt, and a `batches` list. If
  `negative` is omitted, no negative prompt is passed, so the model's
  recommended negative prompt applies. Duplicate names are a validation error.
- **Batch schedule:** `batches` lists the batch numbers (1-based) that use the
  pair. In the example above, with `batch_count: 5`, pair `walk` runs as
  batches 1, 3, and 5, and pair `wave` as batches 2 and 4. `batches` is
  optional. A batch listed twice (in one pair or across pairs) or outside
  `1..batch_count` is a validation error naming the batch number.
- **Default pair:** at most one pair may set `default: true`. Every batch that
  no pair lists explicitly uses the default pair, so in the example, with
  `batch_count: 5`, `idle` is used for no batch, and with `batch_count: 7` it
  is used for batches 6 and 7. The default pair may also list `batches`; those
  assignments apply as written. If a batch is unassigned and no pair is marked
  default, the job is invalid, naming the batch number. A job with a single
  pair needs no marker: that pair is the default.
- **Mode:** `mode` is required and is one of `i2i`, `t2v`, or `i2v`:

  | Mode | `input` | Run 1 | Runs 2+ | Default extension | Allowed extensions |
  |------|---------|-------|---------|-------------------|--------------------|
  | `i2i` | required | `--image input` | previous PNG | `png` | `png` |
  | `t2v` | not allowed | no `--image` | previous last frame | `mov` | `mov`, `mp4` |
  | `i2v` | required | `--image input` | previous last frame | `mov` | `mov`, `mp4` |

  A missing or unknown mode, an `input` that conflicts with the mode, and an
  extension the mode does not allow are validation errors naming the field.
  A `t2v` job continues as image-to-video from run 2, so its model must
  support both text-to-video and image-to-video (for example, LTX).
  Text-to-image jobs are out of scope.
- **Seed:** like every other key, the seed is `config_override.seed` if set,
  otherwise `config_file`'s `seed`. If neither sets one, the app draws one
  random seed when the job starts. Every run uses the same seed; it is passed
  as `--seed`, logged once, and written to the manifest, so the job can be
  reproduced.

## Input image size check

Milestone 01 does not resize input images. In `i2i` and `i2v` jobs, the first
input image must already have exactly the job's width and height; otherwise
the job is refused before anything runs.

1. **Find the job's size.** `width` and `height` each come from
   `config_override` if set there, otherwise from `config_file`. If either is
   set in neither place, validation fails, naming `config_override.width` /
   `config_override.height` and the config file.
2. **Read the input's size.** Pillow opens `input` and reads its width and
   height from the file header, without decoding the pixels. If the EXIF
   orientation tag is 5, 6, 7, or 8 (rotated 90° or 270°), width and height are
   swapped, so the size matches how the image is displayed. A file Pillow
   cannot read is a validation error naming `input`.
3. **Compare exactly.** If the two sizes differ in any way, the job fails with
   exit code 2 and nothing runs. The message names the file, both sizes, and
   where the job's size came from, for example:

   ```
   Input image /Users/me/draw-things/input/first-frame.png is 1920x1080, but the
   job size is 832x448 (width and height from config_file
   image-to-video-wan-2-2.example.json). Resize the image to 832x448, or set
   config_override.width and config_override.height to 1920 and 1080.
   Automatic resizing is planned for a later milestone.
   ```

The check runs in `validate-job`, `run-job --dry-run`, and `run-job`, and only
for the first input: later runs start from earlier outputs, which
`draw-things-cli` writes at the job's size. `t2v` jobs have no input image and
are not checked, but still need no width or height of their own (the model's
recommended size applies if neither place sets them).

## Run expansion and chaining

The job expands into exactly `batch_count` runs, in batch-number order. Each
batch uses the prompt pair that lists it, or the default pair if none does.
For the example with `batch_count: 7`:

```
batch 1 (walk), 2 (wave), 3 (walk), 4 (wave), 5 (walk), 6 (idle), 7 (idle)
```

All runs form one chain:

- Run 1 uses `input` (`i2i`, `i2v`) or no input image (`t2v`).
- Run k+1 uses the output of run k. For `.png` output that is the PNG itself;
  for `.mov` / `.mp4` output the app extracts the last frame to a PNG with
  `ffmpeg` and uses that.
- A run succeeds only if `draw-things-cli` exits with 0 and the output file
  exists afterwards. An exit code of 0 with no output file is a failure (exit
  code 1), because the next run would have no input.
- If any run fails, times out, or is interrupted, the job stops: later runs
  have no valid input. The exit code is the failed run's exit code. Any
  partial output file is kept, not deleted or renamed, and the error message
  names its path.

Output names, inside `output.directory`, are built for each run just before
it starts by joining these parts with hyphens:

- base: the job's `name`
- timestamp: `datetime.now().strftime("%Y%m%d-%H%M%S")`
- a random 4-digit number (`1000`–`9999`)
- the extension: `.png` for `i2i`, `.mov` for `t2v` and `i2v`, unless
  `output.extension` overrides it

```
sunset-walk-20260924-153012-4821.mov                 # generated output
sunset-walk-20260924-153012-4821-last-frame.png      # video only
```

A run never overwrites an existing file: if the generated output name, or its
last-frame name, already exists, the app draws a new random number and tries
again.

## Job manifest

Output names do not say which batch or prompt pair made them, so every
`run-job` (not `--dry-run`) writes a manifest to `output.directory` when the
global `write_job_records` is `true`, named
after the time the job starts: `<name>-<YYYYmmdd-HHMMSS>-job.json`. It is
written before run 1 and rewritten after each run, so it is current even if
the job stops partway through.

```json
{
  "job_file": "/Volumes/Work_Volume/draw-things-control/data/example-job.yaml",
  "name": "sunset-walk",
  "mode": "i2v",
  "config_file": "image-to-video-wan-2-2.example.json",
  "config_override": {"model": "wan_v2.2_a14b_hne_i2v_i8x.ckpt", "steps": 40},
  "seed": 829023514,
  "seed_source": "config_file",
  "started_at": "2026-09-24T15:30:12+09:00",
  "finished_at": null,
  "status": "running",
  "log_file": "sunset-walk-20260924-153012-job.log",
  "runs": [
    {
      "batch": 1,
      "pair": "walk",
      "positive": "a woman walks along the beach at sunset",
      "negative": "blurry, distorted",
      "input": "/Users/me/draw-things/input/first-frame.png",
      "output": "sunset-walk-20260924-153012-4821.mov",
      "last_frame": "sunset-walk-20260924-153012-4821-last-frame.png",
      "command": ["draw-things-cli", "generate", "--model", "..."],
      "started_at": "2026-09-24T15:30:12+09:00",
      "seconds": 812.4,
      "exit_code": 0,
      "status": "succeeded"
    }
  ]
}
```

- `seed_source` is `config_override`, `config_file`, or `random`.
- Job `status` is `running`, `succeeded`, `failed`, or `interrupted`. Run
  `status` is `running`, `succeeded`, `failed`, `timed_out`, or `interrupted`.
- `config_override` holds the job's overrides as written. `command` is the
  exact argument list, with the same secret redaction as `--dry-run`.
- `output` and `last_frame` are file names in `output.directory`. A failed
  run's partial `output` is recorded if the file exists, otherwise `null`.
- The manifest name also avoids existing files: if it exists, a 4-digit random
  number is added (`<name>-<timestamp>-<NNNN>-job.json`).

## Job log file

When `write_job_records` is `true`, every `run-job` (not `--dry-run`) also
saves its full log beside the
manifest, as `<name>-<YYYYmmdd-HHMMSS>-job.log`, with the same timestamp (and
the same extra random number, if the manifest needed one). It receives
everything the terminal shows: the app's messages, each run's child stdout
and stderr, progress, and errors. Each line starts with the local time and
the stream (`app`, `stdout`, or `stderr`). The terminal output is unchanged.
The log is opened before run 1 and flushed after every line, so it is
complete up to the moment a job stops, and its name is recorded in the
manifest as `log_file`.

## Interrupting a job

`SIGINT` (Ctrl-C), `SIGTERM`, or `SIGHUP` stops the job at once:

1. The current run is stopped gracefully, using the runner's shutdown
   (`SIGTERM` to its process group, then `SIGKILL` after `--shutdown-grace`).
2. No later run starts.
3. The current run and the job are marked `interrupted` in the manifest, the
   log records which signal stopped them, and any partial output is kept and
   named.
4. The app exits with `128 + signal number`: 130 for Ctrl-C, 143 for
   `SIGTERM`, 129 for `SIGHUP`. This relies on the exit-code fix in
   [Milestone 02](milestone-02-runner-fixes.md).

A signal between runs (for example, during last-frame extraction) stops the
job the same way, before the next run starts.

## Planned changes

| File | Change |
|------|--------|
| `pyproject.toml`, `uv.lock` | Add `pyyaml` and `pillow` (via `uv add pyyaml pillow`) |
| `global_config.py` (new) | `GlobalConfig` dataclass; `load_global_config(path)` parses and validates the YAML, expands `~`, and rejects relative directories |
| `config/global-config.example.yaml` (new, committed) | Template with placeholder absolute `input_directory` and `output_directory`; users copy it to `config/global-config.yaml` and edit it |
| `.gitignore` | Ignore `config/global-config.yaml`, because its absolute paths are specific to one machine |
| `job_definition.py` (new) | `GenerationMode` enum (`i2i`, `t2v`, `i2v`); `JobDefinition` / `ConfigOverride` / `PromptPair` (name, positive, negative, batches) dataclasses; `load_job(path, global_config)` parses YAML, resolves paths using the global directories, and validates keys, types, the job `name`, mode, mode-dependent `input`, extension, and `frame_count`, the `config_file` name, `config_override` keys, pair names, and the batch schedule; raises `ValueError` with the field name |
| `generation_config.py` (new) | `DT_CONFIG_DIRECTORY` (the repository's `dt-config/`); `find_config_file(name, directory)` validates a bare file name and lists available files when it is missing; `build_config_json(base, override)` applies `refiner_model`, `refiner_start`, and `shift` onto the base configuration |
| `output_naming.py` (new) | `output_name(base, extension, now, number)` builds `<base>-<timestamp>-<number>.<ext>`; `next_output_path(directory, base, extension, clock, random_number)` retries while the output or its last-frame file exists |
| `job_service.py` (new) | `JobService`: expands a job into runs, builds each run's `DrawThingsGenerateArguments` directly from the validated job and runs it through `GenerationService.execute`, names each run's output just before it starts, executes runs in order, and wires chaining. Dependencies (runner factory, frame extractor, seed source, clock, random number source) are injected for tests |
| `job_log.py` (new) | Adds and removes a Loguru file sink for one job, with the line format above; the terminal sinks in `main.py` are unchanged |
| `job_manifest.py` (new) | `JobManifest` dataclass and `write_manifest(path, manifest)`, which writes to a temporary file and renames it, so a crash never leaves a half-written manifest |
| `input_size.py` (new) | `read_image_size(path)` reads the header with Pillow and applies EXIF rotation; `check_input_size(image_size, job_size, source)` raises `ValueError` with the message above when they differ |
| `frame_extraction.py` (new) | `extract_last_frame(video, png)` using `ffmpeg -sseof`; clear error if `ffmpeg` is not on `PATH` |
| `main.py` | `run-job JOB_FILE [--dry-run] [--executable] [--shutdown-grace] [--global-config]` and `validate-job JOB_FILE [--global-config]` commands |
| `data/example-job.yaml` (new) | Commented example job based on `dt-config/image-to-video-wan-2-2.example.json` |
| `tests/test_global_config.py` (new) | Loading, validation, `~` expansion, and rejection of relative paths |
| `tests/test_job_definition.py` (new) | Loading and validation cases, including resolution against the global directories and every `config_override` key |
| `tests/test_input_size.py` (new) | Matching and mismatched sizes, EXIF rotation, an unreadable file, size taken from `config_override` over `config_file`, a missing width or height |
| `tests/test_generation_config.py` (new) | Finding a config file by name, rejecting paths and missing files, merge order, refiner keys |
| `tests/test_job_manifest.py` (new) | Manifest content after success, failure, and interruption; `seed_source`; `log_file`; atomic rewrite |
| `tests/test_output_naming.py` (new) | Name format, per-mode default extension, retry on an existing file |
| `tests/test_job_service.py` (new) | Expansion order, chaining for each mode (including `t2v` run 1 without `--image`), seed precedence, stop-on-failure, exit 0 without an output file, partial output kept, dry run writes no manifest |
| `README.md` | Document the global configuration, job files, and the new commands |

Logging per run: `Run 3/5 (batch 3, pair walk): input=…, output=…` (the job seed is logged once at start), and a
final summary with completed/total runs.

`--dry-run` performs the same full validation as a real run (the mode and its
input rules, the input file and config file exist, `ffmpeg` is available for
video output) and then prints every command in order without running anything.
Output names contain the time and a random number, so the names a dry run
prints are examples; a real run generates new ones.
A job that passes a dry run is expected to start for real. Because nothing is
generated, chained inputs show the paths the previous runs would produce.

## Acceptance criteria

- [x] `uv run python main.py validate-job data/example-job.yaml` reports the job
      as valid, with its run count and resolved input and output paths.
- [x] A missing or invalid `config/global-config.yaml` (missing key, unknown
      key, relative directory, missing input directory) fails with exit code 2 and names the file and field.
- [x] A job with a relative `input` reads it from the global
      `input_directory`; a job without `output.directory` writes to
      `<output_directory>/<name>`, creating it if missing.
- [x] Invalid jobs (unknown key, missing or invalid `name`, missing
      `prompt_pairs` or `positive`,
      duplicate pair name, more than one default pair, an unassigned batch with
      no default pair, a batch assigned twice or outside `1..batch_count`,
      `batch_count < 1`, missing or unknown `mode`, missing input file,
      extension not allowed for the mode, an out-of-range or mistyped
      `config_override` value, an unknown `config_override` key,
      `frame_count` in an `i2i` job, `refiner_start` without a refiner model,
      a missing `config_file`, a `config_file` that is a path or is not in
      `dt-config/`, an `i2i`/`i2v` job with no width or height in either
      place, an input image Pillow cannot read) fail with
      exit code 2 and a message naming the field, and nothing runs.
- [x] `run-job --dry-run` fails on a missing input file or a missing config
      file, exactly as a real run would.
- [x] An `i2i` or `i2v` job without `input`, and a `t2v` job with `input`, are
      rejected, naming the field.
- [x] `config_file: image-to-video-wan-2-2.example.json` loads
      `dt-config/image-to-video-wan-2-2.example.json` when the app is started from
      another directory.
- [x] An `i2i` or `i2v` job whose input image is not exactly the job's width
      and height fails with exit code 2 in `validate-job`, `run-job
      --dry-run`, and `run-job`, before any run starts, and the message gives
      both sizes and where the job's size came from. An input of exactly that
      size passes.
- [x] Every run of a job receives the same `config_override` settings: the
      same flags, and one `--config-json` holding the base configuration with
      `refinerModel` and `refinerStart` replaced by `refiner_model` and
      `refiner_start`.
- [x] A `t2v` job's run 1 has no `--image`; its run 2 uses run 1's last frame.
- [x] An `i2i` job writes `.png` outputs and a `t2v` or `i2v` job writes `.mov`
      outputs when `output.extension` is omitted.
- [x] Every output is named `<name>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`, using the
      job's `name`, and a
      name that already exists is replaced by a new random number instead of
      being overwritten.
- [x] `run-job --dry-run` prints `batch_count` commands in batch order, with
      the prompts of the pair assigned to each batch (the example job with
      `batch_count: 7` gives walk, wave, walk, wave, walk, idle, idle), and
      each run's `--image` equal to the previous run's
      (predicted) output or last frame.
- [x] A real run of a 2-pair, 4-batch video job produces 4 videos and 4
      last-frame PNGs with the documented names; run k+1's input is run k's last
      frame.
- [x] A failing run stops the job, and the app exits with that run's exit code.
      Its partial output, if any, is kept and named in the error.
- [x] A run that exits with 0 but leaves no output file stops the job with
      exit code 1.
- [x] The seed is `config_override.seed`, else `config_file`'s `seed`, else
      one random seed; every run uses it, and the manifest records it with
      its source.
- [x] Without `write_job_records: true`, `run-job` writes no `-job.json` or
      `-job.log` file.
- [x] With it, `run-job` writes `<name>-<timestamp>-job.json` before run 1 and
      updates it after every run, including when a run fails or is
      interrupted; `--dry-run` writes no manifest.
- [x] With it, `run-job` writes `<name>-<timestamp>-job.log` beside the manifest,
      holding the same messages and child output as the terminal, and
      `--dry-run` writes no log.
- [x] Ctrl-C during a run stops that run and the job, starts no later run,
      marks both `interrupted` in the manifest, keeps any partial output, and
      exits with 130; `SIGTERM` exits with 143.
- [x] A job file without `version: 1` is rejected, naming `version`.
- [x] `make check` passes, and the new tests use no real subprocess or `ffmpeg`.

## Verification

- `make check`: lint clean, 65 tests pass. The job tests use fake runners and
  a fake frame extractor; no `draw-things-cli` or `ffmpeg` process runs.
- End to end on macOS with a stand-in `draw-things-cli` script that writes a
  17-frame 832×448 `.mov` with `ffmpeg`: a 5-run `i2v` job completed, chained
  each last frame into the next run, and wrote the manifest and log. The
  extracted last frame is pixel-identical to frame 17 of the video.
- The same stand-in setup confirmed a failing run (exit 3), exit 0 without an
  output file (exit 1), and Ctrl-C during a run (exit 130, partial output
  kept, no child left running).
- `run-job --dry-run` with the real `draw-things-cli` binary printed all 7
  commands for `data/example-job.yaml`. No real generation was run.

## Open questions

None.
