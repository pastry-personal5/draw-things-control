# User Guide

How to use the `draw-things-control` command line. Every command is run from
the project root as `uv run dtc <command>`.

## Contents

- [Before you start](#before-you-start)
- [Commands at a glance](#commands-at-a-glance)
- [Generate one image or video](#generate-one-image-or-video)
- [Check a configuration file](#check-a-configuration-file)
- [Jobs: chained runs](#jobs-chained-runs)
- [Job file reference](#job-file-reference)
- [Where outputs go](#where-outputs-go)
- [Browse jobs in the terminal UI](#browse-jobs-in-the-terminal-ui)
- [Execution history and the run lock](#execution-history-and-the-run-lock)
- [Stopping, failures, and exit codes](#stopping-failures-and-exit-codes)
- [Troubleshooting](#troubleshooting)

## Before you start

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and the
[Draw Things CLI](https://github.com/drawthingsai/draw-things-community)
installed locally. Video jobs also need `ffmpeg` on your `PATH` to extract
last frames.

```bash
uv sync
uv run dtc --help
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
| `cooldown` | no | The wait between a job's runs: a mapping with `mode` `auto`, `manual`, or `off`; see [Cooldown](#cooldown) (default `auto`) |
| `history_retention_days` | no | Days of execution history to keep, 0 to 3650; 0 keeps it forever (default 14) |

Use `--global-config PATH` with `run-job` or `validate-job` to read a
different file.

## Commands at a glance

| Command | Purpose |
|---------|---------|
| `generate` | Generate one image or video |
| `validate-config FILE` | Check a Draw Things YAML or JSON configuration |
| `validate-job FILE` | Check a job file; runs nothing |
| `run-job FILE` | Run every generation in a job, chained |
| `import-history` | Import phase 1 job manifests into the execution history |
| `tui` | Browse, run, and watch jobs in a terminal UI |

Add `--help` to any command for its full option list.

## Generate one image or video

Text to image needs only a model, a prompt, and an output:

```bash
uv run dtc generate \
  --model flux_2_klein_4b_q6p.ckpt \
  --prompt "a small red cube on a table" \
  --output cube.png
```

Image to video with a bundled configuration:

```bash
uv run dtc generate \
  --config-file dt-config/image-to-video-wan-2-2.example.yaml \
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
| `--config-file` (alias `--config`) | YAML or JSON configuration file; see [base configurations](#base-configurations). None is used by default |
| `--image` | Reference image; repeat for several, in order |
| `--steps`, `--cfg`, `--width`, `--height`, `--frames`, `--strength`, `-s/--seed` | Generation settings; left out, Draw Things picks its recommended values |
| `-o`, `--output` | Output file. Without it, the image previews in the terminal |
| `--remote`, `--cloud-compute` and their related options | Choose remote or cloud generation instead of local |
| `--timeout SECONDS` | Stop the run if it takes longer |
| `--shutdown-grace SECONDS` | Wait this long after asking to stop before forcing it (default 10) |

## Base configurations

A base configuration holds Draw Things settings (`model`, `steps`,
`sharpness`, and so on, under Draw Things' own key names). Write them as YAML
files in `dt-config/`, one mapping of keys to values:

```yaml
# Wan 2.2 A14B image-to-video
model: wan_v2.2_a14b_hne_i2v_i8x.ckpt
refinerModel: wan_v2.2_a14b_lne_i2v_i8x.ckpt
steps: 40
shift: 3.99
loras: []
```

`draw-things-cli` reads only JSON, so `dtc` converts the YAML and passes it
inline with `--config-json`. It never writes a JSON file.

YAML is read strictly, and a file that breaks a rule is rejected (exit code 2)
with its name and the line or key path, for example `loras[0].version`:

- The file must hold exactly one mapping, and every key must be a string.
- A key may not appear twice in one mapping; that is usually a typo.
- Unquoted dates (`2026-09-25`), binary data, sets, `.inf`, `.nan`, and an
  alias that contains itself are rejected, since JSON cannot hold them. Quote
  a date when a string is meant.
- Unquoted `yes`, `no`, `on`, and `off` are booleans (`true`, `false`). Quote
  them when a string is meant (`'no'`). The same goes for
  keys: `on:` is the boolean `true`, not a string, and is rejected.
- Numbers read as JSON would read them: `1e-3` and `5e0` are numbers. YAML
  1.1's other readings are rejected rather than guessed: a leading zero
  (`010`, octal in YAML 1.1) and a colon (`1:30`, base 60). Write the number
  in decimal, or quote it when a string is meant.
- A value that cannot be read (`!!float abc`) or nesting too deep to read is
  rejected too.
- The keys a merge (`<<`) brings in follow the same rules, though the mapping
  itself may override them.

Where each format is accepted:

| Used by | YAML (`.yaml`, `.yml`) | JSON (`.json`) |
|---------|------------------------|----------------|
| A job's `config_file` | Yes | No: rejected, naming the YAML file with the same name if there is one |
| `generate --config-file` | Yes, passed inline with `--config-json`, any `--config-json` merged on top | Yes, passed to `draw-things-cli` as a file |
| `validate-config` | Yes | Yes |

Check a file before use:

```bash
uv run dtc validate-config dt-config/image-to-video-wan-2-2.example.yaml
```

The files in `dt-config/`, YAML and JSON alike, are yours: this tool reads
them and never changes them. The JSON files from before YAML support are left
as they are; each has a YAML copy with the same name (`.yaml`) for jobs to use.

## Jobs: chained runs

A job is a YAML file in `data/` describing a chain of generations. It runs
`run_count` times, and every run starts from the previous run's output (the
last frame, for video). Each run uses one of your named prompt pairs.

The workflow is always the same three steps:

```bash
uv run dtc validate-job data/example-job.yaml
uv run dtc run-job data/example-job.yaml --dry-run
uv run dtc run-job data/example-job.yaml
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
| `run_count` | Total number of runs |
| `prompt_pairs` | Named positive/negative prompts; see below |
| `config_file` | A YAML file name in `dt-config/`, the [base configuration](#base-configurations) |
| `config_override` | Settings applied to every run on top of `config_file` |
| `output` | `directory` and `extension` (`mov` or `mp4` for video) |
| `run_timeout_seconds` | Limit for each run |
| `cooldown` | The wait after each successful run except the last; replaces the global `cooldown` as a whole; see [Cooldown](#cooldown) |
| `desired_input_width`, `desired_input_height` | Resize the first input; see below |
| `max_input_crop_percent` | With one desired size, refuse a larger crop (default 10) |

### Prompt pairs

Each pair has a `name`, a `positive` prompt, an optional `negative` prompt,
and either `runs: [1, 3, 5]` (the runs that use it) or `default: true`
(every run no other pair lists). A pair with no `negative` leaves Draw
Things' recommended one in effect.

Job files written before the rename used `batch_count` and `batches`; they now
fail validation with a message naming the new key (`run_count`, `runs`).

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
[input resize milestone](archive/phase-1/milestone-03-input-image-resize.md).

### Cooldown

Long chains, especially video, can overheat the machine. The `cooldown`
mapping makes a job wait after each successful run except the last, in one of
three modes:

```yaml
# auto: a share of the run that just finished, kept between two bounds.
cooldown:
  mode: auto
  ratio: 0.5               # optional, default 0.5
  minimum_seconds: 300     # optional, default 0
  maximum_seconds: 1800    # optional, default 3600
```

```yaml
# manual: the same fixed wait after every run.
cooldown:
  mode: manual
  seconds: 900
```

```yaml
# off: no wait.
cooldown:
  mode: off
```

| Key | Modes | Meaning |
|-----|-------|---------|
| `mode` | all | `auto`, `manual`, or `off`; required |
| `ratio` | `auto` | The share of the last run's time to wait, above 0 and up to 1 (default 0.5) |
| `minimum_seconds` | `auto` | The shortest wait, 0 to 3600 (default 0) |
| `maximum_seconds` | `auto` | The longest wait, 0 to 3600 (default 3600); not below `minimum_seconds` |
| `seconds` | `manual` | The wait, 0 to 3600; required. `0` means no wait |

- **The auto wait** after a run that took T seconds is
  `min(maximum_seconds, max(minimum_seconds, ceil(T × ratio)))`. T is the
  time `draw-things-cli` ran, the `seconds` the manifest records for the run;
  extracting the last frame and tagging colors are not counted. The share is
  rounded up to a whole second, so a 1201-second run waits 601 seconds at the
  default ratio. A wait set by a bound says so in the log and the TUI, for
  example `5 min (the minimum; half of run 1's 3 min is less)`.
- **Where it comes from:** a job's `cooldown` replaces the global one as a
  whole; keys are not merged across the two files. With neither set, `auto`
  applies with its defaults (half of each run, 0 s to 1 h). `validate-job`,
  `run-job --dry-run`, and `/run` show the mode and its source (`job`,
  `global_config`, or `default`); for `auto` they show the most the waits can
  add up to.
- **Validation is strict:** a missing or unknown `mode`, a key of another
  mode (`seconds` with `auto`, anything but `mode` with `off`), an unknown
  key, a value out of range, or a non-number fails with exit code 2 and names
  the key. An unquoted `mode: off`, which YAML reads as `false`, is accepted.
- **The old key:** `cooldown_seconds` fails in both files with a message
  giving the mapping for the same wait, for example
  `write cooldown: {mode: manual, seconds: 1200} for the same wait` (or
  `{mode: off}` for 0).

No wait follows the last run or a failed run. Ctrl-C (or `/cancel` in the TUI)
ends a wait at once and stops the job.

## Where outputs go

- Files go to `<output_directory>/<name>/`, or `output.directory` under the
  global `output_directory` if the job sets it.
- Names are `<name>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`. Draw Things writes video
  with no color tags, so each finished video in a job is tagged in place with a
  `colr` box (BT.709 primaries, sRGB transfer, BT.709 matrix); only that box is
  added, and the frames and timing stay byte-identical. A video that already
  has color tags, or that cannot be tagged, is left as it is (with a warning in
  the second case). Video runs also save
  `<name>-…-last-frame.png`, taken from the final second of the video with
  `ffmpeg` and labeled sRGB (`sRGB`, `cHRM`, and `gAMA` chunks). Draw Things'
  current H.264 videos carry no color tags, so the frame is decoded as BT.709
  limited range, which is how Draw Things encodes them; a video that does state
  its matrix is decoded with it. The label itself changes no pixel. Nothing is ever overwritten.
- With `write_job_records: true`, each `run-job` also writes
  `<name>-<timestamp>-job.json` (a manifest of every run: prompts, seed,
  files, command, exit code, timing) and `<name>-<timestamp>-job.log`.

## Browse and run jobs in the terminal UI

`dtc tui` shows your jobs, runs one while you watch, and shows what ran
before, all on one screen in a dark theme. You drive it by typing commands
that begin with `/`:

```bash
uv run dtc tui
uv run dtc tui --data-dir /path/to/jobs --executable /path/to/draw-things-cli --shutdown-grace 10
```

The screen, top to bottom:

- **draw-things-cli** (top left, 15 lines): the run line (the running run's
  number, elapsed time, step progress, and output file, or the cooldown
  countdown), then the last 2000 lines `draw-things-cli` printed. stderr is
  in red, and progress-bar lines update the run line instead of adding
  lines.
- **Messages** (below it, like the draw-things-cli pane and the status line
  on black): the output of each command and a readable log of
  the running job: when it started, each run's start (with its command,
  credentials redacted) and result, cooldowns, and the job's result with its
  manifest and log paths. It keeps the last 5000 lines.
- **History** (the right third): every execution recorded in the state
  store, newest first: its ID, job name, status, start time, and runs
  succeeded of total.
- **The command line**, between two lines, showing `> ` and a white block
  cursor, then the **status line**: the running or last job, the data
  directory, and a reminder of `/help` and Ctrl-C.

On a terminal shorter than 30 lines, the draw-things-cli pane shrinks (down
to 7 lines) to leave Messages some room. The smallest usable size is 80x24.

| Command | Action |
|---------|--------|
| `/help` | List the commands and keys |
| `/jobs` | List the job files and whether each is valid |
| `/job JOB` | The summary, prompt pairs, and dry-run plan of a job |
| `/run JOB` | Read the job again, confirm, and run it |
| `/stop` | Stop the running job, after confirmation |
| `/history` | Read the history again |
| `/execution ID` | The detail of one execution |
| `/filter status STATUS` | Show only `succeeded`, `failed`, `interrupted`, or `running` executions |
| `/filter name TEXT` | Show only executions whose job name or job file name contains `TEXT` |
| `/filter off` | Remove both filters |
| `/reveal ID [RUN]` | Show a run's output in Finder (default: the last run with an output) |
| `/clear` | Clear the messages |
| `/quit` | Quit; while a job runs, asks to stop it first |

- `JOB` is a file name in the data directory (`walk.yaml`), or the name
  without its suffix when only one file has it. Quote a name with spaces,
  or escape them: `/run '[b] walk.yaml'` or `/run my\ job.yaml`. Tab
  completes such names in the style you started. A line that does not begin with `/` runs nothing.
- The data directory (default: `data/` in the project, whatever the working
  directory) holds the jobs: each `*.yaml` and `*.yml` file (any letter case)
  directly in it, sorted by name. `/jobs` shows each one's name, mode, and
  run count, or the first error of an invalid one. Sub-directories,
  dot-directories, and dotfiles are ignored. `/jobs`, `/job`, and `/run`
  read the files again each time, so edit a job in your editor and run the
  command again.
- `/job` prints what `validate-job` and `run-job --dry-run` print. For a job
  that sets no seed, the plan uses the placeholder seed `0`, so it is the
  same each time; a run draws a real seed. If `draw-things-cli` or `ffmpeg`
  is missing, the plan says why and the rest still shows.
- `--executable` is the `draw-things-cli` the plan names and a run uses, and
  `--shutdown-grace` is how long a stopped run may take before it is killed
  (default 10 seconds), as for `run-job`.
- `/run` asks to confirm, showing the job, mode, runs, cooldown, seed,
  output directory, and executable. Only `y` runs it; Enter does not, so a
  second Enter after `/run` cannot start a job by accident. `n` or Escape
  cancels. The job then runs as `run-job` would run it: it takes the run
  lock, is recorded in the execution history, and writes its manifest and
  log when `write_job_records` is true. If another run holds the lock, or
  the job cannot start (for example, `draw-things-cli` is missing), Messages
  says why and nothing runs. One job runs at a time.
- `/stop` stops the job after you confirm, as Ctrl-C stops `run-job`: the
  run and any cooldown end, no later run starts, and the job is
  `interrupted` with exit code 130.
- The history pane is read when the TUI starts, on `/history`, when a filter
  changes, and as the TUI's own job progresses. While another process runs a
  job (for example, `run-job` in another terminal), it is checked every 5
  seconds, so that job appears and updates whatever the filter. When no process holds the run lock, an execution left `running`
  by a crash is shown as `interrupted`. More rows load as you move to the
  last one.
- `/execution ID`, or Enter on a history row, prints the execution as it
  ran, from the stored record rather than the current job file: its
  settings, and each run's prompts, input, output, last frame, seconds,
  exit code, and command. A file that no longer exists is marked
  `(missing)`. Executions brought in by `import-history` are marked
  `imported`.
- `/reveal` runs `open -R` on the output (macOS). If the file is missing,
  or `open` fails, Messages says so.

| Key | Action |
|-----|--------|
| Enter | Run the command; in the history, show the selected execution |
| Tab | Complete a command, job file name, or filter word; with nothing to complete, move to the history |
| Up/Down | Recall the commands typed in this session; in the history, move |
| Escape | Clear the command line; in the history, go back to the command line |
| Ctrl-C | Clear the command line; on an empty line, press twice within 2 seconds to quit |

- Ctrl-C twice while a job runs asks whether to stop the job and quit; the
  TUI quits only once the job has stopped. Ctrl-C does nothing while a
  confirmation is open. If `dtc tui` receives `SIGTERM`, `SIGHUP` (the
  terminal closed), or `SIGINT`, it stops the job the same way and exits
  with 128+N (143 for `SIGTERM`). A signal while the job is already stopping
  changes nothing.
- Browsing only reads files. Running a job writes what `run-job` writes (its
  outputs and last frames, its manifest and log when `write_job_records` is
  true, `state/dtc.db`, and `state/run.lock`) and nothing else. The TUI never
  changes a job file, `dt-config/`, or the global configuration, and it
  does not create `state/dtc.db` just to show an empty history.
- An invalid global configuration is reported before the TUI starts, and the
  command exits with 2. If the TUI itself fails, it prints the error and the
  command exits with 1.

## Execution history and the run lock

Every `run-job` (not `--dry-run`) is recorded in a SQLite database,
`state/dtc.db` in the project, whatever `write_job_records` says: the job
file's exact text, the settings it ran with, and each run's prompts, files,
redacted command, timing, and result. `state/` is git-ignored, created on first
use, and readable only by you. `generate` is not recorded.

- History older than `history_retention_days` (default 14) is pruned whenever a
  command opens the database. Only database rows are removed, never outputs,
  manifests, or logs.
- If a run is killed, its record stays `running` until the next run starts,
  which closes it as `interrupted`.
- To bring in manifests written before the database existed (jobs run with
  `write_job_records: true`), run `uv run dtc import-history`. It searches the
  output directory, or `--directory PATH`, for `*.json` manifests, skips any
  already imported or past the retention period, and reports how many it
  imported, skipped, and could not read. It is safe to repeat and never changes
  a manifest.

Only one run drives the GPU at a time. `run-job` and `generate` take a lock
(`state/run.lock`) after validating their input and hold it until they finish,
cooldowns included. If another run holds it, the command exits with 75 and
says who does, and nothing starts:

```text
Another run is in progress (run-job, PID 4123). Try again when it finishes.
```

The operating system releases the lock when its holder exits, however it
exits. If `dtc` itself was killed with `SIGKILL` while `draw-things-cli` was
running, the next start also refuses (exit 75) until that `draw-things-cli`
ends, and never stops it for you. `--dry-run`, `validate-job`,
`validate-config`, and `import-history` never take the lock. If `state/` cannot
be used (unwritable, a filesystem without SQLite WAL support, or a database
written by a newer version), `run-job` exits with 1 and the reason, and starts
nothing.

## Stopping, failures, and exit codes

Press Ctrl-C to stop. The current run is asked to stop, then forced after the
shutdown grace period. A failed, timed-out, or interrupted run stops the job,
keeps any partial output, and exits with that run's exit code.

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | A run exited with 0 but wrote no output, last-frame extraction failed, or the `state/` database or lock cannot be used |
| 2 | Invalid input: options, configuration, or job file |
| 75 | Another run holds the run lock; try again when it finishes |
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
