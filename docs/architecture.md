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
| `server/` (phase 3) | `dtc serve`'s FastAPI app, its bounded HTTP body gate, audit and safe error boundary, SSE watch, and gRPC monitoring service; see [below](#milestone-2-http-api-and-grpc-monitoring-done) and [Milestone 11](#milestone-11-safety-hardening-done) |
| `mcp_server/` (phase 3) | `dtc mcp`: the MCP server's tools, resources, and wait, over the HTTP API alone, on stdio or Streamable HTTP; see [Milestone 10](#milestone-10-mcp-server-done) and [Milestone 13](#milestone-13-mcp-over-streamable-http-done) |
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

## Phase 2: TUI and shared state (done)

Adds what every later front end needs, without changing the CLI's behavior.

- **Events.** `jobs/events.py` holds the typed events and `event_to_dict`;
  `JobExecutor.cancel()` stops a job from any thread through a `CancelToken`
  that ends the current run and any cooldown. The job's log lines, its
  manifest, and its log file are written from the same steps by `JobLogWriter`
  and `JobRecords`, which the executor calls itself, in a fixed order, so the
  log file starts before `JobStarted` and a manifest that cannot be written
  still fails the job. Child output feeds `RunOutput` events.
- **State store.** `state/` keeps SQLite execution history in `state/dtc.db`
  (schema 3, migrated by any open): every `run-job` and TUI run with its YAML
  text and resolved settings, as frozen `ExecutionRow` and `RunRow` values. Rows
  older than 14 days are pruned; output files never are. Each execution has an
  execution number and each job file name in `data/jobs/` a job number, from
  counters that only go up (`E0012`, `J0001`). The front end reserves the
  execution number (`ExecutionRecorder.reserve`) and passes it in
  `JobRunOptions.reserve_execution_id`; `jobs/` never imports `state/`.
  `dtc import-history` loads phase 1 manifests.
- **Running a job.** `services/job_runs.py`'s `JobRunSession` is the one place
  that takes the run lock, opens the store, closes what a crash left running,
  and runs the job with the recorder watching it. `run-job` and the TUI call
  it; the lock is `core/run_lock.py`'s `fcntl.flock`, which also names the
  running `draw-things-cli`, so a run refuses to start while one survives a
  `SIGKILLed` `dtc`. The holder's `record_child` reaches the runner as a
  `ChildStartCallback` (`JobRunOptions.on_child_start`), so no module state is
  involved. A second starter fails at once with exit code 75.
- **Configurations.** Base configurations are YAML in `data/params/`, read
  strictly; `draw-things-cli` gets JSON inline (`--config-json`), so no JSON
  file is written. Job files and the global configuration are read by the same
  strict reader. A `.json` file is refused by every command, and never touched.
- **Cooldown.** A frozen `CooldownPolicy` (`core/cooldown.py`) gives each wait
  through `wait_after(run_seconds)` (`auto`, the default: a share of the run just
  finished, within bounds; `manual`: fixed; `off`). `JobStarted` carries the
  policy; `CooldownStarted` carries the mode, the auto ratio, the run time the
  wait follows, and the bound that set it. The manifest and the execution's
  settings keep the resolved mapping.
- **Measured outputs.** After each successful run, `RunFinisher` measures the
  output (`jobs/media/info.py`) and puts its actual size and frame count on
  `RunFinished`, the manifest, and the `runs` table. A video job needs `ffprobe`
  (checked with `ffmpeg` before run 1).
- **TUI** (`tui/`, Textual). Textual code stays in `tui/`; the data it shows
  comes from `services/` (`JobCatalog`, `HistoryReader`, `StoreProvider`), whose
  errors `PaneHistory` turns into the messages a pane shows, since a failed
  Textual worker closes the app.

  | Module | Responsibility |
  |--------|----------------|
  | `app.py` | The app: settings, paths, data directory, executable, the job executor (built with `handle_signals=False`), the job worker, and the two-press Ctrl-C |
  | `signals.py`, `delete_dialog.py` | `SignalGuard` and `QuitPress`; `DeleteDialog`, the one dialog (Phase 3 Milestone 6) |
  | `screens.py`, `controller.py` | `MainScreen` (layout, events to panes) and `CommandController` (the `/` commands and their arguments) |
  | `panes/` | `StatusPane`, `CliPane`, `JobDefinitionPane`, `HistoryPane`, `ExecutionPane`, over `base.py` (the height rule for a table that scrolls sideways, and reads whose newest result wins) |
  | `text/` | Every text the TUI shows, by subject (`jobs`, `status`, `events`, `execution`, `prompts`, `arguments`, `history`); job text comes from `jobs/text.py` where the CLI prints the same |
  | `commands.py`, `widgets.py`, `job_watch.py` | The command table, `CommandInput`, `MessageLog`, and `SteadyText` (the text the one-second tick sets), and the file watcher |
  | `reader.py`, `job_sort.py`, `desktop.py` | `PaneHistory`, the kept sort, reveal in Finder and the clipboard |
  | `live_run.py`, `estimate.py` | `LiveRun`, the running job's state built from its events, and the run and job estimates |

  A job runs on a thread worker through `JobRunSession`. Its events reach the
  main thread through `App.post_message`, update the `LiveRun`, and are passed
  to the panes. The app registers `SIGHUP`, `SIGTERM`, and `SIGINT` on the
  asyncio loop and cancels any running job when it unmounts, so no
  `draw-things-cli` outlives it. Browsing writes nothing but a schema upgrade,
  the job IDs, and the kept sort.

