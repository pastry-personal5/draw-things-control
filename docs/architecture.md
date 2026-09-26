# Architecture

`draw-things-control` wraps the locally installed `draw-things-cli`. One core
runs generations; front ends (CLI, TUI, API, MCP) are thin layers over it.
Only one `draw-things-cli` runs at a time on the machine.

## Overview

```
Phase 1 (done)       Phase 2 (in progress)   Phase 3 (planned)
CLI ─────────┐       TUI ────────┐          MCP server ─▶ HTTP API ┐
             ▼                   ▼                                 ▼
        JobService ◀── events, cancel() ──────────────────── queue worker
             │                   │                                 │
             ▼                   ▼                                 ▼
   DrawThingsProcessRunner   state store (SQLite)   run lock (flock)
             │
             ▼
      draw-things-cli
```

## Source layout

Everything is one package, `src/draw_things_control/`; nothing lives in the
project root but configuration, data, docs, and tests. Dependencies point one
way, from front ends down to `core`.

```
src/draw_things_control/
├── core/        # runner, arguments, generation, configuration      (phase 1)
├── jobs/        # job definition and service, manifests, inputs     (phase 1; events phase 2, queue phase 3)
├── cli/         # Typer app: the `dtc` command                      (phase 1)
├── state/       # SQLite store and recorder                         (phase 2)
├── tui/         # Textual app                                       (phase 2)
├── server/      # HTTP API and queue worker                         (phase 3)
└── mcp_server/  # MCP client of the HTTP API                        (phase 3)
tests/           # mirrors the package: tests/core, tests/jobs, tests/cli, ...
```

Import direction: `cli`, `tui`, `server` -> `state` -> `jobs` -> `core`;
`mcp_server` -> `server` over HTTP only. Front ends never import each other,
except that the `dtc tui` command in `cli/app.py` starts the TUI app.
Launch with `dtc` or `python -m draw_things_control`.

Phase plans: [1](archive/phase-1/README.md), [2](phase-2/README.md),
[3](phase-3/README.md). Each phase reuses the layers below it unchanged.

## Phase 1: core and CLI (done)

| Module | Responsibility |
|--------|----------------|
| `cli/app.py` | Commands (`generate`, `validate-config`, `validate-job`, `run-job`) and wiring |
| `core/generation_service.py` | `generate` use case: validate, resolve, preview or run |
| `core/draw_things_arguments.py` | Validated options and argument-vector building (no shell); `command_settings` reads a saved command's model, refiner, CFG, shift, and steps back (phase 2); one ordered flag table (`GENERATE_FLAGS`) drives the builder, the parser, and redaction |
| `core/numbers.py` | Numbers read from a command, `--config-json`, a manifest, or `ffprobe`: `setting_number` and `positive_whole` (phase 2) |
| `core/draw_things_runner.py` | Process group, signals, timeout, graceful-then-forced shutdown |
| `core/process_output.py` | Classify and log child output; strip terminal codes; structured progress (step counter and percent) |
| `core/global_config.py` | Global config loading; `CooldownPolicy`, the `cooldown` mapping shared by the global configuration and jobs |
| `core/configuration.py`, `core/generation_config.py` | Configuration files (YAML or JSON, phase 2), `data/params/` lookup and merging |
| `jobs/job_service.py` | `run-job` use case: chain a job's runs, cooldown |
| `jobs/job_definition.py` | Job file loading and validation |
| `jobs/job_report.py` | Reading jobs and the text `validate-job` and `run-job --dry-run` print, shared by the CLI and the TUI (phase 2) |
| `jobs/input_size.py`, `jobs/input_resize.py` | Input image check and resize |
| `jobs/output_naming.py`, `jobs/frame_extraction.py` | Output names; last frames via `ffmpeg`, labeled sRGB; finding `ffprobe` |
| `jobs/media_info.py` | Measures an output's actual size and frame count: `ffprobe` for a video, the header for a PNG (phase 2) |
| `jobs/video_color.py` | Adds a `colr` color-tag box to a finished video, without touching frames or timing |
| `jobs/job_manifest.py`, `jobs/job_log.py` | Per-job JSON manifest and log file |

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

## Phase 2: TUI and shared state (in progress)

Adds what every later front end needs, without changing the CLI's behavior:

- `jobs/job_events.py`: structured job events and `JobService.cancel()`. Events
  are additive: `JobService` still writes the log lines. Child output feeds
  `RunOutput` events through `OutputProcessor`'s callback.
- `state/store.py`, `state/recorder.py`, `state/history_import.py` (Milestone
  02, done): SQLite execution history (standard library) in `state/dtc.db`,
  recording every `run-job` run with its YAML text and resolved settings. Rows
  older than 14 days are pruned; output files never are. `dtc import-history`
  loads phase 1 manifests.
- `core/run_lock.py` (Milestone 02, done): `fcntl.flock` on `state/run.lock`
  taken by `run-job`, `generate`, and the TUI. A second starter fails
  immediately with exit code 75; nothing queues silently. The file also names
  the running `draw-things-cli`, so a run refuses to start while one survives a
  `SIGKILLed` `dtc`. The holder passes its `record_child` down as a
  `ChildStartCallback` (`JobService.run(on_child_start=)`,
  `GenerationService.execute(on_start=)`, then the `RunnerFactory`'s
  `on_start`), so no module state is involved.
