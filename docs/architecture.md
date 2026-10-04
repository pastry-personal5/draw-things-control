# Architecture

`draw-things-control` exists for long-horizon video generation by
autoregressive image-to-video chaining: each run starts from the previous run's
output. It uses the Draw Things app beneath, through the locally installed
`draw-things-cli`. One core runs the chained generations; front ends (CLI, TUI, API, MCP) are thin layers over it.
Only one `draw-things-cli` runs at a time on the machine.

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
             │
             ▼
   core: DrawThingsProcessRunner ──▶ draw-things-cli
```

## Source layout

Everything is one package, `src/draw_things_control/`; nothing lives in the
project root but configuration, data, docs, and tests. Dependencies point one
way, from front ends down to `core`, and `tests/test_architecture.py` enforces
it from the imports.

```
src/draw_things_control/
├── core/        # paths, errors, exit codes, YAML, configuration, arguments, the runner and its process package
├── jobs/        # job definition and parsing, the executor and its parts, events, records, inputs, media
├── state/       # SQLite database, repositories, typed rows, recorder, history import
├── services/    # the use cases every front end shares, and the wiring of the real tools
├── cli/         # Typer app: the `dtc` command
├── tui/         # Textual app: screens, panes/, text/, the command controller
├── server/      # HTTP API; its queue worker is in services/        (phase 3)
└── mcp_server/  # MCP server: typed tools over the HTTP API, its SSE watch included, on stdio or Streamable HTTP (phase 3)
tests/           # mirrors the package: tests/core, tests/jobs, tests/state, tests/services, ...
```

Import direction: `cli`, `tui`, `server` -> `services` -> `state` -> `jobs` ->
`core`; `mcp_server` -> `server` over HTTP only, and imports nothing else from the
package. Front ends never import each other, except that three commands in
`cli/app.py` start one: `dtc tui` the TUI app, `dtc serve` the server, and
`dtc mcp` the MCP server. Nothing below the front ends imports Typer, Textual, a
web, MCP, or gRPC framework.
Launch with `dtc` or `python -m draw_things_control`.

Phase plans: [1](archive/phase-1/README.md), [2](archive/phase-2/README.md),
[3](phase-3/README.md). Each phase reuses the layers below it unchanged.

## Modules

| Module | Responsibility |
|--------|----------------|
| `core/paths.py` | `ProjectPaths`: where the project keeps its files, worked out once and passed down; no module holds a path of its own |
| `core/errors.py`, `core/exit_codes.py` | `DtcError` and its coded subclasses (`invalid_input`, `tool_missing`, `not_found`, `busy`, `state_unavailable`, and, from phase 3, `conflict` and `not_permitted`, among others); the exit codes, signal codes, and the one mapping from an error's code to an exit code |
| `core/yaml_files.py` | The one strict YAML reader: duplicate and non-string keys, octal and base-60 numbers are refused |
| `core/draw_things_config.py` | Draw Things configurations (YAML only), lookup in `data/params/`, and job overrides applied to them |
| `core/arguments.py` | Validated options and argument-vector building (no shell); one flag table (`GENERATE_FLAGS`) drives the builder, the parser, and redaction; `OVERRIDE_TARGETS` says what each job override becomes; `command_settings` reads a saved command back |
| `core/generation.py` | `generate` use case: `GenerateRequest`, resolve, preview or run; `require_executable` |
| `core/global_config.py`, `core/cooldown.py` | The global configuration; `CooldownPolicy` and its mapping, shared by it and jobs |
| `core/numbers.py`, `core/clock.py` | Numbers read from outside and written for people; the clock every timestamp comes from |
| `core/run_lock.py` | `fcntl.flock` on `state/run.lock`, naming the holder and the running child |
| `core/process/` | `DrawThingsProcessRunner` (process group, timeout, graceful-then-forced shutdown), its protocols (`RunnerFactory`), output processing, signals, `CancelToken`, process groups |
| `jobs/definition.py`, `jobs/parsing.py`, `jobs/prompt_pairs.py`, `jobs/overrides.py` | A validated job, and `JobParser` (`load_job`, `load_job_text`), whose errors are `InputError`s naming the file and the field |
| `jobs/executor.py` | `JobExecutor.run(job, JobRunOptions)`: the chain of runs, the cooldown between them, cancellation; the chain's first image (a `t2v` job's, named by `JobStarted` before run 1 writes it, dropped with `FirstImageDropped`, which `state/recorder.py` follows, when it is not kept), and each correcting run's anchor, re-anchored where the prompt pair changes (`RunColor`) |
| `jobs/planning.py`, `jobs/launcher.py`, `jobs/run_finisher.py`, `jobs/color_run.py` | The seed, file names, and arguments of each run (`JobPlanner`); one run through draw-things-cli (`RunLauncher`); tagging, last frame, the color drift check, the color correction, and measuring (`RunFinisher`); the correction's two passes over a run's frames, its corrected copy and handoff, and its checks (`ColorCorrector`, loaded only when a job first corrects; a failed correction hands off the uncorrected frame) |
| `jobs/records.py`, `jobs/log_writer.py` | The manifest and job log file (`JobRecords`); the job's log lines, written from its events |
| `jobs/events.py` | Typed events, `RunStatus` and `JobStatus`, and `event_to_dict` for a JSON stream |
| `jobs/files.py`, `jobs/text.py`, `jobs/parsing.py` | Bounded job-file reads, parser validation and path confinement before file access, and the text `validate-job` and the TUI show |
| `jobs/inputs/`, `jobs/media/`, `jobs/output_naming.py` | Input image check, and run 1's copy of every input (`inputs/resize.py`: upright, 8-bit sRGB, the job's size, made in floating point and rounded once; 16-bit RGB read through `ffmpeg`; a matrix-and-curves ICC profile applied with gamut mapping at constant Oklab lightness and hue, `inputs/gamut.py`, any other through LittleCMS's perceptual intent; Lab values through LittleCMS's Lab transform, profile or not; YCbCr and HSV converted to RGB first); Oklab and gamut edges (`media/oklab.py`); a video's frames as floating-point sRGB raised by the half level Draw Things truncated, and the PNGs the tool wrote read as `draw-things-cli` reads them (`media/clip_frames.py`); color statistics and the `color_drift` check of every video run against its input, its frame 0, and the chain's first image, region by region where a segmenter is given (`media/color_stats.py`, `media/drift.py`); a frame's regions, people, their skin, and the background, from a `Segmenter` (`media/regions.py`: the skin set by each face's own, soft masks kept 8-bit at the segmenter's resolution, feathered for blending, numpy and Pillow only), and Apple Vision's `Segmenter` (`media/vision_segmenter.py`, the only module that imports pyobjc, when `services/toolkit.py` first makes it, once per process, for both the drift check and the correction); the color correction's fit, anchor pull and ramp, caps, smoothing, chroma refinement, skin residual, and applying it to a frame, region by region through soft masks, knowing no files (`media/correction.py`), and the corrected copy's encoder (`media/clip_frames.py`, passing over a listed VideoToolbox that fails a one-frame test encode, kept per process); `ffmpeg` and `ffprobe`; the color a video's stream states, read once before tagging (`stream_color.py`), which both the last frame's decode and the `colr` tag (`video_color.py`) follow; the last frame as the handoff the next run reads (16-bit RGB without alpha, each sample `v * 256 + 128`, with half a level added back and ordered dither, `frames.py`, which also holds the rounding's numpy twin); measuring; the media checks of a video job's input (a resume's as a handoff), resized copy, video, and last frame (`checks.py`, with the matrix measurement in `fingerprint.py`, finding `ffmpeg` and `ffprobe` at each check), reported through `RunFinisher` and the executor as `MediaChecked` events, which `state/recorder.py` keeps in the `media_checks` table (a check it cannot store is skipped; any other failed write stops recording the execution); output names, including the first image named from the manifest's stem |
| `state/database.py`, `state/schema.py` | The SQLite file, its connections, transactions, and migrations |
| `state/execution_rows.py`, `state/executions.py`, `state/job_ids.py`, `state/settings.py` | `ExecutionRow`, `RunRow`, `NewExecution`, `NewRun`, and `ExecutionSettings`; the `ExecutionRepository` built on them; `JobIdRepository`, the `job_definitions` table behind J0001 |
| `state/store.py`, `state/recorder.py`, `state/history_import.py`, `state/ids.py` | `Store` (opened as `run`, `write`, or `browse`), the event recorder, the phase 1 import, and `E0012` and `J0001` |
| `state/queue.py` (phase 3) | `QueueRepository`, `QueueRow`, and `QueueState`: the `queue` table, ordered first in, first out by its public ID (`Q0007`) |
| `services/toolkit.py` | `Toolkit`: the real tools; builds the generation service and executors |
| `services/job_runs.py` | `JobRunSession`: takes the run lock, opens the store, sweeps, and runs a job with its execution recorded |
| `services/job_catalog.py`, `services/job_details.py`, `services/history.py`, `services/store_provider.py` | The job files of a directory, a job's summary and plan, the execution history, and the browsing store they share |
| `services/queue_submit.py`, `services/queue_resume.py` (phase 3) | Validate a job and snapshot it as a queue entry; resolve and accept a resume |
| `services/queue_worker.py`, `services/queue_worker_status.py`, `services/queue_recovery.py`, `services/queue_host.py`, `services/queue_cancel.py` (phase 3) | The queue's one worker thread and its observable status (`is_alive`, `state`, `cooldown_until`, `current_run`, `current_step`, `between_runs_after`); restart recovery; the host that owns the run lock, the store, and the worker's lifecycle; cancelling an entry |
| `services/queue_hold.py`, `services/queue_park.py`, `services/queue_park_text.py` (phase 3) | The queue's hold, in memory and in the `settings` row; parking and unparking a running entry, and holding and releasing the queue; the words both front ends use for a park and its outcome |
| `services/queue_callers.py` (phase 3) | Keeping agents (the caller `mcp`) off the queue entries and holds people made: `check_entry_permitted`, and the words of a refusal |
| `services/api_rules.py`, `services/input_listing.py`, `services/queue_events.py` (phase 3) | The rules and limits every job the API runs or writes must meet; the input directory's images; turning the worker's transitions and job events into the (kind, data) shape an event sink takes |
| `state/audit.py` (phase 3) | `AuditRepository`: the `audit_log` table (schema 5) behind `GET /audit` |
| `server/` (phase 3) | `dtc serve`'s FastAPI app, bounded HTTP body gate, audit and safe error boundary, SSE watch, and gRPC monitoring service; see [Phase 3](phase-3/README.md) |
| `mcp_server/` (phase 3) | `dtc mcp`: tools, resources, and queue waiting over the HTTP API alone, on stdio or Streamable HTTP; see [Milestone 10](phase-3/milestone-10-mcp-server.md) and [Milestone 13](phase-3/milestone-13-mcp-over-http.md) |
| `cli/app.py` | Commands (`generate`, `validate-config`, `validate-job`, `import-history`, `tui`, `serve`, `mcp`, and the `queue` and `history` groups) and `CliServices` in Typer's context |

Services receive their runner and executable lookup as dependencies, so tests
never start a process.

**Process supervision.** The runner starts the CLI in its own process group and
reads stdout and stderr concurrently. `SIGHUP`, `SIGINT`, and `SIGTERM` send
`SIGTERM` to the group, wait `--shutdown-grace` (10 s), then `SIGKILL`.
`--timeout` uses the same sequence. Without `--output`, the child inherits the
terminal for inline preview.

**Exit codes.** 0 success; 1 run wrote no output, last-frame extraction
failed, or the state database or lock cannot be used; 2 invalid input; 75 run
lock held by another run; 124 timeout; 128+N stopped by signal N (130 for
Ctrl-C); otherwise the CLI's own code.

## Runtime ownership

`dtc serve` owns the run lock, state store, queue host, and the only worker
allowed to start `draw-things-cli`. It executes one entry at a time. Shutdown
stops the current child and releases the lock only after the worker has joined;
startup recovers entries left running by a previous server.

A queued job stores the exact job and base-configuration text plus its resolved
directories and cooldown policy. A queued one-off generation stores its
resolved request. Later file edits therefore do not change queued work.
Resuming creates a new entry at the next unfinished run with the original seed;
an interrupted run always restarts from its boundary.

The SQLite store keeps execution, run, queue, job-ID, audit, and preference
state. Public IDs such as `E0012`, `Q0007`, and `J0001` are stable and
monotonic. Retention prunes database history, not generated output; resumable
parked chains are retained while they are still needed.

## Interfaces

The CLI and TUI use the HTTP API for queue control. The TUI and
`dtc queue add --wait` observe live work over authenticated gRPC; MCP uses the
API's authenticated SSE watch. `dtc mcp` is an HTTP-only client of
`dtc serve` and is available over stdio or Streamable HTTP.

The TUI reads display data through services and renders typed events; it never
parses logs. The API and MCP layers translate requests and responses but keep
business rules in `services/`. The MCP package imports no other project
package and reaches the server only over HTTP.

Job-file creation, replacement, and recoverable deletion require
`dtc serve --allow-write`. Replacements use SHA-256 optimistic concurrency,
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
- [Phase 3](phase-3/README.md): API, queue, and MCP
