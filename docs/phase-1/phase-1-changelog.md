# Phase 1 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 1](README.md). Newest first.

## 2026-09-25

- **Change**: Phase 1 is done. All four milestones are done and the exit
  criteria are met. Follow-on work continues in
  [Phase 2](../phase-2/README.md) (a TUI) and
  [Phase 3](../phase-3/README.md) (an API server and an MCP server).

- **Change** [M04]: Code review fixes. The cooldown's "until" time is local
  time, like the other timestamps. A signal that arrives after a full wait
  now stops the job "before run k" instead of "during the cooldown". The
  wake-up-pipe wait moved to `draw_things_runner.interruptible_wait`.
  `validate-job`, the dry run, and the log print seconds as written in the
  file (`0.00001 s`, not `1e-05 s`), and totals keep tenths (`0.4 s`, not
  `0 s`).

- **Change** [M04]: Milestone 04 is done. The global configuration and job
  files accept `cooldown_seconds` (0 to 3600); a job waits that long after
  each successful run except the last, and a signal ends the wait at once.
  `config/global-config.example.yaml` sets 900. The job manifest gains
  `cooldown_seconds` and `cooldown_source`, and run records gain
  `cooldown_after_seconds`. `validate-job` prints a `cooldown` line, and
  the `run-job --dry-run` header names the cooldown, with `# Cooldown <n> s`
  between runs.

- **Owner decision** [M04]: A signal ends the cooldown through a wake-up pipe
  (`signal.set_wakeup_fd` and `select`), so the stop is instant. Polling with
  short sleeps was offered and not chosen.
- **Design decision** [M04]: The draft's `threading.Event`, set from the
  signal handler, is dropped: `Event.wait()` holds a non-reentrant lock, so
  a signal arriving at the wrong moment would deadlock the main thread. The
  handler only sets `_interrupt`, as before, and the wait restores the
  previous wake-up fd when it ends. This supersedes the `threading.Event`
  part of the M04 design decision below.
- **Owner decision** [M04]: The manifest records the time waited on the run
  before the wait (`cooldown_after_seconds`), saved when the wait starts and
  when it ends, so a wait cut short by a signal is recorded too. The draft's
  per-run `cooldown_seconds` on the next run is dropped, since that run never
  starts when a signal ends the wait.
- **Owner decision** [M04]: The log shows a line when each cooldown starts
  and another when it ends, and the global configuration source is labeled
  `global_config`, like the seed's `config_file`. This supersedes the
  `global config` label in the M04 owner decisions below.

- **Owner decision** [M04]: The cooldown is allowed in every mode (`i2i`,
  `t2v`, `i2v`), not only `i2v`. This supersedes "`i2v` only" in the M04
  design decision below.
- **Owner decision** [M04]: The cooldown has a global default: a new
  optional `cooldown_seconds` key in the global configuration, which applies
  to every mode and which a job's `cooldown_seconds` overrides (0 turns it
  off). With neither key set, the app's default is 0 (no wait).
  `config/global-config.example.yaml` sets 900 (15 minutes). This supersedes
  "per job only" in the M04 design decision below.
- **Owner decision** [M04]: The cooldown is a fixed time, from 0 to 3600
  seconds, and `validate-job`, the dry run, the log, and the manifest show
  where the value came from (`job`, `global config`, or `default`).
- **Change** [M04]: The milestone is renamed "Cooldown between runs"
  (`milestone-04-run-cooldown.md`), since it is no longer `i2v` only.
- **Change** [M04]: Milestone 04, a cooldown between `i2v` runs, is
  planned. A new optional job key, `cooldown_seconds` (0 to 3600, default 0),
  makes the job wait after each successful run except the last. A signal
  ends the wait and stops the job. The job manifest records the setting and
  the time waited before each run.