## Phase 3: API and MCP for agents (in progress)

### Milestone 7: job file management (done)

- **Job files.** `server/routes_job_files.py` always provides authenticated draft validation and conditionally registers write routes when `ServerContext.allow_write` is on. `services/job_files_write.py` checks text size, the strict API filename/name relationship, parser and API rules, and input decoding before an atomic create or replacement. Writes share `submission_lock` with queue submission; replacement and deletion require SHA-256 optimistic concurrency and refuse queued or running paths. Replacements copy the old bytes to `data/jobs/.backups/<name>/`; deletions hard-link bytes into `data/jobs/.trash/`, so recovery is manual and no server action permanently deletes a job file. The `conflict` error code maps to HTTP 409.

### Milestone 1: queue and run manager (done)

- **Queue.** `state/queue.py`'s `QueueRepository` keeps the `queue` table
  (schema 4): one row per submission, with a public ID (`Q0007`, from a
  `queue` counter beside `execution` and `job`) and no position column, since
  nothing reorders it — order is by ID. `services/queue_submit.py`'s
  `submit_job` validates a job file exactly as running it would
  (`load_job_text`), then stores its snapshot: the job's exact text, its base
  configuration's exact text (`jobs/parsing.py`'s `load_job_text` gained
  `base_config_text` to parse it from the snapshot instead of `data/params/`),
  and the global configuration's `input_directory`, `output_directory`, and
  cooldown default. Editing or deleting any of those afterwards changes
  nothing about what runs. A queue entry links to its execution, and to the
  entry it resumes, by their public numbers, never a row id: a plain number
  (unlike a foreign key) survives the row it names being pruned, which is how
  a resume tells "its ancestor was pruned" apart from "never ran".
- **Worker.** `services/queue_worker.py`'s `QueueWorker` runs one entry at a
  time on its own thread, through `JobRunSession`: it claims the oldest
  `queued` entry in one transaction (so a concurrent cancel cannot interleave
  with the claim), parses its snapshot, and runs it. A cancel that lands
  between the claim and the executor's `begin()` is kept and applied at
  `JobStarted` (a shutdown too, so it also stops a job caught in that same
  window). `JobFinished`'s status maps to the entry's: `succeeded` and
  `failed` as reported; `interrupted` reads `cancelled` when the
  worker itself asked for the stop, `interrupted` when the host's shutdown
  did, and `failed` otherwise, since nothing the worker did caused it (in
  practice a run killed from outside never reaches `JobExecutor` as
  `interrupted` in the first place: with no signal of ours requested, it
  looks like an ordinary failed exit code, so it is already `failed` before
  the entry is even considered; this branch exists for the milestone's own
  stated case regardless). The execution in the history keeps its own
  `interrupted` for any stopped run, Ctrl-C included, unaffected by which of
  these applies to the entry. After a job that
  succeeded, when another entry is already queued, the worker waits its
  resolved cooldown (applied to the last run's seconds) before starting the
  next one; the wait ends at once on a stop or once nothing is queued any
  more. An error that escapes a job (a collaborator's bug, not a normal run
  failure) fails only that entry, with the message, and the worker goes on.
- **Resume.** `services/queue_resume.py`'s `resume_entry` walks an entry's own
  chain of resumes back to the last succeeded run, refusing (naming the
  reason or path) when none ever succeeded, the file it would start from is
  gone, the job's own first input is gone, or an ancestor's execution was
  pruned; a resume can only be accepted once. It resolves the resume point
  (the first run, its input, and the original seed) once, and stores it on
  the new entry, so pruning an ancestor afterward cannot invalidate an
  already-accepted resume. `JobExecutor` starts a chain at run *k*
  (`JobRunOptions.resume`, a `ResumePoint`): it skips run 1's resized copy of
  the input and keeps the given seed instead of drawing one. A resumed
  execution's manifest holds only the runs it made, from position 0, so
  `import-history` numbers them from the manifest's own `first_run`, not from
  1.
- **Restart recovery and shutdown.** `services/queue_recovery.py`'s
  `recover_queue` runs before the worker starts, while
  `services/queue_host.py`'s `QueueHost` holds the run lock and the state
  store (opened `WRITE`, so nothing is pruned before recovery can read it):
  `sweep_interrupted()` first, then a `running` entry takes its linked
  execution's real status (`interrupted` after the sweep, or whatever it
  already was), and a `running` entry with no linked execution at all (the
  crash landed before `JobStarted`) goes back to `queued`. `queued` entries
  are untouched. `QueueHost.stop()` cancels the running job at once, ends any
  between-jobs wait, joins the worker thread, and only then releases the
  lock, so a second starter never races this shutdown's own recovery.
