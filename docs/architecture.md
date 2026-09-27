# Architecture

`draw-things-control` wraps the locally installed `draw-things-cli`. One core
runs generations; front ends (CLI, TUI, API, MCP) are thin layers over it.
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
├── server/      # HTTP API and queue worker                         (phase 3)
└── mcp_server/  # MCP client of the HTTP API                        (phase 3)
tests/           # mirrors the package: tests/core, tests/jobs, tests/state, tests/services, ...
```

Import direction: `cli`, `tui`, `server` -> `services` -> `state` -> `jobs` ->
`core`; `mcp_server` -> `server` over HTTP only. Front ends never import each
other, except that the `dtc tui` command in `cli/app.py` starts the TUI app.
Nothing below the front ends imports Typer, Textual, or a web framework.
Launch with `dtc` or `python -m draw_things_control`.

Phase plans: [1](archive/phase-1/README.md), [2](phase-2/README.md),
[3](phase-3/README.md). Each phase reuses the layers below it unchanged.

## Modules

| Module | Responsibility |
|--------|----------------|
| `core/paths.py` | `ProjectPaths`: where the project keeps its files, worked out once and passed down; no module holds a path of its own |
| `core/errors.py`, `core/exit_codes.py` | `DtcError` and its coded subclasses (`invalid_input`, `tool_missing`, `not_found`, `busy`, `state_unavailable`); the exit codes, signal codes, and the one mapping from an error's code to an exit code |
| `core/yaml_files.py` | The one strict YAML reader: duplicate and non-string keys, octal and base-60 numbers are refused |
| `core/draw_things_config.py` | Draw Things configurations (YAML only), lookup in `data/params/`, and job overrides applied to them |
| `core/arguments.py` | Validated options and argument-vector building (no shell); one flag table (`GENERATE_FLAGS`) drives the builder, the parser, and redaction; `OVERRIDE_TARGETS` says what each job override becomes; `command_settings` reads a saved command back |
| `core/generation.py` | `generate` use case: `GenerateRequest`, resolve, preview or run; `require_executable` |
| `core/global_config.py`, `core/cooldown.py` | The global configuration; `CooldownPolicy` and its mapping, shared by it and jobs |
| `core/numbers.py`, `core/clock.py` | Numbers read from outside and written for people; the clock every timestamp comes from |
| `core/run_lock.py` | `fcntl.flock` on `state/run.lock`, naming the holder and the running child |
| `core/process/` | `DrawThingsProcessRunner` (process group, timeout, graceful-then-forced shutdown), its protocols (`RunnerFactory`), output processing, signals, `CancelToken`, process groups |
| `jobs/definition.py`, `jobs/parsing.py`, `jobs/prompt_pairs.py`, `jobs/overrides.py` | A validated job, and `JobParser` (`load_job`, `load_job_text`), whose errors are `InputError`s naming the file and the field |
| `jobs/executor.py` | `JobExecutor.run(job, JobRunOptions)`: the chain of runs, the cooldown between them, cancellation |
| `jobs/planning.py`, `jobs/launcher.py`, `jobs/run_finisher.py` | The seed, file names, and arguments of each run (`JobPlanner`); one run through draw-things-cli (`RunLauncher`); tagging, last frame, and measuring (`RunFinisher`) |
| `jobs/records.py`, `jobs/log_writer.py` | The manifest and job log file (`JobRecords`); the job's log lines, written from its events |
| `jobs/events.py` | Typed events, `RunStatus` and `JobStatus`, and `event_to_dict` for a JSON stream |
| `jobs/files.py`, `jobs/text.py` | Reading jobs, and the text `validate-job` and `run-job --dry-run` print, shared by the CLI and the TUI |
| `jobs/inputs/`, `jobs/media/`, `jobs/output_naming.py` | Input image check and resize; `ffmpeg` and `ffprobe`, last frames, measuring, color tags; output names |
| `state/database.py`, `state/schema.py` | The SQLite file, its connections, transactions, and migrations |
| `state/executions.py`, `state/job_ids.py`, `state/settings.py` | The repositories; `ExecutionRow`, `RunRow`, `NewExecution`, `NewRun`, and `ExecutionSettings` |
| `state/store.py`, `state/recorder.py`, `state/history_import.py`, `state/ids.py` | `Store` (opened as `run`, `write`, or `browse`), the event recorder, the phase 1 import, and `E0012` and `J0001` |
| `services/toolkit.py` | `Toolkit`: the real tools; builds the generation service and executors |
| `services/job_runs.py` | `JobRunSession`: takes the run lock, opens the store, sweeps, and runs a job with its execution recorded |
| `services/job_catalog.py`, `services/job_details.py`, `services/history.py`, `services/store_provider.py` | The job files of a directory, a job's summary and plan, the execution history, and the browsing store they share |
| `cli/app.py` | Commands (`generate`, `validate-config`, `validate-job`, `run-job`, `import-history`, `tui`) and `CliServices` in Typer's context |

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
  | `signals.py`, `confirm.py` | `SignalGuard` and `QuitPress`; the yes-or-no dialog |
  | `screens.py`, `controller.py` | `MainScreen` (layout, events to panes) and `CommandController` (the `/` commands and their arguments) |
  | `panes/` | `StatusPane`, `CliPane`, `JobDefinitionPane`, `HistoryPane`, `ExecutionPane`, over `base.py` (the height rule for a table that scrolls sideways, and reads whose newest result wins) |
  | `text/` | Every text the TUI shows, by subject (`jobs`, `status`, `events`, `execution`, `prompts`, `arguments`, `history`); job text comes from `jobs/text.py` where the CLI prints the same |
  | `commands.py`, `widgets.py`, `job_watch.py` | The command table, `CommandInput` and `MessageLog`, and the file watcher |
  | `reader.py`, `job_sort.py`, `desktop.py` | `PaneHistory`, the kept sort, reveal in Finder and the clipboard |
  | `live_run.py`, `estimate.py` | `LiveRun`, the running job's state built from its events, and the run and job estimates |

  A job runs on a thread worker through `JobRunSession`. Its events reach the
  main thread through `App.post_message`, update the `LiveRun`, and are passed
  to the panes. The app registers `SIGHUP`, `SIGTERM`, and `SIGINT` on the
  asyncio loop and cancels any running job when it unmounts, so no
  `draw-things-cli` outlives it. Browsing writes nothing but a schema upgrade,
  the job IDs, and the kept sort.

## Phase 3: API and MCP for agents (planned)

- Queue and one worker in the state store, with restart recovery and explicit
  resume (a queue repository in `state/`, and a worker built on `JobRunSession`). The server holds the run lock while it is up.
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
