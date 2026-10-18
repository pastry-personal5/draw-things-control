# Architecture

`framecontrol` exists for long-horizon video generation by
autoregressive chaining: each run starts from the previous run's output.
Each run of a chained job selects its own backend: Draw Things through
`draw-things-cli`, or H3 image-to-audio-video through the project-owned h3.c
installation, and one job may mix them. One executor runs the chain; front ends (CLI, TUI,
API, MCP) are thin layers over it. The run lock permits one supervised backend
command at a time on the machine.

## Overview

```
CLI (Typer) ─┐   TUI (Textual) ─┐   HTTP API ─┐            MCP server ─▶ HTTP API
             ▼                  ▼             ▼
                 services: Toolkit, JobRunSession, JobCatalog, HistoryReader
             │
             ▼
   state (SQLite: Store, repositories, recorder) ── run lock (flock)
             │
             ▼
   jobs: JobExecutor ◀── events, cancel() ── JobPlanner, RunLauncher, JobRecords, JobLogWriter
             │                             │
             └──────── backend registry ───┘ (Draw Things and H3 adapters)
             │
             ▼
   core: supervised process runner ──▶ backend executable
```

## Source layout

Everything is one package, `src/framecontrol/`; nothing lives in the
project root but configuration, data, docs, and tests. Dependencies point one
way, from front ends down to `core`, and `tests/test_architecture.py` enforces
it from the imports.

```
src/framecontrol/
├── core/        # paths, errors, exit codes, YAML, configuration, arguments, the runner and its process package
├── jobs/        # job definition and parsing, the executor and its parts, events, records, inputs, media
├── state/       # SQLite database, repositories, typed rows, recorder, history import
├── services/    # the use cases every front end shares, and the wiring of the real tools
├── cli/         # Typer app: the `fc` command
├── tui/         # Textual app: screens, panes/, text/, the command controller
├── server/      # HTTP API and gRPC monitoring service; its queue worker is in services/
└── mcp_server/  # MCP server: typed tools over the HTTP API, its SSE watch included, on stdio or Streamable HTTP
tests/           # mirrors the package: tests/core, tests/jobs, tests/state, tests/services, ...
```

Import direction: `cli`, `tui`, `server` -> `services` -> `state` -> `jobs` ->
`core`; `mcp_server` imports nothing else from the package and talks to `server`
over HTTP only, with no gRPC. Front ends never import each other, except that three commands in
`cli/app.py` start one: `fc tui` the TUI app, `fc serve` the HTTP API and gRPC
service, and `fc mcp` the MCP server (none of those imports `cli`). Nothing below the front ends imports Typer, Textual, a
web, MCP, or gRPC framework.
Launch with `fc` or `python -m framecontrol`.

## Modules