- **Busy message.** While the queue host holds the run lock, `RunLock`'s busy
  message (`core/run_lock.py`) names the server and how to free it instead of
  `Another run is in progress`, for `run-job` and the TUI alike.
- **Job log scoping.** A job's log file (`jobs/records.py`) now holds only
  that job's own lines: `JobRecords.open` tags every message logged while the
  job runs (`logger.contextualize(dtc_job=True)`), and the log sink filters
  on that tag, so a server's own lines (API requests, once Milestone 2 adds
  them) never reach it.
- Out of scope here, and still planned: the HTTP and MCP interfaces, the
  `serve` command that starts the worker, `dtc queue` and the TUI's Queue
  widget, submission rules and limits, and the event backlog for a stream.

### Milestone 2: HTTP API and gRPC monitoring (done)

- **`server/`.** `dtc serve` (`server/serve.py`) runs a FastAPI app
  (`server/app.py`, `create_app`) and a `grpc.aio.server()`
  (`server/grpc_service.py`'s `MonitorServicer`, generated from
  `server/proto/monitor.proto`) in one process, sharing the run lock, the
  state store, and the queue worker (`services/queue_host.py`'s
  `QueueHost`) for its whole lifetime. Both bind to `--host` (loopback
  unless `--allow-remote-bind`) on their own ports (`--port`,
  `--grpc-port`); an HTTP request whose `Host` header names neither loopback
  nor the bound address is refused with `invalid_input`, but status 400, an
  explicit exception to the error table's usual 422 for that code
  (`server/host_check.py`, ASGI middleware installed by `create_app`)
  against DNS rebinding — gRPC has no equivalent gap, since a channel dials
  the bound address directly. `QueueHost.start(start_worker=False)` takes
  the lock, opens the store, and recovers, but leaves the worker thread
  unstarted; `_serve_async` binds both ports first (for HTTP, every address
  `getaddrinfo` resolves the host to, with `SO_REUSEADDR` and `IPV6_V6ONLY`
  as asyncio's own `loop.create_server` sets them, handed to uvicorn's
  private `Server._serve(sockets=...)`; `add_insecure_port` for gRPC, as
  before; each an `InputError` naming its own flag if the port is taken) and
  only then calls `host.start_worker()`, so a bad port never lets the worker
  claim and then abandon a queued entry.
- **Authentication.** A 32-byte hex token in `config/server-token`
  (`ProjectPaths.server_token`, mode 0600, created on first start;
  `server/token_file.py`), compared with `hmac.compare_digest`
  (`server/token_file.py`'s `token_matches`). HTTP: `require_auth`
  (`server/dependencies.py`), every router but `health_router`. gRPC:
  `TokenAuthInterceptor` (`server/grpc_auth.py`), ending an unauthenticated
  call `UNAUTHENTICATED`.
- **Routes** (`server/routes_health.py`, `routes_jobs.py`, `routes_inputs.py`,
  `routes_executions.py`, `routes_queue.py`, `routes_audit.py`), each a
  small `APIRouter`. Responses are plain dicts from `server/serializers.py`,
  not Pydantic models. A `{job}` path segment resolves through
  `server/job_reference.py`'s `resolve_job_reference` (`JobCatalog.find`,
  never a joined path; a symbolic link reads as invalid). Pagination
  (`server/pagination.py`) is an opaque base64-encoded offset, so the scheme
  can change later without a contract break; `GET /queue` pages only its
  finished entries this way (newest first, after the queued and running ones
  in full on the first page), since those alone are unbounded once
  `history_retention_days: 0`, through `QueueRepository.list_active` and
  `.list_finished`. `server/caller.py` resolves `X-Dtc-Caller` (`cli`,
  `tui`, `mcp`, or the `api` default) against a known set; each audited
  route in `routes_queue.py` reads the raw header and resolves it itself,
  inside its own `audited()` block, rather than through a shared dependency
  that would fail ahead of that block and leave an unknown value
  unaudited. `GET /inputs` reads through `services/input_listing.py`'s
  `InputCatalog`, one per `ServerContext`, which re-reads a file's header
  only when its modification time or size changed, instead of on every
  page. `server/errors.py` maps `DtcError` codes to HTTP statuses in one
  table, as `EXIT_CODES_BY_ERROR_CODE` does to exit codes, and installs a
  FastAPI exception handler for it.
