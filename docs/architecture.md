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
├── jobs/        # job definition and service, manifests, inputs     (phase 1; events, queue later)
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
| `core/draw_things_arguments.py` | Validated options and argument-vector building (no shell) |
| `core/draw_things_runner.py` | Process group, signals, timeout, graceful-then-forced shutdown |
| `core/process_output.py` | Classify and log child output; strip terminal codes; structured progress (step counter and percent) |
| `core/global_config.py` | Global config loading |
| `core/configuration.py`, `core/generation_config.py` | JSON overrides, `dt-config/` lookup and merging |
| `jobs/job_service.py` | `run-job` use case: chain a job's runs, cooldown |
| `jobs/job_definition.py` | Job file loading and validation |
| `jobs/job_report.py` | Reading jobs and the text `validate-job` and `run-job --dry-run` print, shared by the CLI and the TUI (phase 2) |
| `jobs/input_size.py`, `jobs/input_resize.py` | Input image check and resize |
| `jobs/output_naming.py`, `jobs/frame_extraction.py` | Output names; last frames via `ffmpeg`, labeled sRGB |
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
- `tui/` (Textual, Milestone 03 done): `app.py` takes its settings, data
  directory, executable, and `JobService` as arguments; `screens.py` has the
  job list, detail view, and help; `widgets.py` renders `jobs/job_report.py`
  text. Files are read on worker threads and never written. The live run view
  and execution history follow in Milestones 04 and 05.

## Phase 3: API and MCP for agents (planned)

- Queue and one worker in the state store, with restart recovery and explicit
  resume (`jobs/job_queue.py`). The server holds the run lock while it is up.
- `server/`: HTTP API (`dtc serve`, FastAPI and uvicorn), bearer-token
  auth, job control, history, event stream, limits, and an audit log.
- Job file management in `data/` behind a write flag, with `.backups/` and
  `.trash/`.
- `mcp_server/` (`dtc mcp`): a thin client of the HTTP API, exposing typed
  tools. It never touches the core directly.

## Rules across phases

- No source in the project root; each front end gets its own subpackage.
- Front ends call services and observe events; they do not parse logs.
- One run at a time, machine-wide. No parallel generation.
- Never edited by any interface: `dt-config/*.json`, `config/global-config.yaml`.
- No credential (`--api-key`, `--remote-shared-secret`) reaches events, the
  state store, logs, or API responses.
- Agents (phase 3) can express only what a job file can express, within
  `data/` and the configured input directory.