| Module | Responsibility |
|--------|----------------|
| `core/paths.py` | `ProjectPaths`: where the project keeps its files, including the project-owned H3 executable and model snapshot, worked out once and passed down; no module holds a path of its own |
| `core/errors.py`, `core/exit_codes.py` | `FramecontrolError` and its coded subclasses (`invalid_input`, `tool_missing`, `not_found`, `busy`, `state_unavailable`, `conflict`, and `not_permitted`, among others); the exit codes, signal codes, and the one mapping from an error's code to an exit code |
| `core/yaml_files.py` | The one strict YAML reader: duplicate and non-string keys, octal and base-60 numbers are refused |
| `core/draw_things_config.py` | Draw Things configurations (YAML only), lookup in `data/params/`, and job overrides applied to them |
| `core/arguments.py` | Validated options and argument-vector building (no shell); one flag table (`GENERATE_FLAGS`) drives the builder, the parser, and redaction; `OVERRIDE_TARGETS` says what each job override becomes; `command_settings` reads a saved command back |
| `core/generation.py` | `generate` use case: `GenerateRequest`, resolve, preview or run; `require_executable` |
| `core/global_config.py`, `core/cooldown.py` | The global configuration; `CooldownPolicy` and its mapping, shared by it and jobs |
| `core/numbers.py`, `core/clock.py` | Numbers read from outside and written for people; the clock every timestamp comes from |
| `core/run_lock.py` | `fcntl.flock` on `state/run.lock`, naming the holder and the running child |
| `core/process/` | The supervised process runner (process group, timeout, graceful-then-forced shutdown), its fork-free `posix_spawn`/clean-exec boundary for threaded front ends, runner factories, output processing, signals, `CancelToken`, process groups. Each reader thread reads its pipe as bytes and `segments.py` cuts the stream at `\r` and `\n`, stamping every segment with the arrival of its last content byte; a backend whose redraws end with no terminator (H3) offers a predicate in its arguments (`unterminated_reading`) so a whole reading is released after an idle window instead of one update late |
| `jobs/backend.py`, `jobs/backends/` | The backend protocol and registry; adapters own typed settings, safe settings summaries, command preparation, tool checks, per-run finishing state, and output finishing. The Draw Things adapter keeps its color policy inside that boundary. H3 owns first-frame preparation, project-installed runtime preflight, staged MP4 verification/promotion, audio facts, mixed `i2av`/`r2av` argv, fingerprint-gated color finishing, and verified sRGB handoffs when extraction succeeds; a valid MP4 can succeed with a missing-handoff warning, while the executor stops a dependent `i2av` run before launch. H3 uses its own media color inspection, conversion, drift, and correction modules, and does not enter the Draw Things color pipeline. `h3_references.py` makes private, digest-checked copies |
| `jobs/definition.py`, `jobs/parsing.py`, `jobs/prompt_pairs.py`, `jobs/overrides.py` | A validated job, and `JobParser` (`load_job`, `load_job_text`), which validates and confines paths before file access, and whose errors are `InputError`s naming the file and the field. A job is either a version-1 job, whose job-wide fields describe every run (only a snapshot queued before the cutover is read this way, `allow_version_1`), or a version-2 job, whose `effective_runs` are its frozen per-run plan. Shared code asks `JobDefinition` per run (`run_backend`, `run_needs_visual_input`, `run_checks_input`, `job_for_run`, `mode_label`) and never reads a job-wide field to decide what one run does |
| `jobs/compile_v2.py` | `RunCompiler`: turns a version-2 file (name-keyed catalogs, `defaults` by `(backend, pipeline)`, ordered `runs`) into one immutable `EffectiveRun` per run. Each run's own adapter parses and validates a one-run view of it (`EffectiveRun.backend_job`), so the command, finishing, and color code of the backends is reused unchanged; the compiler resolves each knob with its source, derives H3 frame counts, checks every cross-run edge it can know before anything runs (the size a handoff has against the size its consumer takes), and answers a client's `GET /v1/capabilities` (`job_format_summary`) |
| `jobs/executor.py` | `JobExecutor.run(job, JobRunOptions)`: the chain of runs, the cooldown between them, cancellation; the preparation of a handoff that crosses a backend or profile boundary (the consuming adapter's `plan_input`); the anchor reset an independent `r2av` or `t2v` run makes; the chain's first image (a `t2v` job's, named by `JobStarted` before run 1 writes it, dropped with `FirstImageDropped`, which `state/recorder.py` follows, when it is not kept), and the anchor state handed to the backend for each run |
| `jobs/planning.py`, `jobs/launcher.py`, `jobs/run_finisher.py`, `jobs/color_run.py` | The shared seed, file names, and backend-prepared arguments of each run (`JobPlanner`); supervised execution and common result mapping (`RunLauncher`); Draw Things tagging, last frame, drift check, correction, and measurement (`RunFinisher`, called by its adapter); the correction's two passes over a run's frames, its corrected copy and handoff, and its checks (`ColorCorrector`, loaded only when a job first corrects; a failed correction hands off the uncorrected frame) |
| `jobs/records.py`, `jobs/log_writer.py` | The manifest and job log file (`JobRecords`); the job's log lines, written from its events |
| `jobs/events.py` | Typed events, `RunStatus` and `JobStatus`, and `event_to_dict` for a JSON stream. A run's stages are `StageStarted` and `StageFinished`, and what it is judged by is `TimingStarted` and `TimingFinished`; `RunOutput` carries the `phase` a line read as, its `stage` and counter (`done`, `total`), and the `offset` (seconds after `RunStarted`, like a stage's) it was written at |
| `jobs/timing/`, `jobs/run_timing.py` | Stage timing, with no file or process in it. `parsers.py` reads each backend's output lines as readings (a stage, its units done, and whether the line marks work starting, as Draw Things' does, or done, as H3's); `tracker.py` turns readings into contiguous spans, a counter that restarts opening a new one and an unknown phase kept as `other`; `timeline.py` puts the child's spans and Framecontrol's own (`with timeline.stage("verify")`) on one run clock and names every gap `unaccounted`; `log_import.py` rebuilds spans from a job log and removes H3's one-update lag; `RunTiming` is the executor's side, which makes the events. The forecast model is `features.py` (the signature and feature values, with H3's reuse interval and the steps it evaluates), `fit.py` (the ridge fit, the tiers, and the plan that prices a run), `factory.py` (measured starting points), `forecast.py` (the `Forecaster`, a pure function of a job's events and the fitted model, and the `Forecast` it publishes) and `replay_stream.py` (a stored run replayed as events). They import only `core` and `numpy`. An adapter opts in with `stage_parser()`, `timing_workload()`, and `timing_environment()`, and its `finish` takes a `timer` |
| `jobs/files.py`, `jobs/text.py` | Bounded job-file reads, and the text `validate-job` and the TUI show |
| `jobs/inputs/`, `jobs/media/`, `jobs/output_naming.py` | Input image check, and run 1's copy of every input (`inputs/resize.py`: upright, 8-bit sRGB, the job's size, made in floating point and rounded once; 16-bit RGB read through `ffmpeg`; a matrix-and-curves ICC profile applied with gamut mapping at constant Oklab lightness and hue, `inputs/gamut.py`, any other through LittleCMS's perceptual intent; Lab values through LittleCMS's Lab transform, profile or not; YCbCr and HSV converted to RGB first); Oklab and gamut edges (`media/oklab.py`); a video's frames as floating-point sRGB raised by the half level Draw Things truncated, and the PNGs the tool wrote read as `draw-things-cli` reads them (`media/clip_frames.py`); color statistics and the `color_drift` check of every video run against its input, its frame 0, and the chain's first image, region by region where a segmenter is given (`media/color_stats.py`, `media/drift.py`); a frame's regions, people, their skin, and the background, from a `Segmenter` (`media/regions.py`: the skin set by each face's own, soft masks kept 8-bit at the segmenter's resolution, feathered for blending, numpy and Pillow only), and Apple Vision's `Segmenter` (`media/vision_segmenter.py`, the only module that imports pyobjc, when `services/toolkit.py` first makes it, once per process, for both the drift check and the correction); the color correction's fit, anchor pull and ramp, caps, smoothing, chroma refinement, skin residual, and applying it to a frame, region by region through soft masks, knowing no files (`media/correction.py`), and the corrected copy's encoder (`media/clip_frames.py`, passing over a listed VideoToolbox that fails a one-frame test encode, kept per process); `ffmpeg` and `ffprobe`; the color a video's stream states, read once before tagging (`stream_color.py`), which both the last frame's decode and the `colr` tag (`video_color.py`) follow; the last frame as the handoff the next run reads (16-bit RGB without alpha, each sample `v * 256 + 128`, with half a level added back and ordered dither, `frames.py`, which also holds the rounding's numpy twin); measuring; the media checks of a video job's input (a resume's as a handoff), resized copy, video, and last frame (`checks.py`, with the matrix measurement in `fingerprint.py`, finding `ffmpeg` and `ffprobe` at each check), reported through `RunFinisher` and the executor as `MediaChecked` events, which `state/recorder.py` keeps in the `media_checks` table (a check it cannot store is skipped; any other failed write stops recording the execution); output names, including the first image named from the manifest's stem |
| `state/database.py`, `state/database_migration.py`, `state/schema.py` | The `state/framecontrol.db` SQLite file, its connections and schema migrations, and the one-time verified backup from `state/framechain.db` or `state/dtc.db` before opening state |
| `state/execution_rows.py`, `state/executions.py`, `state/job_ids.py`, `state/settings.py` | `ExecutionRow`, `RunRow`, `NewExecution`, `NewRun`, and `ExecutionSettings`; the `ExecutionRepository` built on them. Runs retain measured video dimensions/rate/duration and native H3 audio facts as additive fields; `JobIdRepository`, the `job_definitions` table behind J0001 |
| `state/store.py`, `state/recorder.py`, `state/history_import.py`, `state/ids.py` | `Store` (opened as `run`, `write`, or `browse`), the event recorder, the phase 1 import, and `E0012` and `J0001` |
| `state/timing.py`, `state/timing_import.py` | `TimingRepository`: the `run_stages` table (one row per span, written when its stage finishes, so a crash keeps every finished span) and the `run_timing` table (one row per run: what it was judged by, `live` or `log` fidelity, and `complete`, false until the run succeeds). Both cascade from `executions`, so `history_retention_days` pruning and deleting an execution remove them with it; there is no second retention rule. `import_timing` rebuilds them from the job logs that still exist, once per execution, and never writes a log. A timing row the recorder cannot store is skipped and logged once, like a media check |
| `state/queue.py` | `QueueRepository`, `QueueRow`, and `QueueState`: the `queue` table, ordered first in, first out by its public ID (`Q0007`); retention removes queue-owned H3 reference snapshot directories after their final queue reference is gone, with a one-hour grace period for unpublished submissions |
| `services/toolkit.py` | `Toolkit`: the real tools; builds the generation service and executors |
| `services/job_runs.py` | `JobRunSession`: takes the run lock, opens the store, sweeps, and runs a job with its execution recorded |
| `services/job_catalog.py`, `services/job_details.py`, `services/history.py`, `services/store_provider.py` | The job files of a directory, a job's summary and plan, the execution history, and the browsing store they share |
| `services/queue_submit.py`, `services/queue_resume.py` | Validate a job and snapshot it as a queue entry; freeze H3 references at submission and verify copies selected by unfinished runs on claim/resume; resolve and accept a resume |
| `services/queue_worker.py`, `services/queue_worker_status.py`, `services/queue_recovery.py`, `services/queue_host.py`, `services/queue_cancel.py` | The queue's one worker thread and its observable status (`is_alive`, `state`, `cooldown_until`, `current_run`, `between_runs_after`, and the latest `forecast`); restart recovery; the host that owns the run lock, the store, and the worker's lifecycle; cancelling an entry |
| `services/queue_hold.py`, `services/queue_park.py`, `services/queue_park_text.py` | The queue's hold, in memory and in the `settings` row; parking and unparking a running entry, and holding and releasing the queue; the words both front ends use for a park and its outcome |
| `services/queue_callers.py` | Keeping agents (the caller `mcp`) off the queue entries and holds people made: `check_entry_permitted`, and the words of a refusal |
| `services/api_rules.py`, `services/input_listing.py`, `services/queue_events.py` | The rules and limits every job the API runs or writes must meet; the input directory's images; turning the worker's transitions and job events into the (kind, data) shape an event sink takes |
| `state/audit.py` | `AuditRepository`: the `audit_log` table (schema 5) behind `GET /audit` |
| `server/` | `fc serve`'s FastAPI app, bounded HTTP body gate, audit and safe error boundary, SSE watch, and gRPC monitoring service; see [Phase 3](archive/phase-3/README.md) |
| `mcp_server/` | `fc mcp`: tools, resources, and queue waiting over the HTTP API alone, on stdio or Streamable HTTP; see Phase 3's [MCP server](archive/phase-3/milestone-10-mcp-server.md) and [MCP over Streamable HTTP](archive/phase-3/milestone-13-mcp-over-http.md) |
| `cli/app.py` | Commands (`generate`, `validate-config`, `validate-job`, `import-history`, `tui`, `serve`, `mcp`, and the `queue`, `history`, and `timing` groups) and `CliServices` in Typer's context. `fc timing import` is registered here beside `import-history`, which it opens the state as; `fc timing show` is in `cli/timing_app.py` and reads the API |

Services receive their runner and executable lookup as dependencies, so tests
never start a process.

**Process supervision.** The runner starts the CLI in its own process group and
reads stdout and stderr concurrently, as bytes, stamping each line when its last
byte arrives (not when the supervisor next looks at the queue). `SIGHUP`, `SIGINT`, and `SIGTERM` send
`SIGTERM` to the group, wait `--shutdown-grace` (10 s), then `SIGKILL`.
`--timeout` uses the same sequence. Without `--output`, the child inherits the
terminal for inline preview. Every project-owned subprocess starts through a
clean helper interpreter reached with `posix_spawn`; that helper closes inherited
descriptors, creates the requested session, changes directory, and execs the
real command. The threaded gRPC server and clients therefore never fork their
live C-core state into a child.

**Exit codes.** 0 success; 1 run wrote no output, last-frame extraction
failed, or the state database or lock cannot be used; 2 invalid input; 75 run
lock held by another run; 124 timeout; 128+N stopped by signal N (130 for
Ctrl-C); otherwise the CLI's own code.

## Runtime ownership

`fc serve` owns the run lock, state store, queue host, and the only worker
allowed to start `draw-things-cli`. It executes one entry at a time. Shutdown
stops the current child and releases the lock only after the worker has joined;
startup recovers entries left running by a previous server.

A queued job stores a versioned snapshot of the exact job text, each Draw Things
profile's base-configuration text, backend identity, resolved directories, and
cooldown policy. A version-2 snapshot (version 7) also stores its compiled plan,
which is compared with what the frozen text recompiles to on every read, so a
resume reads the snapshot and never a live file, profile, or reference. H3 queue snapshots also store private copies, digests, and media facts for every reference pair. Retention removes those copies when their queue snapshots are gone. H3 snapshots deliberately exclude the mutable local model snapshot, which is rechecked before execution and resume. Older
rows without a backend read as Draw Things. Backend IDs in older queued snapshots are not translated; a snapshot naming `draw-things` or `h3` must be resubmitted with `draw-things-cli` or `h3-metal-cli`. A queued one-off
generation stores its resolved request and also identifies Draw Things as its
backend. Later file edits therefore do not change queued work. Resuming copies
the original snapshot and backend into a new entry at the next unfinished run
with the original seed; an interrupted run always restarts from its boundary.

The SQLite store keeps execution, run, queue, job-ID, audit, and preference
state. Public IDs such as `E0012`, `Q0007`, and `J0001` are stable and
monotonic. Retention prunes database history, not generated output; resumable
parked chains are retained while they are still needed.

**Stage timing.** Every run of either backend leaves a measured timeline: the
child's stages as its output named them, then Framecontrol's own finishing, on
one run clock whose gaps are named `unaccounted`, so the stages add up to the
run. It is kept in `run_stages` and `run_timing`, which belong to the
execution: `history_retention_days` pruning and deleting an execution remove
them with it, so a forecast fitted on them sees the retained window and nothing
older. The timeline is the executor's, built from events a front end can also
read (`StageStarted`, `StageFinished`, `TimingStarted`, `TimingFinished`); the
recorder keeps each span when its stage finishes. A one-off `generate` is not a
job run and is not timed.

**Forecast.** The server computes one completion-time forecast of the running job
and every front end shows it. `services/timing_history.py` loads the retained
`complete` runs and fits `FittedModel` once per completed run (timing rows
recorded under the pre-rename backend ids join the current backends),
`jobs/timing/forecast.py` turns the queue worker's job events into a `Forecast`
(the active stage and its units, `run` and `job` spans with a range, and a
basis), and `services/forecast_publisher.py` applies the publishing rule (at a
change, and within a stage at most once every 2 s), holds the span's `floor` so
a bar never moves back, and sends `forecast_updated` through
`QueueEventPublisher`, not as a `JobEvent`, so the recorder and the job log never
see it. The HTTP queue entry, the SSE watch, gRPC's `QueueEntrySnapshot` and
MCP carry it; `services/forecast_view.py` is the one place that turns a span
into seconds left, a share done and end text, and
`services/timing_replay.py` scores an estimator on the retained runs
(`fc timing replay`). `tests/test_architecture.py` fails if `tui/`, `cli/` or
`mcp_server/` imports the model or its loader, so no front end can grow a second
forecast.

## Interfaces

The CLI and TUI use the HTTP API for queue control. The TUI and
`fc queue add --wait` observe live work over authenticated gRPC; MCP uses the
API's authenticated SSE watch. `fc mcp` is an HTTP-only client of
`fc serve` and is available over stdio or Streamable HTTP.

The TUI reads display data through services and renders typed events; it never
parses logs. The API and MCP layers translate requests and responses but keep
business rules in `services/`. The MCP package imports no other project
package and reaches the server only over HTTP.

Job-file creation, replacement, and recoverable deletion require
`fc serve --allow-write`. Replacements use SHA-256 optimistic concurrency,
and backups and trashed files remain under `data/jobs/`. Execution deletion is
separate and permanent.

## Safety boundaries

- Paths originate in `ProjectPaths` and are passed down. Job input, output,
  job-file, and named-configuration paths are confined and bounded before use.
- The API is bearer-authenticated except for health. Non-loopback binding
  requires an explicit override; request bodies, job sizes, run counts, and
  worst-case durations are limited.
- Queue mutation is serialized with submission and worker claims. Agents cannot
  control entries or holds created by people.
- Audits record actions and typed outcomes without prompt text, YAML, or
  credentials. Credentials never enter events, storage, logs, or responses.
- `data/params/*.json`, `data/params/*.yaml`, and
  `config/global-config.yaml` are never edited by an interface.

## Design history

The architecture above describes the current system. Rationale, superseded
choices, and implementation history stay in the phase documents rather than
being repeated here:

- [Phase 1](archive/phase-1/README.md): jobs and chained runs
- [Phase 2](archive/phase-2/README.md): TUI, shared state, and the run lock
- [Phase 3](archive/phase-3/README.md): API, queue, and MCP
- [Phase 4](phase-4/README.md): multiple backends, H3 video, the Framecontrol rename, and version-2 jobs
