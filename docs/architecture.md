# Architecture

`draw-things-control` wraps the locally installed `draw-things-cli`. One core
runs generations; front ends (CLI, TUI, API, MCP) are thin layers over it.
Only one `draw-things-cli` runs at a time on the machine.

## Overview

```
Phase 1 (done)       Phase 2 (planned)      Phase 3 (planned)
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

Phase plans: [1](phase-1/README.md), [2](phase-2/README.md),
[3](phase-3/README.md). Each phase reuses the layers below it unchanged.

## Phase 1: core and CLI (done)

Flat modules in the project root; `main.py` is the Typer entry point.

| Module | Responsibility |
|--------|----------------|
| `main.py` | Commands (`generate`, `validate-config`, `validate-job`, `run-job`) and wiring |
| `generation_service.py` | `generate` use case: validate, resolve, preview or run |
| `job_service.py` | `run-job` use case: chain a job's runs, cooldown |
| `draw_things_arguments.py` | Validated options and argument-vector building (no shell) |
| `draw_things_runner.py` | Process group, signals, timeout, graceful-then-forced shutdown |
| `process_output.py` | Classify and log child output; structured progress |
| `global_config.py`, `job_definition.py` | Global config and job file loading and validation |
| `configuration.py`, `generation_config.py` | JSON overrides, `dt-config/` lookup and merging |
| `input_size.py`, `input_resize.py` | Input image check and resize |
| `output_naming.py`, `frame_extraction.py` | Output names; last frames via `ffmpeg` |
| `job_manifest.py`, `job_log.py` | Per-job JSON manifest and log file |

Services receive their runner and executable lookup as dependencies, so tests
never start a process.

**Process supervision.** The runner starts the CLI in its own process group and
reads stdout and stderr concurrently. `SIGHUP`, `SIGINT`, and `SIGTERM` send
`SIGTERM` to the group, wait `--shutdown-grace` (10 s), then `SIGKILL`.
`--timeout` uses the same sequence. Without `--output`, the child inherits the
terminal for inline preview.

**Exit codes.** 0 success; 1 run wrote no output or last-frame extraction
failed; 2 invalid input; 124 timeout; 128+N stopped by signal N (130 for
Ctrl-C); otherwise the CLI's own code.

## Phase 2: TUI and shared state (planned)

Adds what every later front end needs, without changing the CLI's behavior:

- `job_events.py`: structured job events and `JobService.cancel()`. The CLI's
  log output becomes one event observer. Runner output feeds `RunOutput`
  events through an `on_output` callback.
- `state_store.py`, `state_recorder.py`: SQLite run history (standard
  library), recording every `run-job` run with its YAML text and resolved
  settings. Rows older than 14 days are pruned; output files never are.
- `run_lock.py`: `fcntl.flock` lock taken by `run-job`, `generate`, and the
  TUI. A second starter fails immediately; nothing queues silently.
- `tui/` (Textual): job browser, live run view, run history. Read-only for
  job files.

## Phase 3: API and MCP for agents (planned)

- Queue and one worker in the state store, with restart recovery and explicit
  resume (`job_queue.py`). The server holds the run lock while it is up.
- `server/`: HTTP API (`main.py serve`, FastAPI and uvicorn), bearer-token
  auth, job control, history, event stream, limits, and an audit log.
- Job file management in `data/` behind a write flag, with `.backups/` and
  `.trash/`.
- `mcp_server/` (`main.py mcp`): a thin client of the HTTP API, exposing typed
  tools. It never touches the core directly.

## Rules across phases

- The core stays flat; each front end gets its own subpackage.
- Front ends call services and observe events; they do not parse logs.
- One run at a time, machine-wide. No parallel generation.
- Never edited by any interface: `dt-config/*.json`, `config/global-config.yaml`.
- No credential (`--api-key`, `--remote-shared-secret`) reaches events, the
  state store, logs, or API responses.
- Agents (phase 3) can express only what a job file can express, within
  `data/` and the configured input directory.