- **Rules and limits.** `services/api_rules.py`'s `check_api_rules` (needed
  by `POST /queue` and, from Milestone 07, every write) requires
  `run_timeout_seconds`, checks the input and output directories, and
  enforces `GlobalConfig.api_limits` (`core/global_config.py`'s
  `ApiLimits`): `max_queued_jobs`, `max_job_runs`, `max_job_seconds` (worst
  case: runs × `run_timeout_seconds`, plus the longest cooldown wait between
  them; from Milestone 09, plus each video run's `color_drift` limit and each
  correcting run's correction limit, `JobDefinition.drift_seconds` and
  `correction_seconds`), `max_job_file_bytes`. A resume counts only the runs it has left.
- **Audit log.** `state/audit.py`'s `AuditRepository` (schema 5's
  `audit_log` table) records every submit, cancel, and resume, refused ones
  included, through `server/audit.py`'s `audited` context manager, called
  from the queue routes after authentication (so an unauthenticated request
  leaves no row). Two refusals happen before a route's own `audited()` block
  ever opens and are recorded outside it, with the documented default caller
  `api` (the value that failed is not trustworthy enough to store in the
  caller column itself): an unknown `X-Dtc-Caller`, raised by the route
  itself right after entering `audited()` (see above), and a `POST
  /v1/queue` body FastAPI's own validation refuses, recorded directly in
  `errors.py`'s `RequestValidationError` handler, which re-checks the bearer
  token itself (`dependencies.is_authenticated`) rather than assume body
  validation always runs after `require_auth`.
- **Events.** `server/event_backlog.py`'s `EventBacklog` is a bounded
  (2000), thread-safe deque; IDs are seeded from wall-clock milliseconds, so
  an ID from a previous server run naturally reads as too old (`since`
  returns `None`, telling `WatchEvents` to send `Reset`) with no separate
  run token needed. `services/queue_worker.py`'s `QueueWorker` takes an
  optional `on_event` sink (`services/queue_events.py`'s
  `QueueEventPublisher`), forwarded from `QueueHost`, publishing Phase 2 job
  events and the queue's own (`queue_entry_changed`, `queue_wait_started`,
  `queue_wait_ended`) into it. A submission's or a resume's own 'queued'
  change, and a queued entry's own 'cancelled', are published by the worker
  too, through `enqueue` and `cancel_queued` (`routes_queue.py` passes
  `context.worker.enqueue` into `submit_job`/`resume_entry`, and
  `queue_cancel.py`'s `cancel_entry` calls `worker.cancel_queued`), never by
  a route directly: both run the actual DB write and the publish under the
  same lock `_claim_and_run_one` claims with (`services/queue_claim_gate.py`'s
  `QueueClaimGate`, extracted to keep `QueueWorker` under the class size
  limit), so a client watching events can never see a claim's 'running'
  published before an earlier 'queued' for the same entry.
  `MonitorServicer.WatchEvents` streams from the backlog; `WatchQueueEntry`
  polls the store for one entry's change, sending a message only when a
  field other than `current_run_elapsed_seconds` changes.
- **Shutdown.** uvicorn owns SIGINT/SIGTERM (`Server.capture_signals()`);
  the queue worker's executor is built with `handle_signals=False`. The
  gRPC server must start *inside* `capture_signals()`'s `with` block, not
  before it — starting it first was found, empirically, to leave SIGTERM at
  its default disposition for the rest of the process's life (grpc's C core
  appears to reset it during its own startup), so no cleanup ever ran.
  `QueueHost.stop()` (releasing the run lock) must run *inside* that same
  `with` block too: `capture_signals()`'s own `finally` re-raises the
  caught signal, at its restored default disposition, once the block exits,
  which can kill the process before any cleanup written in an outer
  `finally` (after `asyncio.run()` returns) gets to run. `server/serve.py`'s
  `_serve_async` does both: enters `capture_signals()`, starts the gRPC
  server, awaits uvicorn's private `Server._serve()` (skipping the public
  `serve()`'s own nested `capture_signals()`), then stops the gRPC server
  and the queue host in its own `finally`, all still inside the `with`
  block.