- YAML base configurations (Milestone 06, done): `core/configuration.py`
  picks the parser by extension and reads YAML strictly through a
  `yaml.SafeLoader` subclass; `core/generation_config.py` requires a job's
  `config_file` to be YAML. `draw-things-cli` gets JSON inline, so no JSON
  file is written:

  | File | Format | Written by | Read by |
  |------|--------|------------|---------|
  | `data/params/<name>.yaml` (or `.yml`) | YAML | a person | `dtc` (jobs, `generate`, `validate-config`) |
  | `data/params/<name>.json` | JSON | a person (phase 1) | `generate --config-file` and `validate-config` only |
  | the `--config-json` value | JSON | `dtc` (jobs, and `generate` with a YAML file) | `draw-things-cli` |
- Automatic cooldown (Milestone 07, done): a frozen `CooldownPolicy` in
  `core/global_config.py`, parsed by `parse_cooldown` from the `cooldown`
  mapping of the global configuration or a job, gives each wait through
  `wait_after(run_seconds)` (`auto`, the default: a share of the run just
  finished, within bounds; `manual`: fixed; `off`). `JobService` asks it after
  each successful run but the last. `JobStarted` carries the policy;
  `CooldownStarted` carries the `mode`, the auto `ratio`, the `run_seconds` the
  wait follows, and the `bound` that set it, if any. The manifest and the
  execution's `settings` keep the resolved mapping as `cooldown`, and
  `cooldown_seconds` holds only a manual wait (0 for `off`, null for `auto`).
- Measured outputs (Milestone 08, done): after each successful run,
  `JobService` measures the output through an injected `output_measurer`
  (`jobs/media_info.py` in the CLI and TUI) and puts the actual width,
  height, and frame count on `RunFinished` (`output_width`, `output_height`,
  `output_frames`), the manifest's `RunRecord`, and three `runs` columns
  added by state store schema version 2. A video job needs `ffprobe`
  (`require_ffprobe`, checked with `ffmpeg` before run 1). The store migrates
  on any open, a browsing one too: an upgrade is the one write a read-only
  screen may make.
- `tui/` (Textual, Milestones 03 to 05, 08, and 09 done). Textual code stays in the
  modules that need it; the rest are plain functions that run on worker
  threads and are tested without an app.

  | Module | Responsibility |
  |--------|----------------|
  | `app.py` | The app: its collaborators (settings, data directory, executable, shutdown grace, a `JobService` built with `handle_signals=False`), the job worker, signals, and the two-press Ctrl-C |
  | `screens.py` | `MainScreen` (layout, `/` command dispatch, wiring job events to the panes) and the confirmation dialog |
  | `panes.py` | `StatusPane` (the job at a glance), `CliPane` (run line and output), `HistoryPane` (paging, filters, polling, in-place row updates, the other-process lock check), and `ExecutionPane` (the execution under the cursor, its successful runs, selection, and reveal), each owning its state |
  | `widgets.py` | `CommandInput` (completion, recall) and `MessageLog` |
  | `commands.py` | The command table (including the `/get` and `/describe` forms): parsing, usage, help, completion |
  | `history.py` | `HistoryReader` (one store, never raises, never creates or prunes the database), output paths, reveal |
  | `job_files.py` | Reading and planning the job files in the data directory |
  | `text.py` | Every text the TUI shows, from `jobs/job_report.py` where the CLI prints the same |
  | `live_run.py` | `LiveRun`, the running job's state, built from its events on the main thread, with the step readings and run times the estimates use |
  | `estimate.py` | The run estimate (a past run's time, then its time per step at the first report, then the live step rate) and the job estimate (runs timed by their full time, waits by the cooldown policy), as plain functions of a `LiveRun` |

  A job runs on a thread worker that takes `RunLock("tui")`, opens its own
  `Store`, and runs `JobService.run` with the `ExecutionRecorder`. Its
  events reach the main thread through `App.post_message`, update the
  `LiveRun`, and are passed to the panes; a finished run updates only its
  history row, named by the recorder's `execution_id`. The app registers
  `SIGHUP`, `SIGTERM`, and `SIGINT` on the asyncio loop and cancels any
  running job when it unmounts, so no `draw-things-cli` outlives it. No
  Textual worker may raise, since a failed worker closes the app: store
  reads and `open` failures become messages. Browsing writes nothing but a
  schema upgrade of an older `state/dtc.db`.

  Before a job starts, its worker reads the latest successful run of any job
  (`Store.latest_succeeded_run`) and posts it to the app, so the Status
  widget can estimate the first run before `draw-things-cli` reports a
  step.

  The Execution widget follows the history cursor after a 0.2-second pause,
  reading through the same `HistoryReader`; only the newest read is shown.
  Its file-name links carry a click action built from the execution and run
  numbers only (`reveal(12, 3)`), never from stored text, and run in the
  widget that holds the text, which hands them to the pane.

## Phase 3: API and MCP for agents (planned)

- Queue and one worker in the state store, with restart recovery and explicit
  resume (`jobs/job_queue.py`). The server holds the run lock while it is up.
- `server/`: HTTP API (`dtc serve`, FastAPI and uvicorn), bearer-token
  auth, job control, history, event stream, limits, and an audit log.
- Job file management in `data/jobs/` behind a write flag, with `.backups/` and
  `.trash/`.
- `mcp_server/` (`dtc mcp`): a thin client of the HTTP API, exposing typed
  tools. It never touches the core directly.

## Rules across phases

- No source in the project root; each front end gets its own subpackage.
- Front ends call services and observe events; they do not parse logs.
- One run at a time, machine-wide. No parallel generation.
- Never edited by any interface: `data/params/*.json`, `data/params/*.yaml`, `config/global-config.yaml`.
- No credential (`--api-key`, `--remote-shared-secret`) reaches events, the
  state store, logs, or API responses.
- Agents (phase 3) can express only what a job file can express, within
  `data/jobs/` and the configured input directory.