- **Design decision** [M04]: The cooldown is a fixed wait set per job, not
  based on temperature. Reading macOS thermal state (`pmset -g therm`) was
  rejected for now: it reports throttling only after it has started, and it
  would tie the app to one platform. A global default was rejected because
  the right wait depends on the model, size, and frame count, which the job
  sets. The wait is a `threading.Event` wait, so a signal ends it at once;
  `time.sleep` would resume after the signal handler returned. Pending owner
  confirmation: `i2v` only, default 0, and per job only (see the
  milestone's open questions).
- **Change** [M03]: Review fixes. The manifest's `input_resize.fit` gains
  `scale` (scaled to exactly the target: no crop, no bars) and `rotate`
  (upright copy at the target size), so an exact fit is no longer reported
  as `letterbox` or `crop` with 0.0%; `max_crop_percent` and `crop_percent`
  are set whenever exactly one `desired_input_*` key is. Run records gain
  `resized_input`, the temporary path run 1's `command` passes as
  `--image`. An image over Pillow's pixel limit, or in a mode that cannot be
  converted, is now a validation or resize error (exit code 2) instead of a
  traceback. `read_image_size` is removed.
- **Design decision** [M03]: The input is fully decoded only when a copy is
  made, after the target and crop checks, and a real `run-job` relies on
  writing the copy to decode it, so it is decoded once. An input used as-is
  is not decoded, as without the keys. Loading a job no longer imports numpy
  or LittleCMS. An embedded profile described as sRGB is not converted.
  This supersedes the "validation fully decodes the input" rule in the
  Milestone 03 plan.

- **Design decision** [M03]: Resizing the first input aims for maximum
  fidelity without losing sharpness. Downscaling runs in linear light, in
  32-bit float, with Lanczos and 70% anti-ringing; upscaling runs in sRGB
  values, also in float; 8-bit rounding happens once. Measured against
  Pillow's 8-bit sRGB-value Lanczos: fine bright detail keeps its brightness
  (187.5 against 127.5, ideal 188), edges are as sharp (1.38 against 1.39
  px), the dark halo is no larger (7.3%), and thin highlights keep their
  light (103% against 77%). Alternatives rejected: sRGB-value resampling
  (darkens fine detail), plain linear light (33% dark halos), full
  anti-ringing or a Hamming filter (softer edges, 1.49 px), linear light for
  upscaling (softer), and unsharp masking (over-bright highlights). This
  supersedes the "resampling in sRGB values" part of the fidelity decision
  below.
- **Change** [M03]: New dependency `numpy`, for the floating-point
  resampling. Pillow alone cannot apply the sRGB transfer curve to float
  images.
- **Design decision** [M03]: The resized first input keeps its fidelity. An
  embedded ICC profile is converted to sRGB (relative colorimetric, black
  point compensation), 16-bit and 32-bit grayscale are scaled to 8 bits
  instead of clipped, the crop fit resamples the exact fractional source area
  to the target in one Lanczos pass, and an input that only needs rotating is
  copied losslessly. Alternatives rejected: dropping the profile (P3 photos
  shift color), keeping it in the PNG (`draw-things-cli` is not known to
  honor it), resizing then cropping (two roundings), and resampling in linear
  light (unusual, and slower in Pillow).
- **Change** [M03]: Milestone 03 is done. Jobs accept
  `desired_input_width`, `desired_input_height`, and
  `max_input_crop_percent`; the first input is resized (cropped or
  letterboxed, never stretched) to a temporary PNG for run 1, and every run
  generates at that size. New module `input_resize.py`; `input_size.py`
  plans the resize; the job manifest gains `input_resize`. The Milestone 01
  size mismatch message now suggests the new keys instead of saying resizing
  is planned.
- **Owner decision** [M03]: `desired_input_width` and `desired_input_height`
  are at most 8192 each, after rounding and including a derived value.
  Alternatives rejected: a pixel-count limit, and no limit (a huge value
  would exhaust memory in Pillow).
- **Owner decision** [M03]: With one `desired_input_*` key, a job is refused
  when the crop would remove more than `max_input_crop_percent` of the scaled
  image on the cropped axis. The new optional root-level key defaults to 10.
  Alternatives rejected: a warning only, no check, and a fixed limit.
- **Design decision** [M03]: `max_input_crop_percent` is a validation error
  unless exactly one `desired_input_*` key is set, because only then is the
  input cropped; this follows the rule that a setting with no effect fails
  instead of being ignored.
- **Owner decision** [M03]: An input whose EXIF orientation is not 1 always
  gets an upright temporary copy, even when its size already matches, because
  `draw-things-cli` may not apply EXIF rotation. Alternative rejected: passing
  the original file when the size matches.
- **Owner decision** [M03]: `run-job --dry-run` shows run 1's `--image` as a
  placeholder (`<photo.jpg resized to 832x448>`) when a copy will be written.
  Alternative rejected: the original path with only an INFO line.
- **Design decision** [M03]: Validation fully decodes the input, not only its
  header, and `run-job` writes the resized copy before the manifest and log
  are created, so a bad image exits with code 2 and leaves no manifest.
  `JobDefinition.size` is `None` without the keys, so existing jobs build the
  same commands as before.
- **Owner decision** [M03]: When only one `desired_input_*` key is given,
  the input's aspect ratio is kept and there are no black bars: the image is
  scaled to cover the target and the few pixels left over from rounding down
  to 64 are cropped, centered. When both keys are given, the letterbox rule
  (below) still applies. In neither case is the image stretched. This
  supersedes the letterbox-for-every-job part of the earlier fit decision.
  Alternatives rejected: letterboxing with thin bars, and choosing a smaller
  64-multiple box whose ratio is closer to the input's.
- **Change**: Added the Milestone 03 plan
  (`milestone-03-input-image-resize.md`).
- **Owner decision** [M03]: `i2v` and `i2i` jobs get two optional root-level
  keys, `desired_input_width` and `desired_input_height` (any positive
  integers). Either may be set alone; the missing one is derived from the
  input image's aspect ratio. Without either key, the Milestone 01 exact-size
  check is unchanged. This takes up the input resizing that Milestone 01
  deferred, in place of `size_from_input` and `max_pixels`.
- **Owner decision** [M03]: Each value (given or derived) is rounded down to a
  multiple of 64; a value that comes out under 64 is a validation error.
  Alternatives rejected: rounding to the nearest or up, and clamping to 64.
- **Owner decision** [M03]: The first input is letterboxed (scaled to fit,
  upscaled if needed, padded with black) to the target size. Alternatives
  rejected: center-crop, stretch, refusing a mismatched aspect ratio, and a
  configurable pad color or blurred fill.
- **Owner decision** [M03]: The target size is the generation size for every
  run. When a `desired_input_*` key is set, `width` and `height` from
  `config_override` and `config_file` are ignored, and an INFO message names
  each ignored value. Alternative rejected: a validation error when
  `config_override` also sets them.
- **Owner decision** [M03]: The keys are a validation error in `t2v` jobs,
  which have no input image.
- **Owner decision** [M03]: The resized input is a temporary PNG, deleted
  when run 1 ends or the job exits. Alternative rejected: keeping it beside
  the outputs.
- **Design decision** [M03]: A missing value is derived from the other value
  after it is rounded down, so the target is as close as possible to the
  input's aspect ratio. An input already at the target size is used as-is.
  The manifest records the resize in a job-level `input_resize` field, and
  run 1's `input` keeps the original path.

## 2026-09-24

- **Owner decision**: Code is formatted with Black, not `ruff format`; Ruff
  stays the linter. Black's `line-length` is 65535, the same as Ruff's, so
  there is in practice no maximum line length. `make format` runs Black, and
  `make check` fails if Black would reformat a file.
- **Change** [M01]: `seed` (in `config_override` or the `config_file`) must
  be at most 4294967295, because `draw-things-cli` takes a UInt32 seed.
  `validate-job` now rejects a larger seed instead of the run failing.
- **Change**: When `draw-things-cli` cannot be started (for example, it is not
  executable), the error is reported and the command exits 2, instead of
  showing a traceback and leaving the run marked `running` in the manifest.
- **Design decision** [M01]: `JobService` is the only owner of signal
  handling during `run-job`. Its runners are created with
  `handle_signals=False`, and it forwards a signal to the current run. The
  install/restore code is shared from `draw_things_runner.py`. Rejected: each
  runner swapping in its own handlers during a run, which duplicated the code
  and hid mid-run signals from the job.
- **Change**: The runner's shutdown stays bounded. After it sends SIGKILL and
  gives up, cleanup no longer starts a second SIGTERM/SIGKILL cycle, and the
  output readers share one drain deadline and are joined once.
- **Owner decision** [M01]: `dt-config/image-to-video-wan-2-2.example.json`
  is the example base configuration; it replaces
  `dt-config/image-to-video-wan-2-2.json`. The README, `data/example-job.yaml`,
  and the milestone 01 document now point to it. Earlier entries keep the old
  name.
- **Owner decision** [M01]: `run-job` no longer writes the job manifest
  (`-job.json`) or job log (`-job.log`) by default. The new optional global
  setting `write_job_records: true` turns both on. This supersedes the rule
  that every `run-job` writes them.
- **Owner decision** [M01]: `i2v` jobs ignore `batchCount` from the
  `dt-config/` base configuration, so it is not passed to `draw-things-cli`.
  When the key is present, an INFO message names the value and the file in
  `validate-job`, `run-job --dry-run`, and `run-job` (also in the job log).
  `i2i` and `t2v` jobs keep it.
- **Change**: The global configuration file is renamed from
  `config/global_config.yaml` to `config/global-config.yaml`, and its template
  from `config/global_config.example.yaml` to
  `config/global-config.example.yaml`. Earlier entries keep the old names.
- **Change** [M01]: The job file's `input` value is stripped of leading and
  trailing whitespace before it is resolved, so `input: " first-frame.png "`
  finds `first-frame.png`. A value that is only whitespace is rejected.
- **Change** [M01]: Milestone 01 is done. New `run-job` and `validate-job`
  commands; new modules `global_config.py`, `job_definition.py`,
  `job_service.py`, `generation_config.py`, `input_size.py`,
  `output_naming.py`, `frame_extraction.py`, `job_manifest.py`, and
  `job_log.py`; new dependencies `pyyaml` and `pillow`.
- **Design decision** [M01]: `JobService` builds each run's
  `DrawThingsGenerateArguments` directly from the validated job instead of
  through `GenerationService.prepare`, which takes the `generate` command's
  string-keyed options. It still runs each request through
  `GenerationService.execute`, so exit codes and previews match `generate`.
  The full `--config-json` also sets `model`, so it never disagrees with
  `--model`.
- **Change** [M02]: Milestone 02 is done. Exit codes now report the cause
  (124 timeout, 128 + signal when stopped); the runner reaps the child and
  bounds the wait after SIGKILL, keeps the child's last lines, decodes output
  with replacement characters, installs signal handlers before starting the
  child, and lets `generate` without `--output` or with `--terminal-image` use
  the terminal.
- **Owner decision** [M01]: An omitted `output.directory` is
  `<output_directory>/<name>`, so each job gets its own folder. This
  supersedes the rule that it means the global `output_directory` itself.
- **Owner decision** [M01]: `name` stays required. Alternative rejected:
  defaulting it to the job file's name, which would tie output names to a file
  rename.
- **Owner decision** [M01]: Each `run-job` saves its full log as
  `<name>-<timestamp>-job.log` beside the manifest, in addition to the
  terminal output. `--dry-run` writes no log.
- **Owner decision** [M01]: Ctrl-C, `SIGTERM`, or `SIGHUP` during a job stops
  the current run gracefully and the whole job at once, marks both
  `interrupted`, and exits with `128 + signal number`. Alternative rejected:
  letting the current run finish on the first Ctrl-C.
- **Design decision** [M01] (owner): A job file has a required root-level
  `name`, the job name. It replaces `ai-video` as the base of output file
  names (`<name>-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`), last-frame names, and the
  manifest name (`<name>-<timestamp>-job.json`). It is 1 to 64 lowercase
  letters, digits, and hyphens, so it is safe in file names. It does not
  choose the output directory: an omitted `output.directory` still means the
  global `output_directory`. This supersedes the removal of `name` and the
  fixed `ai-video` base (both earlier today).
- **Owner decision** [M01]: The seed follows the same precedence as every
  other setting: `config_override.seed`, then `config_file`'s `seed`, then one
  random seed per job. This supersedes the rule that a missing
  `config_override.seed` always means a random seed.
- **Owner decision** [M01]: Every `run-job` writes a JSON manifest,
  `ai-video-<timestamp>-job.json`, in the output directory, recording each
  run's batch, pair, prompts, seed, input, output, command, exit code, and
  timing, because output names no longer identify the batch or pair.
  Alternatives rejected: the log only, and one sidecar file per output.
- **Owner decision** [M01]: A failed, timed-out, or interrupted run's partial
  output is kept and named in the error. Alternatives rejected: deleting or
  renaming it.
- **Owner decision** [M02]: The runner bugs from the code review of `b99603b`
  are fixed in a separate Milestone 02, done before Milestone 01.
- **Owner decision** [M01]: The one-off `generate` command is unchanged; modes
  and automatic output names apply to jobs only.
- **Design decision** [M01]: A run that exits with 0 but leaves no output file
  fails the job with exit code 1, since the next run would have no input. The
  job file's `version` key is required and must be 1.
- **Change**: Added the Milestone 02 plan
  (`milestone-02-runner-fixes.md`).
- **Owner decision** [M01]: Milestone 01 does not resize input images. In
  `i2i` and `i2v` jobs, the first input image must be exactly the job's width
  and height (from `config_override`, else `config_file`); if it is not, the
  app reports both sizes and refuses the job with exit code 2 before any run.
  `validate-job` and `--dry-run` run the same check. Resizing, including
  `size_from_input` and `max_pixels`, moves to a later milestone. This
  supersedes the `size_from_input` entries below.
- **Owner decision** [M01]: An `i2i` or `i2v` job must get a width and height
  from `config_override` or `config_file`, so the input can always be
  checked. Alternatives rejected: skipping the check, and using the input's
  own size.
- **Design decision** [M01]: Pillow stays, to read the input's size from the
  file header with EXIF rotation applied, so the size compared is the one the
  image is displayed at.
- **Owner decision** [M01]: `config_override.size_from_input: true` sets
  `width` and `height` from the job's input image, computed once and used by
  every run. It is opt-in, allowed only in `i2i` and `i2v` jobs, and cannot be
  combined with `width` or `height`. Alternatives rejected: always using the
  input size, and using it unless `width`/`height` are set.
- **Owner decision** [M01]: The size is scaled down (never up) to an area
  limit, rounded to the nearest multiple of 64, then stepped down by 64 until
  it fits, so the limit is never exceeded. The limit is `config_file`'s width ×
  height, or `config_override.max_pixels`. Alternatives rejected: a bounding
  box, no limit, a fixed default, and allowing a small overshoot.
- **Owner decision** [M01]: The app reads the image size with Pillow (new
  `pillow` dependency) and applies EXIF rotation. Alternatives rejected:
  ffprobe, which does not reliably report JPEG rotation, and macOS `sips`.
- **Owner decision** [M01]: `config_file` is required in every job. This
  supersedes the earlier rule that a job without `config_file` uses the model's recommended settings as its base.
- **Owner decision** [M01]: The repository folder stays `dt-config/`, and the
  raw Draw Things pass-through stays dropped from `config_override`.
- **Owner decision** [M01]: The `generation` block is replaced by two
  root-level keys. `config_file` holds a file name only (for example,
  `image-to-video-wan-2-2.json`); the app finds it in `dt-config/`, and its
  settings are the base configuration. `config_override` holds `model`,
  `refiner_model`, `refiner_start`, `steps`, `guidance_scale`, `shift`,
  `width`, `height`, `frame_count`, `strength`, and `seed`, which override the
  base for every run. This supersedes the `generation` block and its
  job-relative `config_file` path (2026-09-24, job generation settings).
- **Design decision** [M01]: `dt-config/` is the repository's directory next to
  `main.py`, not one relative to the current directory or the job file, so a
  job loads the same configuration wherever the app is started. A
  `config_file` value with a path separator or `..` is rejected.
- **Design decision** [M01]: The raw `config_overrides` pass-through is
  dropped. `config_override` accepts only the named keys, so every key is
  validated; any other Draw Things setting belongs in a `dt-config/` file.
  Alternative rejected: mixing named snake_case keys and raw camelCase keys in
  one block.

- **Owner decision** [M01]: Job files have no root-level `name` key. Output
  file names no longer use it, since each run is named
  `ai-video-<timestamp>-<NNNN>`.
- **Design decision** [M01]: Without `name`, an omitted `output.directory`
  means the global `output_directory` itself, superseding the
  `<output_directory>/<name>` default. Alternative rejected: deriving a
  directory from the job file's name, which would tie outputs to a file
  rename. Set `output.directory` to keep a job's outputs separate.

- **Owner decision** [M01]: Each job sets model, refiner model, steps, and
  the other generation settings once, and they apply to every run.
- **Design decision** [M01]: These settings live in one `generation` block
  (`model`, `refiner_model`, `refiner_start`, `config_file`, `steps`,
  `guidance_scale`, `shift`, `width`, `height`, `frame_count`, `strength`,
  `seed`, `config_overrides`). This supersedes the top-level `model` and
  `config_file` keys and the `settings` block. Key names are descriptive
  snake_case instead of copies of CLI flags: `guidance_scale` replaces `cfg`,
  and `frame_count` replaces `frames`. The top-level `timeout` is renamed
  `run_timeout_seconds` to state its unit and that it limits each run.
- **Design decision** [M01]: Settings without a `draw-things-cli` flag
  (refiner, shift, and anything in `config_overrides`) are merged with
  `config_file` by the app into one `--config-json`, in the order
  `config_file`, `config_overrides`, named keys. Alternative rejected: passing
  both `--config-file` and `--config-json` and relying on the CLI's merge
  order, which is undocumented. A `config_overrides` key that duplicates a
  named key is an error, so a setting cannot be set in two places.

- **Owner decision** [M01]: Each job declares a required `mode`: `i2i`
  (image-to-image), `t2v` (text-to-video), or `i2v` (image-to-video). `i2i`
  and `i2v` jobs require `input`; a `t2v` job must not set it. This supersedes
  the 2026-09-28 decision that `input` is required in every job. Text-to-image
  jobs remain out of scope.
- **Owner decision** [M01]: A `t2v` job generates run 1 from text alone, then
  continues as image-to-video from the previous run's last frame, so the chain
  is kept. Its model must support both. Alternatives rejected: independent
  unchained `t2v` runs, and a per-job `chain` switch.
- **Owner decision** [M01]: Each run's output is named
  `ai-video-<YYYYmmdd-HHMMSS>-<NNNN>.<ext>`: base `ai-video`, the time the run
  starts, and a random 4-digit number, joined with hyphens. The default
  extension is `.mov`; `i2i` defaults to `.png` because `draw-things-cli` writes
  images only as PNG. Last frames are `<stem>-last-frame.png`. This supersedes
  the `<name>-b<batch:03>-<pair-name>.<ext>` names (2026-09-25).
- **Design decision** [M01]: Because names are unique per run, an existing
  file no longer blocks the job. If a generated name already exists, the app
  draws a new random number instead of overwriting. Names printed by
  `--dry-run` are examples, since a real run generates new ones. This
  supersedes the refuse-to-start rule and the dry-run existing-output check
  (2026-09-24, 2026-09-28).

## 2026-09-28

- **Owner decision** [M01]: `input` is required in every job. Text-to-image
  jobs (no input file) are out of scope for this milestone.
- **Owner decision** [M01]: Remote and cloud generation are out of scope for
  M1. Jobs run locally with `draw-things-cli` from `PATH`, or the binary named
  by `--executable`. Backend options and secrets do not appear in job files or
  the global config.
- **Owner decision** [M01]: When `settings.seed` is not set, the app draws one
  random seed at job start, uses it for every run, and logs it once. This
  supersedes the per-run random seed from 2026-09-24.
- **Design decision** [M01]: `--dry-run` runs full validation (input and config
  files exist, no output file exists, `ffmpeg` available for video) before
  printing the commands, so a job that passes a dry run should start for real.
  Alternative rejected: syntax-only validation.

## 2026-09-27

- **Owner decision** [M01]: `run-job` and `validate-job` take the job file's
  path. A bare job name (`sunset-walk` resolving to `data/sunset-walk.yaml`) is
  not supported.
- **Owner decision** [M01]: The repository commits
  `config/global_config.example.yaml` and git-ignores
  `config/global_config.yaml`, since absolute directories are specific to one
  machine. This supersedes the plan to commit `config/global_config.yaml`
  itself.

## 2026-09-26

- **Owner decision** [M01]: One prompt pair can be marked as the default
  (`default: true`). A batch with no explicit assignment uses the default pair.
  This supersedes the rule from 2026-09-25 that every batch must be assigned
  explicitly.
- **Design decision** [M01]: At most one default pair is allowed. An unassigned
  batch with no default pair is still a validation error, so nothing is guessed.
  The default pair may also list explicit `batches`, and those apply as
  written. A single-pair job treats that pair as the default without a marker.
  Duplicate and out-of-range batch numbers remain errors.

## 2026-09-25

- **Owner decision** [M01]: Prompt pairs are named by the user (unique name),
  and a batch schedule assigns batches to pairs. With `batch_count: 5`, pair 1
  can take batches 1, 3, and 5, and pair 2 batches 2 and 4. This supersedes the
  positional pairing of `positive_prompts` and `negative_prompts` (2026-09-24).
- **Design decision** [M01]: `batch_count` is now the total number of runs, not
  a per-pair repeat count, so a job expands into exactly `batch_count` runs in
  batch order. This supersedes the `pairs × batch_count` expansion
  (2026-09-24). Every batch must be assigned to exactly one pair; gaps,
  duplicates, and out-of-range batches are validation errors, so a typo cannot
  silently skip or repeat a run.
- **Design decision** [M01]: Batches are listed explicitly rather than as
  ranges or patterns, to keep the format simple for this milestone. Output
  names become `<name>-b<batch>-<pair-name>.<ext>`, superseding the
  `-p<pair>-b<batch>` form.

## 2026-09-24

- **Owner decision** [M01]: `config/global_config.yaml` holds absolute
  directories (`input_directory`, `output_directory`). A leading `~` is
  expanded; relative paths are rejected. This supersedes the earlier plan to
  resolve these paths against the project root.
- **Owner decision** [M01]: Global settings live in a YAML file at
  `config/global_config.yaml`. For now it specifies only the default input and
  output directories.
- **Owner decision** [M01]: Multiple positive and negative prompts pair by
  position. Negatives may be omitted (the model's recommended negative prompt
  applies), a single shared entry, or the same length as the positives.
  Alternatives rejected: every combination, and joining all prompts into one.
- **Owner decision** [M01]: Runs are chained in Milestone 1. The first input
  file seeds run 1, and each run's output (the last frame, for video) seeds the
  next run. A failed run stops the job.
- **Owner decision** [M01]: Batch jobs are described by a YAML job definition
  under `data/`, which the app reads and executes. It covers the first input
  file, batch count, and multiple positive and negative prompts.
- **Design decision** [M01]: Jobs expand into `pairs × batch_count` runs, all
  batches of one prompt pair before the next. Output files are named
  `<name>-p<pair>-b<batch>.<ext>`, and the job refuses to start if any of them
  already exist, so earlier results are never overwritten.
- **Design decision** [M01]: Unknown keys in the global config and job files
  are errors, so typos fail before any generation starts.
- **Change**: Added the Phase 1 plan (`README.md`) and the Milestone 1 plan
  (`milestone-01-job-definition-batch.md`). No code has changed yet.