- Out of scope here, and still planned: job file management behind a write
  flag, the MCP server, and the queue and Queue widget for people.

### Milestone 4: TUI verbose mode (done)

- `tui/preferences.py`: `VerboseLevel` (`high`, `medium`, `low`), the saved level in `config/tui-preferences.yaml`
  (`ProjectPaths.tui_preferences`, gitignored; a missing or malformed file is `high`, never an error), and
  `wants_output(level)`, false only for `low`.
- `tui/feed.py` opens `WatchEvents` with `include_output=wants_output(level)`. `DrawThingsApp.set_verbose_level`
  restarts the feed worker only when a switch crosses the low boundary; it resumes from the last event ID, and a
  backlog that cannot explain the gap is the ordinary `Reset` and reseed.
- `tui/running_job.py`'s `RunningJobView` (held by `MainScreen` as `running`) applies the job's events to the panes
  and owns `tick(force)`: at low, an unforced tick (the screen's one-second timer) renders the Status widget, run
  line, and status line only once `LOW_STATUS_REFRESH_SECONDS` after the last render; every event-driven caller
  forces it. All three are `tui/widgets.py`'s `SteadyText`: an unchanged text draws nothing, and a changed one is drawn
  without a layout pass (`styles.tcss` fixes their heights), so a tick sends the terminal only the widgets that changed.
- `tui/panes/cli_output.py`'s `CliPane.write_output` skips lines for good at low, and at medium once a run's first
  `MEDIUM_OUTPUT_WINDOW_SECONDS` have passed (or the minute since `/verbose medium` was typed, `LiveRun.output_window_start`).

### Milestone 5: park and hold (done)

- **States.** `JobStatus.PARKED` (`jobs/events.py`) and `QueueState.PARKED` (`state/queue.py`, in `FINISHED_STATES`) name a
  job that ended at a run boundary because it was parked; runs are never parked. The state columns are plain `TEXT`,
  so no migration. `EXIT_PARKED = 3` (`core/exit_codes.py`) is the job's exit code and `dtc queue add --wait`'s.
  `parked` is resumable (`services/queue_resume.py`), a history filter (`services/history.py`), recovered from a
  parked execution (`services/queue_recovery.py`), and listed with the finished entries (`list_finished` builds its
  clause from `FINISHED_STATES`).
- **The executor's park request and its commit point.** `CancelToken` (`core/process/signals.py`) keeps a park flag
  apart from the stop signal: `park()` sets it and writes the wake-up byte, so a cooldown between runs ends, and never
  reaches the runner; `RunFinisher.finish` reads only `requested`, so a park never turns a failed last-frame extraction
  into `interrupted`. `JobExecutor._run_runs` commits with `take_park()` at a run boundary after a run this execution
  made: after a succeeded run that is not the last (after its finish and `RunFinished`, so its last frame is written),
  after a cooldown the park ended, and at the top of a later run. A stop requested first wins. Once taken, `unpark()`
  is refused until the next job's `begin()`; a park withdrawn before it was taken leaves the rest of the cooldown to
  wait out. `CancelToken.park_count` tells such a wait from one the cooldown ended early on its own, which is never
  waited again. `JobFinished` then reads `parked`, exit code 3, no signal.
- **The worker's reservation and hold.** `QueueWorker` keeps the running entry's park reservation in memory
  (`_park_pending`) and applies it to the executor only once `JobStarted` has been seen, under `_state_lock`, so an
  unpark before then withdraws it in the worker alone and never lands between the guard's read and its call. The entry
  is marked finished and `_job_finished` set under the same lock, so a park that loses the race with the job's own end
  is refused rather than holding the queue. A park saves its hold before it makes the reservation, and an unpark whose
  hold cannot be released keeps the reservation, so a reservation and its hold never part. `services/queue_hold.py`'s `QueueHold` keeps the hold in memory and in the
  `settings` table's `queue_hold` row (`{"since", "by"}`; a row that cannot be read counts as held, with a warning). A
  reservation holds the queue by its entry, and unpark releases only a hold that entry's reservation made;
  `/queue hold` makes a hold its own. While held, the claim finds nothing (checked under `_state_lock`), the
  between-jobs wait ends at once and is forgotten, and `state()` reads `held` while no job runs. A release wakes the
  worker, which claims at once; one that lands once the claimed job has ended drops that job's between-jobs cooldown
  (`_release_skips_wait`). The claim publishes its `running` under `_state_lock`, as the park and hold events are, so
  a park made just after a claim never reaches a front end before it. `services/queue_park.py`'s `park_entry` and `unpark_entry` refuse with
  `ParkRefusedError` (`invalid_state`), naming the reason.
- **Retention.** `Store.prune` reads the entries to keep once, before either prune (`QueueRepository.kept_parked`: each
  parked entry and the chain of resumes below it, walked by a recursive query, until one of those resumes has a
  succeeded run), and both `ExecutionRepository.prune` and `QueueRepository.prune` skip them and their executions.
  Keeping the resumes in between lets the newest one still walk back to the parked entry, and keeps the parked entry
  from reading as never resumed. Reading it in each prune would let the execution prune delete the resume's runs first,
  and the chain would never be let go.
- **Events and the API.** `services/queue_events.py` publishes `queue_park_changed` (`queue_id`, `park_requested`),
  `queue_held` (`since`, `by`), and `queue_released`; `queue_entry_changed` still means only a change of state. The
  routes add `POST /v1/queue/{queue_id}/park` and `/unpark` (the entry as `GET /v1/queue/{queue_id}` returns it,
  with `between_runs_after_run` from `WorkerStatus`) and `POST /v1/queue/hold` and `/release` (the hold and
  `changed`), all audited; entries carry `park_requested`, and the queue reads carry `held`, `held_since`, and
  `held_by`. `QueueEntrySnapshot` gains `park_requested` and `queue_held`, always set; `WatchQueueEntry` reads the reservation
  before the row, so a job that ends between the two reads never shows `running` without its reservation. `dtc serve` logs a saved hold
  at startup.
- **Front ends.** `dtc queue park|unpark|hold|release`, and `list`, `show`, and `add --wait` show `parking` and the
  hold. Both front ends word a park from `services/queue_park_text.py` (`park_point` decides where it takes effect,
  and that on the job's last run it finishes instead), and read a response's hold with `HoldState.from_body`. The TUI's `LiveRun.park_requested` gives the `parking` phase (a stop still wins), set from its own park and
  unpark responses, `queue_park_changed`, and the reseed's `GET /v1/queue/{id}`. The app keeps the hold from every
  queue read (`QueueSnapshot.hold` from the store while the server is down) for the Queue widget's title and the
  Status widget.

### Milestone 6: delete executions (done)

- **The delete path.** The TUI (`d`, `/delete`) and `dtc history delete` send `POST /v1/executions/delete`
  (`server/routes_executions.py`), 1 to 200 IDs a request. The route takes `ServerContext.submission_lock` first, as a
  resume holds it around the whole of `resume_entry`, so a deletion never lands between a resume's chain walk and its
  insert. It then calls `QueueWorker.delete_executions`, which `QueueClaimGate` runs under the worker's `_state_lock`, so
  no claim, job start, or submission lands between the check and the delete.
  `services/history_delete.py`'s `delete_executions` reads `QueueRepository.resume_links()` (every entry with its linked
  execution's existence, run count, and last succeeded run, in one query), works out the in-use executions and the
  resumes each deletion ends, and calls `ExecutionRepository.delete`: one `BEGIN IMMEDIATE` transaction that skips a
  missing, running, or in-use execution and deletes the rest (`ON DELETE CASCADE` takes the runs). The log and manifest
  go after the lock is released (`Store.delete_execution_files`, `_delete_log` and `_delete_manifest`: a regular `.log`
  or `.json` file only, never a symbolic link). A manifest that stays is reported. `"dry_run": true` runs the same code
  under the same locks and deletes nothing.
- **Refusals and the resume warning.** `services/resume_chain.py`'s `walk_chain` is the one definition of the executions
  a resume chain reads: from the entry's own execution back through its ancestors to the first with a succeeded run.
  `queue_resume._resolve_chain` walks full rows; `history_delete` walks the resume links. A queued or running entry's
  own execution, its `resumes_execution`, and every execution its chain reads are in use. A resume ends when an entry a
  resume would accept now (a resumable state, not yet resumed, a succeeded run in its chain, runs left) reads the
  execution; the output file is not checked. A resume of a deleted execution is refused with "was pruned or deleted".
- **Retention.** `kept_parked` stops keeping a parked chain once a finished entry of it links an execution with no row;
  a running entry does not count, since it links its number before `JobStarted` creates the row.
- **The API and the audit log.** Each ID gets its own `delete_execution` row (`ok`, `invalid_state`, `not_found`); a
  request refused as a whole gets one row with no target, and a dry run none. `server/caller.py`'s `audit_caller` is
  shared by both routers, and `errors.py`'s `AUDITED_BODY_ACTIONS` records a body FastAPI refuses for both audited POST
  bodies. `GET /v1/executions` gains `name` (the store's `name_contains`).
- **Front ends.** `cli/history_app.py` reads a filtered selection in full, lists it from a dry run, asks, and deletes;
  it shares `cli/api_client.py` with `dtc queue`. In the TUI, `HistoryPane` keeps marks as a set of execution numbers,
  shown as an `*` before the ID, and `remove_rows` drops deleted rows and reads again. `tui/deletion.py`'s `DeleteFlow`
  reads the selection from the store (`HistoryReader.every`, `by_numbers`), sends a dry run first (the server's own
  refusals and warnings, and proof the server and token work before any dialog opens), then pushes `DeleteDialog` for
  each execution with `push_screen_wait` on a worker, sending one request per Delete and batches for Delete all.

### Milestone 10: MCP server (done)

- **The API's additions.** `GET /v1/executions/{id}?brief=1` (`serializers.execution_detail(brief=True)`: no run's
  command, each check as its stage and verdict) and `GET /v1/executions/{id}/runs/{run}` (one `run_summary`, matched by
  the run's number as text, so no number is too long to parse); `GET /v1/jobs/{job}?brief=1`, without a valid job's
  text. `dependencies.get_brief` refuses another value of `brief`, as `overwrite` is.
- **The SSE watch.** `GET /v1/queue/{id}/watch` (`routes_queue.watch_queue_entry`) is a plain route that answers 404
  first and returns an `EventSourceResponse` over bytes it formats with `fastapi.sse.format_sse_event`: `event:
  snapshot` and the snapshot as JSON, and `: keep-alive` after `watch_keepalive_seconds` (15) without one. It reads each
  snapshot off the event loop every `watch_poll_seconds` (0.5). `server/entry_snapshot.py` is the one snapshot and the
  one rule for when one is sent, which `grpc_service.WatchQueueEntry` reads too (a field left unset when None);
  `run_progress` is the current run, step, and `cooldown_until` it shares with `GET /v1/queue/{id}`. uvicorn waits for
  open connections when it stops, so `serve.UvicornServer.shutdown` sets `ServerContext.stopping` before it waits, and
  each watch ends at its next poll.
- **People's entries and holds.** Schema 9 adds `queue.submitted_by`, the caller of the submission or resume that made
  the entry (null before it). The hold's saved setting gains `caller` (`HoldState.caller`); a hold saved without one is
  a person's, not damaged. `QueueHold._held_again` is the one rule for a hold on a held queue: a direct hold makes a
  park's hold its own, a person's makes an agent's the person's, and an agent's never takes a person's. The worker
  calls it on every park, so a person's park on an agent's hold takes it. `services/queue_callers.py`'s
  `check_entry_permitted` refuses an agent (`mcp`) a person's entry in `cancel_entry`, `park_entry`, `unpark_entry`,
  and `resume_entry`; `QueueHold._release` refuses an agent a person's hold under the hold's own lock, for a release
  and an unpark alike. The refusal is `NotPermittedError`, 403 `not_permitted`, audited as any refusal.
- **`mcp_server/`.** Imports nothing else from the package and no `grpc` (`tests/test_architecture.py`); `cli/app.py`
  checks `--server-url`, moves logging to stderr, and imports `mcp_server.app` only inside `dtc mcp`.
  - `app.py`: the SDK's low-level `Server`, not `MCPServer`, for hand-written schemas, a list that changes at runtime,
    and error results carrying the API's error shape. `McpApp` holds the API client, the last read of `allow_write`,
    and each connection's state from the server's lifespan (an older client's session). The tool list reads
    `GET /v1/capabilities`; a call reads it again when the last read is over 5 seconds old or failed, and
    `delete_executions` always. A read that differs from the list last given publishes `ToolsListChanged` on an
    `InMemorySubscriptionBus` (served by the SDK's `ListenHandler` on `subscriptions/listen`) and sends
    `notifications/tools/list_changed` on each older session; `tools/list` and `resources/list` carry a 5-second
    cache hint. `_Server` declares `tools.listChanged` to an older client on every transport. Resources are the job
    files, `job://{job}`, through the API.
  - `api.py`: one `httpx.AsyncClient`, every request with the bearer token and `X-Dtc-Caller: mcp`; the token read as
    `core/client_config.read_client_token` reads it (a tested copy) and read again after any failure; `ApiRequest`,
    built whole before anything is sent; `path_segment`, which refuses an empty, dots-only, or slashed argument (an
    ASGI server decodes `%2F` before it routes) and percent-encodes the rest; the SSE stream; and the errors of its
    own, `server_unreachable`, `server_timeout` (sent, not answered: a write may have taken effect), and
    `unauthorized`. The client opens with the first request and closes with the last connection.
  - `tools.py`: each tool's name, description, input schema (with the API's names and bounds, and
    `additionalProperties: false`), annotations, and request; `check_arguments`, which refuses an unknown, missing, or
    wrong argument naming it, before any request.
  - `watch.py`: `get_queue_entry`'s `wait_seconds`. It reads the entry, answers a finished one at once (a tested copy
    of `FINISHED_STATES`), and otherwise reads the SSE watch, without an SSE library, until a snapshot changes a field
    an agent acts on or ends a run (`acted_on`: `current_run` leaving a number, for null or straight for the next),
    compared with the one before it and the first with the entry as read, or the time is up, then reads the entry
    again and adds `changed`. A watch that ends first answers as a read of the entry does (`not_found` once it is
    gone), else `server_unreachable`.
    Progress every 15 seconds, at most 1500 seconds without a progress token, and 60 seconds of silence (the stream's
    read timeout) count as a dropped watch (`WaitTimes`, which tests shorten). A cancelled call cancels the stream.
- **Registration.** `.mcp.json` at the project root registers `uv run dtc mcp` with Claude Code.

### Milestone 13: MCP over Streamable HTTP (done)

- **Why.** `dtc mcp` was stdio alone, so a client that cannot start it here (OpenClaw in a virtual machine) had no way
  in: `dtc serve`'s port is the REST API, 404 for every path but `/v1/...`.
- **`mcp_server/http.py`.** `http_app` is the SDK's `Server.streamable_http_app` (`/mcp`) behind `BearerAuth`, a pure
  ASGI check of `Authorization: Bearer` against the token file, read for each request and compared with
  `hmac.compare_digest`; every other request is 401 `unauthorized` with no reason (an unreadable file is logged, not
  answered). `serve_http` binds the socket itself (`BindError` for a taken port, which `cli/app.py` makes exit 2) and
  runs uvicorn with `ws="none"`. `McpBodyLimit` caps authenticated requests at 8 MiB before SDK parsing. The SDK's
  `Host` and `Origin` check stays on a loopback bind and is off beyond it.
- **Sessions.** The SDK's default, stateful sessions (`Mcp-Session-Id`).
- **`cli/app.py`.** `dtc mcp` takes `--transport`, `--host`, `--port`, and `--allow-remote-bind`; `mcp_server.app`
  gains `run_http` and re-exports `BindError`, the only names `cli/app.py` imports beyond `run`.

### Milestone 11: safety hardening (done)

- `jobs/parsing.py` returns typed input errors and rejects NUL, cyclic aliases, and paths outside the configured
  input and output roots before touching them. Existing job and named base-configuration files have read bounds.
  `JobCatalog` and named configuration lookup refuse symbolic links in files and directory ancestors.
- `server/body_limit.py` bounds each HTTP request before routing, including chunked bodies, with authenticated
  pre-route write refusals audited. `server/audit.py` records a canonical target only when known and records unexpected
  failures as `internal_error`; `server/errors.py` returns a generic, stable error shape and logs exception types and
  frames without message text or locals. Blocking job-file work and gRPC snapshots run off the event loop.
- `cli` and `tui` validate queue IDs before making URL paths. Both MCP transports keep the same tool rules; the HTTP
  listener has its own 8 MiB body cap after bearer authentication. The security and documentation checks cover these
  boundaries.

Milestone 12 will move direct `dtc generate` onto the queue. Until then it is
the one command that invokes `draw-things-cli` itself, under the shared run
lock.

## Rules across phases

- No source in the project root; each front end gets its own subpackage.
- Front ends call services and observe events; they do not parse logs.
- One run at a time, machine-wide. No parallel generation.
- Never edited by any interface: `data/params/*.json`, `data/params/*.yaml`, `config/global-config.yaml`.
- No credential (`--api-key`, `--remote-shared-secret`) reaches events, the
  state store, logs, or API responses.
- Agents (phase 3) can express only what a job file can express, within
  `data/jobs/`, the configured input directory, and the output directory.
