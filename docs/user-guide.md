# User Guide

How to use the `draw-things-control` command line, whose mission is long-horizon
video generation by autoregressive image-to-video chaining on top of the Draw
Things app. Jobs ([Jobs: chained runs](#jobs-chained-runs)) are the main way to
do that; `generate` covers a single image or video. Every command is run from
the project root as `uv run dtc <command>`.

## Contents

- [Before you start](#before-you-start)
- [Commands at a glance](#commands-at-a-glance)
- [Generate one image or video](#generate-one-image-or-video)
- [Check a configuration file](#check-a-configuration-file)
- [Jobs: chained runs](#jobs-chained-runs)
- [Job file reference](#job-file-reference)
- [Where outputs go](#where-outputs-go)
- [Browse and run jobs in the terminal UI](#browse-and-run-jobs-in-the-terminal-ui)
- [Execution history and the run lock](#execution-history-and-the-run-lock)
- [Server: HTTP API and gRPC monitoring](#server-http-api-and-grpc-monitoring)
- [Park a job and hold the queue](#park-a-job-and-hold-the-queue)
- [Stopping, failures, and exit codes](#stopping-failures-and-exit-codes)
- [Troubleshooting](#troubleshooting)

## Before you start

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), and the
[Draw Things CLI](https://github.com/drawthingsai/draw-things-community)
installed locally. Video jobs also need `ffmpeg` on your `PATH` to extract
last frames, and `ffprobe` (installed with `ffmpeg`, beside it or on your
`PATH`) to measure each video's actual size and frame count; a video job
without it does not start.

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
| `validate-config FILE` | Check a Draw Things YAML configuration |
| `validate-job FILE` | Check a job file; runs nothing |
| `run-job FILE` | Run every generation in a job, chained |
| `import-history` | Import phase 1 job manifests into the execution history |
| `tui` | Browse, run, and watch jobs in a terminal UI |
| `serve` | Run the HTTP API and gRPC monitoring service for agents and other programs |
| `queue` | Submit, list, cancel, resume, park, and hold queue entries through `dtc serve` |
| `history delete` | Delete executions from the history through `dtc serve` |

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
  --config-file data/params/image-to-video-wan-2-2.example.yaml \
  --image /path/to/source.png \
  --output /path/to/output.mov \
  --timeout 3600
```

A `.mov` output is ProRes 4444 (`--video-format prores4444`) unless
`--video-format` says otherwise; an `.mp4` output is passed on as asked, which
is H.264 when `--video-format` is not given. `generate` neither tags its output
nor extracts a last frame.

Always preview first with `--dry-run`: it prints the exact command, with
credentials redacted, and starts nothing.

Common options:

| Option | Meaning |
|--------|---------|
| `-m`, `--model` | Model file; may come from the configuration instead. An explicit `--model` wins |
| `-p`, `--prompt`, `--negative-prompt` | Prompt text |
| `--prompt-file`, `--negative-prompt-file` | Read from a file, or `-` for stdin (only one may use stdin) |
| `--config-file` (alias `--config`) | YAML configuration file, passed to `draw-things-cli` inline with `--config-json`; see [base configurations](#base-configurations). None is used by default |
| `--image` | Reference image; repeat for several, in order |
| `--steps`, `--cfg`, `--width`, `--height`, `--frames`, `--strength`, `-s/--seed` | Generation settings; left out, Draw Things picks its recommended values |
| `-o`, `--output` | Output file. Without it, the image previews in the terminal |
| `--remote`, `--cloud-compute` and their related options | Choose remote or cloud generation instead of local |
| `--timeout SECONDS` | Stop the run if it takes longer |
| `--shutdown-grace SECONDS` | Wait this long after asking to stop before forcing it (default 10) |

## Base configurations

A base configuration holds Draw Things settings (`model`, `steps`,
`sharpness`, and so on, under Draw Things' own key names). Write them as YAML
files in `data/params/`, one mapping of keys to values:

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

Every command takes YAML only: a job's `config_file`, `generate --config-file`
(any `--config-json` is merged on top), and `validate-config`. A `.json` file is
rejected (exit code 2), naming the YAML file with the same name if there is
one beside it, and is never changed. Job files and the global configuration
are read by the same strict rules.

Check a file before use:

```bash
uv run dtc validate-config data/params/image-to-video-wan-2-2.example.yaml
```

The files in `data/params/`, YAML and JSON alike, are yours: this tool reads
them and never changes them. The JSON files from before YAML support are left
as they are; each has a YAML copy with the same name (`.yaml`) to use instead.

## Jobs: chained runs

A job is a YAML file in `data/jobs/` describing a chain of generations. It runs
`run_count` times, and every run starts from the previous run's output (the
last frame, for video). Each run uses one of your named prompt pairs.

The workflow is always the same three steps:

```bash
uv run dtc validate-job data/jobs/example-job.yaml
uv run dtc run-job data/jobs/example-job.yaml --dry-run
uv run dtc run-job data/jobs/example-job.yaml
```

1. `validate-job` reports any problem, naming the field, and shows the
   settings the job resolves to, including the cooldown and where it came
   from.
2. `run-job --dry-run` validates again and prints every command without
   running anything.
3. `run-job` runs the chain.

Start from `data/jobs/example-job.yaml`, which is commented line by line. Invalid
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
| `config_file` | A YAML file name in `data/params/`, the [base configuration](#base-configurations) |
| `config_override` | Settings applied to every run on top of `config_file` |
| `output` | `directory`, `extension` (`mov` or `mp4` for video), and, in video jobs, `video_format`: `prores4444` (the default), `prores422hq`, `h264`, or `hevc`, passed to every run as `--video-format`. ProRes needs `mov`, so an `mp4` job names `h264` or `hevc`. ProRes 4444 keeps 4:4:4 chroma at 12 bits, where H.264 is 4:2:0 at 8 bits, and its files are about 7 times larger per pixel (an 81-frame 448x576 clip is about 28 MB); set `video_format: h264` for small files |
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
  `run-job --dry-run`, and `/apply` show the mode and its source (`job`,
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
- Names are `<name>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`. Nothing is ever overwritten.
- A video is read and tagged by its matrix: the one its pixels were encoded
  with, measured from the first 5 frames when they tell (ProRes 4444), else the
  one its stream states (its first frame's, read before anything is added).
  Draw Things' ProRes states BT.601 for smaller frames (832x448, 448x576) but
  is always encoded BT.709, so its header alone cannot be trusted. Draw Things writes
  no `colr` box, so each finished video in a job is tagged in place with one:
  BT.709 primaries, the sRGB transfer, and that matrix
  (BT.709 when nothing says, as Draw Things' H.264 does; in an `mp4`, the
  range too). Only that box is added; the frames and timing stay
  byte-identical. A video that already has a `colr` box, that states a matrix
  this cannot tag, or that cannot be tagged, is left as it is (with a warning
  in the last two cases).
- Video runs also save `<name>-…-last-frame.png`, taken from the final second
  of the video with `ffmpeg`, decoded with the same matrix and the range the
  stream states (BT.709 limited range when nothing says, which is how Draw
  Things encodes its untagged H.264), as RGB without alpha (Draw Things reads alpha
  as a mask), and labeled sRGB (`sRGB`, `cHRM`, and `gAMA` chunks; the label
  changes no pixel). It is the handoff the next run reads, 16-bit RGB from
  every format: each sample holds one 8-bit value `v` as `v * 256 + 128`,
  because `draw-things-cli` reads a 16-bit PNG by its high byte and so reads
  exactly `v` (a viewer that divides by 65535 sees it within half a level).
  `draw-things-cli` truncates every value it writes, so a decoded level stands
  for that level to the next: half a level is added back (none at black, so
  black stays black), and each sample is rounded against a 2x2 ordered-dither
  pattern, so a flat area holds `v` and `v + 1` in equal parts instead of
  being half a level off ([research note](research/color-drift.md#the-handoff)).
  The output check below says when the pixels overrule the stream.
- With `write_job_records: true`, each `run-job` also writes
  `<name>-<timestamp>-job.json` (a manifest of every run: prompts, seed,
  files, command, exit code, timing) and `<name>-<timestamp>-job.log`.
- After each successful run, its output is measured: a video's displayed
  width, height, and frame count with `ffprobe`, and a PNG's width and height
  from its header. Draw Things may round a requested size to a multiple of
  64, or crop it, and a video model may change the frame count, so these are
  what the file holds, not what was asked for. They are kept in the
  execution history and, as `output_width`, `output_height`, and
  `output_frames`, in each manifest run. A file that cannot be measured is a
  warning, and the run still succeeds.

- A video job checks each file it passes through and says what the file
  holds, in a line of its own: on `dtc serve`'s console, in the job log, in
  the TUI's Messages (a warning in yellow), and as a `media_checked` event on
  the gRPC event stream. Each run's checks are also kept in the execution
  history: under each run in the TUI's execution detail, and as `checks`
  (`stage`, `file`, `summary`, `verdict`, `notes`, `facts`, `at`) in each run
  of `GET /v1/executions/{id}`. The checks of a run that never started (a stop
  during the input checks) are shown under `Before run N (never started)` in
  the TUI, and as the execution's own `checks` in the API, each with its `run`.
  A check that cannot be stored (a locked database, say) is skipped with one
  error in the log, and the rest of the execution is still recorded. `ffmpeg`
  and `ffprobe` are looked up at each check, so a `dtc serve` started before
  they were installed finds them without a restart. A check only reads the file; a warning never
  changes or fails a run (owner decision). A `t2v` job has no input, so it
  gets the last two. The four checks:

  | Check | When | What it reads, and what makes a warning |
  |-------|------|-----------------------------------------|
  | `Input check` | Before run 1 of an `i2v` job | Format, size, bit depth, alpha, ICC profile, EXIF orientation, a PNG's `sRGB`/`gAMA`/`cHRM` chunks. A warning for CMYK with no profile, for a PNG that states its color with `gAMA` or `cHRM` only (not read), or, when the job makes no resized copy, for anything but upright 8-bit sRGB RGB, which `draw-things-cli` then gets as it is (it reads only the high byte of a 16-bit sample). On a resume, the last frame the chain continues from is checked as the handoff it is: a warning only when it is not an RGB PNG or has alpha, at 8 or 16 bits |
  | `Resized input check` | After the copy is made, when the job resizes | The copy as above, and what the resize did (`converted from Display P3 to sRGB`, transparency flattened, turned upright). Its mean color is compared with the source's, in the space the resize worked in, over the picture only (`mean color kept within 0.4 levels`, or `moved 3.2 levels`); a warning when it moved more than 1 level, when the copy is not 8-bit RGB PNG, or not the planned size |
  | `Output check` | After each run, around the color tagging | The codec (against `output.video_format`), size, frames, pixel format, the matrix, range, primaries, and transfer the stream itself states (read from its first frame before tagging), the `colr` box after tagging, and the matrix and range the video is decoded with. For ProRes 4444 (4:4:4 at 10 bits or more), the first 5 frames are measured to tell which matrix the pixels were really encoded with ([research note](research/prores-color-matrix.md)). A warning for a different codec, a missing `colr` box, a box whose matrix differs from the stream's, a stated matrix the pixels contradict, or pixels with no 8-bit structure (noise) |
  | `Last frame check` | After the last frame is extracted | Format, size, bit depth, alpha, color chunks, and the matrix and range it was decoded from. A warning for alpha (Draw Things reads it as a mask), a missing sRGB label, a size that differs from the video's, or a depth other than 16-bit |

  For example, a ProRes 4444 run of a Display P3 photo:

  ```
  Input check (run 1): /inputs/a1-0000.jpg: JPEG 959x1280, 8-bit RGB, ICC Display P3, no alpha: ok. The resized copy converts it from Display P3 to sRGB.
  Resized input check (run 1): a1-0000-576x768.png: PNG 576x768, 8-bit RGB, no color profile (read as sRGB), no alpha; converted from Display P3 to sRGB; letterbox to 576x768; mean color kept within 0.0 levels: ok
  Output check (run 1): v-i8x-20260930-093737-5745.mov: ProRes 4444 (ap4h), 576x768, 17 frames, yuva444p12le; stream states matrix bt709, range tv, primaries -, transfer -; colr nclc 1/13/1 (BT.709/sRGB/BT.709); pixels encoded bt709 (bt709 0.217, bt470bg 0.242, bt2020nc 0.258); decoded as bt709, limited range, as its stream states: ok
  Last frame check (run 1): v-i8x-20260930-093737-5745-last-frame.png: PNG 576x768, 16-bit RGB, sRGB, cHRM, gAMA chunks, no alpha; decoded from bt709, limited range, as its stream states: ok
  ```

## Browse and run jobs in the terminal UI

`dtc tui` shows your jobs, runs one while you watch, and shows what ran
before, all on one screen in a dark theme. You drive it by typing commands
that begin with `/`:

```bash
uv run dtc tui
uv run dtc tui --data-dir /path/to/jobs --executable /path/to/draw-things-cli --shutdown-grace 10
```

The screen, top to bottom:

- **Status** (top left, 7 lines): the job at a glance (see
  [The Status widget](#the-status-widget)). Empty until a job runs.
- **draw-things-cli** (below it, 15 lines): the run line (the running run's
  number, elapsed time, step progress, and output file, or the cooldown
  countdown), then the last 2000 lines `draw-things-cli` printed. stderr is
  in red, and progress-bar lines (`Sampling... 4 / 40 [ ] 10  %`) are shown
  as lines too, as well as updating the run line. `/verbose` changes how
  much of this is shown (see [Verbose levels](#verbose-levels)).
- **Messages** (below it, like the draw-things-cli pane and the status line
  on black): the output of each command and a readable log of
  the running job: when it started, each run's start (with its command,
  credentials redacted) and result, cooldowns, and the job's result with its
  manifest and log paths. It keeps the last 5000 lines. A blank line comes
  before each command you type, before and after each message longer than
  one line (a table, a detail, prompts), and before the job's result; never
  two in a row, and none at the top.
- **Job Definition** (top of the right third, 8 rows): the job files in the
  data directory (see [Job Definition](#job-definition)).
- **Execution History** (below it, 5 rows): every execution recorded in
  the state store, newest first: its execution ID (`E0012`), job name,
  status, start time, and runs succeeded of total. On a narrow terminal it
  scrolls sideways, and its scrollbar gets a line of its own, as the Job
  Definition widget's does.
- **Execution** (the rest of the right third): the execution under the
  history cursor (see [The execution detail](#the-execution-detail)).
- **The command line**, between two lines, showing `> ` and a white block
  cursor, then the **status line**: the running or last job, the data
  directory, and a reminder of `/help` and Ctrl-C.

On a terminal shorter than 32 lines, the draw-things-cli pane shrinks (down
to 7 lines) to leave Messages at least 6. The smallest usable size is 80x34,
so the Execution widget keeps about 9 lines under the other two.

| Command | Action |
|---------|--------|
| `/help` | List the commands and keys |
| `/get jobs` | List the job files and whether each is valid |
| `/describe job <Job ID>` | The summary, prompt pairs, and dry-run plan of a job |
| `/sort jobs KEY [asc\|desc]` | Sort the Job Definition widget by `id`, `name`, `changed`, `mode`, or `runs` |
| `/apply <Job ID>` | Read the job again, confirm, and run it |
| `/stop` | Cancel the queue entry the draw-things-cli pane is following (no confirmation) |
| `/queue park <Queue ID>`, `/queue unpark <Queue ID>` | Park a running entry, or withdraw its park reservation (see [Park a job and hold the queue](#park-a-job-and-hold-the-queue)) |
| `/queue hold`, `/queue release` | Hold the queue, or release it |
| `/park`, `/unpark` | `/queue park` and `/queue unpark` on the entry the draw-things-cli pane is following |
| `/hold`, `/release` | The same as `/queue hold` and `/queue release` |
| `/get history` | Read the history again |
| `/get prompts <Execution ID> [RUN]` | An execution's positive and negative prompts: each prompt pair once with the runs that used it, or one run's |
| `/get positive <Execution ID> [RUN]` | Its positive prompts only |
| `/get negative <Execution ID> [RUN]` | Its negative prompts only |
| `/get param <Execution ID> [RUN]` (or `/get parameters`) | A run's `draw-things-cli` arguments without the prompts, as a table (default: its first run) |
| `/describe execution <Execution ID>` | The detail of one execution |
| `/describe J0001` or `/describe E0012` | The same, with the noun left out: a job <Execution ID> or an execution <Execution ID> says which |
| `/filter status STATUS` | Show only `succeeded`, `failed`, `interrupted`, `parked`, or `running` executions |
| `/filter name TEXT` | Show only executions whose job name or job file name contains `TEXT` |
| `/filter off` | Remove both filters |
| `/reveal <Execution ID> [RUN]` | Show a run's output in Finder (default: the last run with an output) |
| `/delete execution <Execution ID> [<Execution ID> ...]` | Delete these executions, asking first (see [Delete executions](#delete-executions)) |
| `/delete filtered` | Delete every execution the history's filters show, asking first; refused with no filter |
| `/delete all` | Delete the whole history, asking first |
| `/verbose [high\|medium\|low]` | Show, or set, how much output and status detail the TUI shows (see [Verbose levels](#verbose-levels)) |
| `/clear` | Clear the messages |
| `/quit` | Quit; `dtc serve` keeps running whatever it is running |

`/get prompts`, `/get positive`, and `/get negative` show each `positive:`
or `negative:` label on its own line, with the prompt starting on the next
line and a blank line around it. They also copy the prompts to the
clipboard with `pbcopy`: the prompts alone for `/get positive` or
`/get negative` (several separated by a blank line), and the labelled
prompts for `/get prompts`. A missing prompt is not copied. Without
`pbcopy`, the terminal is asked to copy them (OSC 52), which not every
terminal does.

- `<Job ID>` is a job ID (`J0001`), a file name in the data directory
  (`walk.yaml`), or the name without its suffix when only one file has it. Quote a name with spaces,
  or escape them: `/apply '[b] walk.yaml'` or `/apply my\ job.yaml`. Tab
  completes such names in the style you started. A line that does not begin with `/` runs nothing.
- The data directory (default: `data/jobs/` in the project, whatever the working
  directory) holds the jobs: each `*.yaml` and `*.yml` file (any letter case)
  directly in it, sorted by name. `/get jobs` shows each one's name, mode, and
  run count, or the first error of an invalid one. Sub-directories,
  dot-directories, and dotfiles are ignored. `/get jobs`, `/describe job`, and `/apply`
  read the files again each time, so edit a job in your editor and run the
  command again.
- `/describe job` prints what `validate-job` and `run-job --dry-run` print,
  in words: the prompt pairs laid out as `/get prompts` lays them out, and
  the plan's header, then each run's heading and its arguments as the
  `/get param` table instead of its command line. The header names the
  executable, which the table leaves out. For a job that sets no seed, the plan uses the placeholder seed `0`, so it is the
  same each time; a run draws a real seed. If `draw-things-cli` or `ffmpeg`
  is missing, the plan says why and the rest still shows.
- `--executable` is the `draw-things-cli` the plan names and a run uses, and
  `--shutdown-grace` is how long a stopped run may take before it is killed
  (default 10 seconds), as for `run-job`.
- `/apply` asks to confirm, showing the job, mode, runs, cooldown, seed,
  output directory, and executable. Only `y` runs it; Enter does not, so a
  second Enter after `/apply` cannot start a job by accident. `n` or Escape
  cancels. The job then runs as `run-job` would run it: it takes the run
  lock, is recorded in the execution history, and writes its manifest and
  log when `write_job_records` is true. If another run holds the lock, or
  the job cannot start (for example, `draw-things-cli` is missing), Messages
  says why and nothing runs. One job runs at a time.
- As each run starts, Messages shows its positive and negative prompts, laid
  out as `/get prompts` lays them out, and then its `draw-things-cli` arguments
  as the `/get param` table. The table marks the job's overrides and every
  `--config-json` value a flag replaced. The command line itself is not
  shown, so no JSON appears: a `--config-json` value is written in words.
  Each value has its own form, so two values that differ never read the
  same. Text is always in double quotes (`"wan.ckpt"`, `""`, `"true"`), null
  is `(none)`, a list is in brackets, and a mapping is in braces as
  `key=value` pairs: `[{file="a.ckpt" weight=0.5}, {file="b.ckpt" weight=1}]`,
  `[]`, `{}`. A number is written as the command line writes it, so `5.0`
  reads `5`, which is the same setting. From
  run 2 on, only what changed since the run before is shown (`Arguments as
  run 1, except:`), with `(not given)` for a row the run no longer has.
  `/execution` and `/describe job` show arguments the same way; `/get param`
  shows one run in full.
- `/stop` stops the job after you confirm, as Ctrl-C stops `run-job`: the
  run and any cooldown end, no later run starts, and the job is
  `interrupted` with exit code 130.
- The history pane is read when the TUI starts, on `/get history`, when a filter
  changes, and as the TUI's own job progresses. While another process runs a
  job (for example, `run-job` in another terminal), it is checked every 5
  seconds, so that job appears and updates whatever the filter. When no process holds the run lock, an execution left `running`
  by a crash is shown as `interrupted`. More rows load as you move to the
  last one.
- `<Execution ID>` is an execution ID (`E0012`), and `RUN` one of its run numbers. The
  letter of an ID can be typed in either case and its leading zeros left
  out: `e12` is `E0012`, and `j1` is `J0001`. A bare number (`12`) is
  refused with `Use E0012`, so an execution ID and a run number never mix
  up. Tab completes the execution IDs the Execution History widget has
  loaded. `/get` reads the stored execution, never the current job file.
- `/get param` shows the arguments the run's saved command passed, with
  credentials already redacted: first the flags (a flag that stands alone
  reads `yes`), then each `--config-json` key. The note column marks a
  value the job's configuration overrides set (`job override`), a width
  and height that `desired_input_width` or `desired_input_height` set
  (`desired input size`, which replaces any width or height override), and a flag
  that replaced a `--config-json` value, on both rows: `--width 832` says
  `replaces --config-json 1000`, and `width 1000` says
  `replaced by --width 832`.
- `/jobs`, `/job`, `/history`, `/execution`, and `/run` are not commands; typing one
  says it is unknown (`/get jobs`, `/describe job`, `/get history`,
  `/describe execution`, and `/apply` are the commands).
- `/describe execution <Execution ID>` prints the execution as it ran, from the stored record
  rather than the current job file: its settings (with the refiner, CFG,
  and shift from the saved command), and each run's prompts, steps,
  measured size and frames, input, output, last frame, seconds, exit code,
  and arguments. The prompts are laid out as `/get prompts` lays them out,
  and the arguments are the `/get param` table, not the command line. It lists every run, failed ones too. A file that no longer
  exists is marked `(missing)`. Executions brought in by `import-history`
  are marked `imported`.
- `/reveal` runs `open -R` on the output (macOS). If the file is missing,
  or `open` fails, Messages says so.

### Verbose levels

`/verbose high|medium|low` (any letter case) trades the draw-things-cli pane's detail for less noise and less
traffic from `dtc serve`; `/verbose` alone says the current level. The level is kept in
`config/tui-preferences.yaml` (a per-machine file, ignored by git) and is the level of the next session too; a
missing or unreadable file means `high`. If the file cannot be written, the level still applies to this session and
Messages says it could not be saved.

| Level | The draw-things-cli pane | Status widget, run line, status line |
|-------|--------------------------|--------------------------------------|
| `high` (default) | Every output line, live | Refreshed every second |
| `medium` | Every line is streamed, but the pane shows only each run's first minute (`(output hidden: verbose medium, after 1 min)` marks the end, once per run) | Every second |
| `low` | No output: `dtc serve` is asked not to send it, and the pane says `(output hidden: verbose low; status updates once a minute)` | Once a minute, and at once when a run starts or ends, a cooldown starts, the job ends, a stop is confirmed, or a Ctrl-C prompt appears; neither the run line nor the Status widget shows step progress |

- Typing `/verbose medium` while a run is going opens a fresh minute at that moment, so output shows at once. A line
  medium or low hides is never shown later, and `high` shows only the lines that arrive after the switch.
- Switching into or out of `low` reconnects the TUI to `dtc serve` (Messages says so). Going back out of low may
  replay the output that arrived meanwhile as one burst, or, after a very chatty run, reread the running job's state
  and show `(earlier output not shown)`. `high` and `medium` differ only in what the pane shows, so switching between
  them reconnects nothing. Without a running server the level still applies and takes effect on the next connection.
- The Queue widget is not affected by the level and stays live.

### The Status widget

| Line | What it shows |
|------|---------------|
| 1 | The phase (`starting`, `running`, `cooling down`, `stopping`, `parking after run 3/7`, `parking on its last run`, `finished (succeeded)`, `did not start`), the execution ID once the job is recorded, the job file's name without `.yaml`, and `run k/N`: `running  E0012: walk  run 2/3` |
| 2 | `Job`: a bar, its percentage, and `ends ~16:42 (in 23 min)`, or `parking` while the entry has a park reservation |
| 3 | `Run`: the same for the running run; during a cooldown, a `Wait` bar with the time the wait ends |
| 4 | The step counter (`step 28/40`) and the run's elapsed time; during a cooldown, `next: run 3/5`; after the job, `job took 1 h 12 min` |
| 5 | `last run took 7 min 12 s`: the job's last successful run, its `draw-things-cli` time without the cooldown |

- A run is estimated from its first moment. Until `draw-things-cli`
  reports a step, the estimate is a past run's time less the time so far:
  the job's own last successful run, or before it has one, the latest
  successful run of any job. At the first step report it becomes that past
  run's time per step times the steps left, when the past run counted the
  same number of steps (otherwise the past run's time less the time so far
  stands), and from the second report on
  the live rate: the time since the first step divided by the steps since,
  so loading the model is not counted. Without any past run, or once a run
  has taken longer than the past one, it says `estimating` until the rate
  exists. After the last step the run says `finishing` (the decode and
  last-frame extraction have no counter).
- The job estimate times each run still to come by the average full time
  of this job's finished runs, and each wait by the job's `cooldown`
  setting applied to their `draw-things-cli` time. On run 1 it starts from
  the past run, so it has an estimate from the start; from run 2 on, it
  keeps its estimate while a run loads. The job bar is the share of the job's
  estimated time that has passed, cooldowns included.
- A time on another day starts with its date (`09-27 02:10`). On a narrow
  terminal the bars shrink and the text stays whole.
- After a stop is requested, the bars stop moving and the end times read
  `stopping`. When the job ends, the widget shows its result, the runs that
  succeeded, and how long the job took, until the next job starts.
- While another process holds the run lock (for example, `run-job` in
  another terminal), it says `A job is running in another process`, from
  the moment the TUI opens.
- While the queue is held and no job runs, it says `Queue held since 12:04
  (by Q0007)` (dated when the hold began on another day): as its first line
  with nothing run this session, and as its third under a finished job.

### Job Definition

The Job Definition widget lists the job files in the data directory, 8 rows
at a time: each one's **job ID**, its file name without the extension (two
files that differ only in their extension, such as `walk.yaml` and
`walk.yml`, keep it), when the file last changed (`09-26 14:05`, or
`2025-09-26` for an earlier year), and the job's mode and runs. An invalid
file is dim red, and its mode and runs read `invalid`; `/describe job` shows
why.

- A job ID (`J0001`) belongs to a file name in the project's `data/jobs/`
  for good. Editing the file keeps it; a renamed file gets a new one; a
  deleted file's ID comes back if the same name returns. A new name takes
  the number after the highest ever given, so an ID is never reused. A file
  gets its ID when the TUI first lists it. With `--data-dir` pointing
  elsewhere, the files are listed without IDs (`-`).
- `s` sorts by the next column and `r` reverses the order, or type
  `/sort jobs KEY [asc|desc]`. Without a direction, `changed` sorts newest
  first and the others ascending. Ties go by ID, and invalid files come
  after valid ones by mode and runs. The sort is kept across sessions; the
  default is `changed`, newest first. The widget's subtitle names it.
- Enter describes the selected job in Messages; `a` asks to run it, with
  the same confirmation as `/apply`.
- The directory is watched, so a file added, changed, renamed, or deleted
  shows within a moment, without `/get jobs` and without polling. While a
  directory cannot be watched, or an invalid job is listed, it is checked
  every 5 seconds instead. A valid job is read again only when its
  file, its input image, or its base configuration in `data/params/`
  changed, so a job turns valid or invalid at the next check when its input
  appears or goes (these files are watched too); an invalid one is read every time. `/get jobs` reads
  every file.
- A job ID works as soon as the TUI opens, before the list is shown. A name
  that is both a file's name (without its suffix) and another file's job ID,
  such as `j1` when `j1.yaml` exists and `J0001` is `walk.yaml`, runs
  nothing and says so: type the full file name (`j1.yaml`) or the ID in
  another form (`J0001`).
- Job IDs and execution IDs live in `state/dtc.db` only. Deleting `state/`
  starts both numberings over, and nothing rebuilds them, so keep it.

### The execution detail

The Execution widget shows the execution under the history cursor, a moment
after the cursor stops:

- its model, its refiner and where the refiner takes over
  (`… from 10%`), and `832x448  CFG 5  shift 3.99`, read from the saved
  `draw-things-cli` command (a flag wins over `--config-json`, as
  `draw-things-cli` applies them);
- one line per **successful** run: its number, frames, steps, and time
  (`draw-things-cli`'s own time, without the cooldown), with its output's
  file name under it. Failed, interrupted, and running runs are left out;
  `/describe execution` lists them. An execution with none says
  `No run finished successfully`.

The size and frames are the measured ones (see
[Where outputs go](#where-outputs-go)), never the size or frame count the
job asked for. `sizes vary` means its runs differ, and `size -` or `-` means
nothing was measured, as for executions recorded before measuring began.
Long names are cut in the middle with `…`; `/describe execution` shows them whole.

Click a file name, or select its run and press Enter, to show it in Finder
(`open -R`). A file that no longer exists is marked `(missing)`. When this
TUI starts a job, the history cursor moves to it, so its runs appear as
they succeed, unless you are in the history or the detail at that moment.

| Key | Action |
|-----|--------|
| Enter | Run the command; in Job Definition, describe the job; in the history, move to the detail; in the detail, show the selected run's output in Finder |
| Tab | Complete a command, job ID or file name, execution ID, or filter or sort word; with nothing to complete, move to Job Definition, then the history, then the detail |
| Up/Down | Recall the commands typed in this session; in the widgets, move |
| PageUp/PageDown, Home/End | In the widgets, move further |
| `a` | In Job Definition, ask to run the selected job |
| `s` / `r` | In Job Definition, sort by the next column / reverse the order |
| Escape | Clear the command line; in the widgets, go back to the command line |
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
  changes a job file, `data/params/`, or the global configuration, and it
  does not create `state/dtc.db` just to show an empty history. Browsing
  writes to `state/dtc.db` only to give job IDs to the files in
  `data/jobs/`, to keep the Job Definition widget's sort, and to upgrade an
  existing database to the current schema, which any `dtc` command does the
  first time it opens an older one.
- An invalid global configuration is reported before the TUI starts, and the
  command exits with 2. If the TUI itself fails, it prints the error and the
  command exits with 1.

## Execution history and the run lock

Every `run-job` (not `--dry-run`) is recorded in a SQLite database,
`state/dtc.db` in the project, whatever `write_job_records` says: the job
file's exact text, the settings it ran with, and each run's prompts, files,
redacted command, timing, and result. `state/` is git-ignored, created on first
use, and readable only by you. `generate` is not recorded.

Each execution gets an **execution ID**, `E` and at least four digits
(`E0012`), before it starts; it is named in `run-job`'s log line, in the
manifest (`execution_id`), and in the TUI. The number only goes up: a pruned
execution's number is never given again. If the database cannot give an ID,
the job does not start: `run-job` exits with 1 and writes no manifest or
log. A `--dry-run` needs no ID. The executions already recorded before
execution IDs existed were numbered by start time, oldest first.

- History older than `history_retention_days` (default 14) is pruned whenever a
  command opens the database. The rows are removed, and so is each pruned
  execution's `.log` file; outputs and manifests are never removed. A log with
  no history row, such as one from before the database existed, is left alone.
- A parked queue entry and its execution are kept past `history_retention_days`
  until a resume of it, or a resume of that resume, has a succeeded run, so a
  parked job stays resumable. The resumes in between (one cancelled, or one
  whose first run failed) are kept with it. After that they age out like any
  other.
- If a run is killed, its record stays `running` until the next run starts,
  which closes it as `interrupted`.
- To bring in manifests written before the database existed (jobs run with
  `write_job_records: true`), run `uv run dtc import-history`. It searches the
  output directory, or `--directory PATH`, for `*.json` manifests, skips any
  already imported or past the retention period, and reports how many it
  imported, skipped, and could not read. It is safe to repeat and never changes
  a manifest. Each imported execution gets the next free ID, whatever its start
  time, and is listed with it; when its manifest records another ID (its
  execution was pruned, or `state/` was deleted), both are named:
  `E0040: /path/walk-job.json (its manifest says E0003)`.

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

### Delete executions

Failed experiments, test runs, and duplicates can be deleted from the history
instead of waiting for `history_retention_days`. A deletion is final and goes
through `dtc serve`'s API, so the server must be up. It removes the
execution's row, its runs, its `.log` file, and its manifest (so
`dtc import-history` cannot bring it back). Its outputs stay, and its number
is never given again. A manifest that cannot be deleted (a permission error,
a read-only volume) is named, since `dtc import-history` would bring the
execution back under a new number; the row is deleted anyway.

Refused, with the reason:

- A running execution: `E0012 is running; it cannot be deleted`.
- An execution a queued or running entry uses: its own, the one it resumes,
  or one its resume chain reads between the two:
  `Q0007 is queued to resume from E0012`.

Anything else can be deleted, including the execution a parked, interrupted,
failed, or cancelled entry would resume from. That entry then cannot be
resumed (`Q0007's execution E0012 was pruned or deleted; it cannot be
resumed`), and you are warned first: `Q0007 can no longer be resumed`. A
parked entry left with nothing to resume is no longer kept by retention.

In the TUI, `Space` on the Execution History widget marks the selected row
(an `*` before its ID) and `d` deletes the marked rows, or the selected row
when none is marked. `/delete execution E0012 E0013`, `/delete filtered`
(every execution the current filters show, all pages), and `/delete all`
do the same from the command line. Marks follow their executions across
re-reads; a filter change or a deletion clears them. A dialog asks about
each execution, newest first:

```text
Delete E0012 (walk, failed, 3/7 runs, 2026-09-28 14:03)?  1 of 12
Its log and manifest are deleted; its outputs stay.
Q0007 can no longer be resumed.

  [Delete]  [Skip]  [Delete all]  [Cancel]
```

`y` or Enter deletes it, `s` skips it, `a` deletes it and every remaining
one without asking again, and `n` or Escape stops (what was already deleted
stays deleted). One execution offers only Delete and Cancel. Executions the
server would refuse are left out first, and Messages names each one. With the
server down, no dialog opens and Messages says it cannot be reached.

From the command line:

```bash
uv run dtc history delete E0012 E0013
uv run dtc history delete --status failed --name walk
uv run dtc history delete --all --yes
```

It lists what it will delete, with any resume each ends, and asks
`Delete 12 executions? [y/N]`; `--yes` skips the question. Without a terminal
it refuses unless `--yes` is given (exit 2). IDs, filters (`--status`,
`--name`, matching as the TUI's `/filter` does), and `--all` cannot be mixed,
and one of them is required. Answering no, or a filter that matches nothing,
deletes nothing and exits 0. An execution refused or missing is named, the
rest are deleted, and the exit code is 2.

## Server: HTTP API and gRPC monitoring

`dtc serve` runs an HTTP API and a gRPC monitoring service in one foreground
process, for an AI agent (over MCP, later) or any local program to list jobs
and inputs, queue and watch runs, and read history — without running
`draw-things-cli` itself.

```bash
uv run dtc serve
```

It reads `config/global-config.yaml` (or `--global-config PATH`) once at
start; edit and restart to pick up a change. Like `run-job`, it takes the run
lock (`state/run.lock`) and holds it for as long as it runs, so `run-job` and
the TUI cannot start a job while a server is up, and a second `dtc serve`
refuses to start (exit 75) — see
[Execution history and the run lock](#execution-history-and-the-run-lock). A
queue submitted through the API runs on the server's own worker, one entry at
a time, with the same cooldown between entries as between a job's own runs.
Stopping the server (Ctrl-C, SIGTERM) stops the run in progress at once, the
same as stopping `run-job`; a later `POST /v1/queue/{id}/resume` reruns the
run that was cut short, never continuing it midway. To keep that run, park the
entry first and stop the server once it has parked (see
[Park a job and hold the queue](#park-a-job-and-hold-the-queue)).

Options: `--host` (default `127.0.0.1`), `--port` (default `8765`),
`--grpc-port` (default `8766`), `--executable`, `--shutdown-grace`,
`--global-config`, and `--allow-remote-bind`. A `--host` that is not loopback
(`127.0.0.1`, `::1`, `localhost`) is refused (exit 2) unless
`--allow-remote-bind` is given, since beyond loopback the bearer token below
crosses the network in plain HTTP; an SSH tunnel is the safer way in from
elsewhere. With the flag, `serve` starts but warns about it. Either way, an
HTTP request whose `Host` header names neither loopback nor the bound address
is refused, so a web page cannot reach the server through DNS rebinding. A
`--port` or `--grpc-port` already in use is refused (exit 2) before the
worker starts, so nothing it may have already queued is disturbed.

**Authentication.** Every endpoint but `GET /v1/health` requires
`Authorization: Bearer <token>`; a gRPC call needs the same token in its
`authorization` metadata. The token is created on first start, 32 random
bytes as hex, in `config/server-token` (mode 0600, git-ignored); an existing
file that others can read, that another user owns, or that is empty, is
refused rather than silently fixed. To change the token, delete the file and restart the server.
The token is never logged, returned, or accepted from a query string. There
is one level of access: whoever holds the token sees every job file and every
execution in the history, whichever front end ran it.

**Endpoints**, all under `/v1` and JSON:

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | Liveness (no auth): up, its version, whether the worker is alive |
| `GET /capabilities` | Whether writes are enabled, and the limits in force |
| `GET /jobs`, `GET /jobs/{job}`, `GET /jobs/{job}/preview` | The job files, one file's text and resolved plan, and its dry-run preview |
| `GET /inputs` | Images in the input directory, with their size |
| `POST /queue`, `GET /queue`, `GET /queue/{id}` | Submit a job by reference; list entries, with the queue's hold (`held`, `held_since`, `held_by`); read one entry's state and the hold |
| `POST /queue/{id}/cancel`, `POST /queue/{id}/resume` | Cancel a queued or running entry; resume an interrupted, failed, cancelled, or parked one from its last succeeded run |
| `POST /queue/{id}/park`, `POST /queue/{id}/unpark` | Park a running entry, or withdraw its park reservation; each returns the entry as `GET /queue/{id}` does |
| `POST /queue/hold`, `POST /queue/release` | Hold or release the queue; each returns `held`, `held_since`, `held_by`, and `changed` (false when the queue already was, or was not, held) |
| `GET /executions`, `GET /executions/{id}`, `GET /executions/{id}/outputs` | Execution history, one execution's runs, and each run's output file with whether it is complete |
| `POST /executions/delete` | Delete executions: body `{"executions": ["E0012", ...], "dry_run": false}`, 1 to 200 IDs; answers `deleted`, `refused` (ID and reason), `missing`, `resumes_ended` (ID and the entries it ends), and `manifests_kept` (ID and path). With `"dry_run": true` it says what a deletion would do now and deletes nothing |
| `GET /audit` | The audit log of every submit, cancel, resume, park, unpark, hold, release, and execution deletion, refused ones included |

A `{job}` reference is a job ID (`J0001`) or a file name in `data/jobs/`, and
a queue entry or execution is named by its own ID (`Q0007`, `E0012`) — never a
raw path or a store row number. `GET /jobs`, `/executions`, `/inputs`, and
`/audit` are paged (`limit`, default and maximum 200, and an opaque `cursor`
from the previous page). `GET /executions` filters by `status`, by `job` (a
job reference), and by `name`, which matches the job name or file name as the
TUI's `/filter name` does. `GET /queue` pages the same way, but only its
finished entries (newest first); queued and running ones always come back in
full on the first page, since those alone are bounded by `max_queued_jobs`.
Each entry carries `park_requested`, true for a running entry with a park
reservation. `GET /queue/{id}` also carries `between_runs_after_run`: the
succeeded run the entry is between runs after, a cooldown included, until its
next run starts, or null while a run is going (`cooldown_until` is the wait
between two queued jobs, null while a job runs).
`worker_state` reads `held` while no job runs and the queue is held.

Watching for change (the event stream, and "tell me when this entry changes")
is gRPC, not HTTP: `WatchEvents` streams job and queue events from a
`last_event_id` onward (an ID from before the server's current run gets a
`Reset`, telling the client to re-read state over HTTP and resubscribe), and
`WatchQueueEntry` streams one entry's snapshot on every change until the
client cancels the call. Its snapshot carries `park_requested` and
`queue_held`, and `WatchEvents` carries `queue_park_changed`, `queue_held`, and
`queue_released`.

**Rules and limits.** Every job the API queues must set
`run_timeout_seconds` and keep its `input` inside `input_directory` and its
`output.directory` inside `output_directory`, and must fit the limits below,
configurable under `api_limits:` in `config/global-config.yaml` (see
`config/global-config.example.yaml` for the full block and defaults):

| Key | Meaning | Default |
|-----|---------|---------|
| `max_queued_jobs` | Entries `queued` at once | 20 |
| `max_job_runs` | Runs a submitted job may have, or a resume may have left | 100 |
| `max_job_seconds` | One entry's worst case: runs × `run_timeout_seconds`, plus the longest cooldown wait between them | 172800 (48 h) |
| `max_job_file_bytes` | Size of job text the API accepts (from Milestone 07) | 65536 |

A refusal names the `code` (`timeout_required`, `outside_directory`,
`limit_exceeded`), the field, and, for a limit, the limit and the job's
value; the job still runs with `run-job` while no server is up. These limits
apply to every caller, `dtc queue` (Milestone 03) included: the API cannot
tell a person from an agent.

**Audit log.** Every submit, cancel, resume, park, unpark, hold, and release
is recorded in the state store (time, action, target, outcome, caller; hold and
release have no target), refused ones included, and so is each execution a
`POST /executions/delete` names (`delete_execution`, one row per ID; a request
refused as a whole has one row with no target; a dry run has none), read
back with `GET /audit`. It holds no prompt text, YAML, or credential, and is
never pruned by `history_retention_days`.

## Park a job and hold the queue

Cancelling a running entry, `/stop`, and stopping the server all kill
`draw-things-cli` at once, and the run it was making is lost. To keep it,
*park* the entry instead: the job goes on until its current run ends, keeps
every run it finished, and ends `parked`. A resume continues it at the next
run, with the original seed, from that run's output (its last frame, for a
video), and reruns nothing.

```bash
uv run dtc queue park Q0007
uv run dtc queue resume Q0007
```

- Parking also *holds* the queue: nothing else starts until you release it
  (`dtc queue release`, `/queue release`, or `/release`). The hold is saved
  in the state store, so it survives a `dtc serve` restart, which logs
  `Queue held since ... ; 'dtc queue release' starts it`. A release starts
  the oldest queued entry at once, even when the last job's cooldown has not
  passed.
- While the entry's run goes on, it reads `parking`: in `dtc queue list`,
  the Queue widget's State column, and the Status widget (`parking after
  run 3/7`, or `parking on its last run`, with `parking` on the Job bar). A
  reservation made from another TUI or from `dtc queue` shows too.
- A park during the cooldown between two runs ends the cooldown at once, and
  the job parks. A park during the job's last run lets it finish
  `succeeded`, and the queue is still held. A parking run that fails ends
  the job `failed`, and the queue stays held.
- `unpark` withdraws the reservation: the job runs on as if it had never
  been made, with its cooldowns in full, and the hold the reservation made
  is released (a hold made by `hold`, or one already there, stays). It says
  `Q0007 runs on; the queue is not held`, or `...; the queue stays held`.
  Once the park has taken effect, unpark is refused (`Q0007 has already
  parked`).
- A cancel still stops at once, and a cancel of a parking entry loses its
  run. Stopping the server while an entry is parking stops the run at once
  too, and the entry ends `interrupted`; the reservation is not kept, the
  hold is.
- Parking a queued entry is refused: cancel it, or hold the queue. So is
  parking a finished entry, or one being cancelled. Parking an entry that is
  already parking does nothing, unless a release ended its hold, which it
  then makes again.
- `hold` holds the queue by itself: a running job is not stopped, and
  nothing starts after it. Submissions and resumes are still accepted and
  wait `queued`. Holding a queue a park reservation already holds makes the
  hold its own, so a later unpark no longer releases it. A saved hold that cannot be read
  counts as held, with a warning, until a release.
- A parked entry and its execution are kept past `history_retention_days`
  until a resume in its chain has a succeeded run.

| Where | Park | Withdraw | Hold | Release |
|-------|------|----------|------|---------|
| `dtc queue` | `park <Queue ID>` | `unpark <Queue ID>` | `hold` | `release` |
| The TUI | `/queue park <Queue ID>`, `/park`, `p` on the Queue widget | `/queue unpark <Queue ID>`, `/unpark`, `u` | `/queue hold`, `/hold` | `/queue release`, `/release` |

`/park` and `/unpark` act on the entry the draw-things-cli pane is
following. None of these asks to confirm. The Queue widget's title reads
`Queue (held)` while the queue is held, from the state store too while the
server is down. `dtc queue add --wait` says when its entry starts parking or
runs on, and when it waits behind a hold; an entry that parks prints
`Q0007 parked after run 5/7; 'dtc queue resume Q0007' continues at run 6`
(the chain's run number) and exits 3.

## Stopping, failures, and exit codes

Press Ctrl-C to stop. The current run is asked to stop, then forced after the
shutdown grace period. A failed, timed-out, or interrupted run stops the job,
keeps any partial output, and exits with that run's exit code.

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | A run exited with 0 but wrote no output, last-frame extraction failed, or the `state/` database or lock cannot be used |
| 2 | Invalid input: options, configuration, or job file |
| 3 | `dtc queue add --wait`: the entry parked; `dtc queue resume` continues it |
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
- **`Could not find 'ffprobe'`:** a video job needs `ffprobe` to measure its
  outputs. It comes with `ffmpeg` (`brew install ffmpeg`); put it beside
  `ffmpeg` or on your `PATH`.
- **Unsure what will run:** add `--dry-run`.
