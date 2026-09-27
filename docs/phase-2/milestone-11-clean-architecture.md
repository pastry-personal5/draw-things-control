# Milestone 11: Clean Architecture Refactor

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Milestone 10: Job Definition widget, job IDs, and execution IDs](milestone-10-tui-job-definitions.md)

## Goal

Leave Phase 2 with a clean codebase and a clean architecture before
[Phase 3](../phase-3/README.md) adds the HTTP API server and the MCP server:

- every fact and every rule lives in one place;
- no long module, class, or function remains where a smaller unit reads
  better, and classes are introduced where they carry state or a role;
- everything a front end needs that is not about its own screen or terminal
  sits below the front ends, where the API server can reach it.

A person using `dtc` sees no difference, except for the few breaking changes
listed under [Legacy contracts](#legacy-contracts).

## Scope

In scope (owner decision):

- Removing the duplicates listed under [Findings](#findings).
- Splitting the long modules, classes, and functions listed there.
- New classes, a new `services/` layer, and new module names.
- Breaking any legacy contract: the Python API between modules freely, and
  the user-facing ones listed under [Legacy contracts](#legacy-contracts).
- Seams that Phase 3 needs (below), built as structure, not as features.
- Tests that pin today's output before the code moves, and a test that
  enforces the import direction.
- Pyright, in its `standard` mode, in `make check`.

One milestone, built in seven steps (owner decision).

Out of scope:

- New features: no queue, no server, no resume, no new command or option.
- A state store schema migration. The database written by Milestone 10 is
  read as it is, and an older `dtc` can still read the database this one
  writes.
- Any change to job files, manifests, `data/params/`, or
  `config/global-config.yaml` as files. A job file that uses a dropped key or
  has a duplicate key is fixed by hand; `data/params/*.json` and
  `data/params/*.yaml` are never edited, deleted, or deduplicated.
- Changing the CLI's output, its exit codes, or the TUI's behavior, apart
  from the listed breaking changes and the small bugs fixed as found (see
  [Decisions](#decisions)).
- Enforcing the size limits after this milestone: they are acceptance
  criteria, measured when it lands, not a test (owner decision).

## Findings

What the code looks like at the end of Milestone 10 (8,520 lines in 38
modules).

### Duplicates

| # | What | Where now | One place after |
|---|------|-----------|-----------------|
| D1 | Project paths, computed three times from `parents[3]` | `core/global_config.py` (`PROJECT_ROOT`), `core/generation_config.py` (`PARAMS_DIRECTORY`, `JOBS_DIRECTORY`), `core/run_lock.py` (`STATE_DIRECTORY`), `tui/history.py` (`database_path`) | `core/paths.py` |
| D2 | Two YAML readers: the strict one for base configurations, a plain `yaml.safe_load` for job files and the global configuration, so a job file with a duplicate key or `010` is read silently | `core/configuration.py` (`_StrictLoader`), `core/global_config.py` (`read_yaml_mapping`) | `core/yaml_files.py` |
| D3 | "An int that is not a bool" and "a number that is not a bool" | `core/global_config.py` (`is_number`, inline at `load_global_config`), `jobs/job_definition.py` (`_is_int`) | `core/numbers.py` |
| D4 | Exit code 128+N for a signal, and back | `core/generation_service.py`, `jobs/job_service.py` (`_stop`, `_execute_run`, `_exit_signal`), `tui/app.py` (twice) | `core/exit_codes.py` |
| D5 | "Could not find 'draw-things-cli' on PATH" check and message | `core/generation_service.py` (`execute`), `jobs/job_service.py` (`_check_tools`) | `require_executable` in `core/generation.py` |
| D6 | Is a process group alive, with opposite answers on `PermissionError` | `core/draw_things_runner.py`, `core/run_lock.py` | `core/process/groups.py`, the difference named by a parameter |
| D7 | The `RunnerFactory` protocol, declared twice | `core/generation_service.py`, `jobs/job_service.py` | `core/process/runner.py` |
| D8 | Which job override becomes which flag or `--config-json` key | `jobs/job_service.py` (`_plan_run`), `core/generation_config.py` (`CONFIG_ONLY_KEYS`), `tui/text.py` (`OVERRIDE_ARGUMENTS`, `FLAG_CONFIG_KEYS`), `core/draw_things_arguments.py` (`command_settings`) | one table in `core/arguments.py` |
| D9 | A local ISO 8601 timestamp from a clock | `jobs/job_service.py`, `state/store.py`, `state/history_import.py`, `tui/job_files.py` | `core/clock.py` |
| D10 | The execution's `settings` snapshot | `state/recorder.py`, `state/history_import.py` | `state/executions.py` |
| D11 | Opening the state store, four ways (create or not, prune or not) | `cli/app.py`, `tui/app.py`, `tui/job_files.py`, `tui/history.py` | `Store.open(..., mode=)` |
| D12 | Running a recorded job: lock, store, sweep, recorder, reservation, observers | `cli/app.py` (`run_job`), `tui/app.py` (`execute`) | `services/job_runs.py` |
| D13 | Reading a typed number and its limits | `tui/history.py` (`parse_id`, `MAX_ID`), `state/ids.py` (`MAX_NUMBER`, `MAX_DIGITS`) | `state/ids.py` |
| D14 | YAML suffixes | `core/configuration.py` (`YAML_SUFFIXES`), `jobs/job_report.py` (`JOB_SUFFIXES`) | `core/yaml_files.py` |
| D15 | The stale-worker-result check (`request != self.read_count`, five times), and the scrollbar height rule (twice) | `tui/panes.py` | `tui/panes/base.py` |
| D16 | Status words as quoted literals (57 of them: `"running"`, `"succeeded"`, ...) | throughout | `RunStatus`, `JobStatus` enums in `jobs/events.py` |
| D17 | The signals that stop a job (`SIGHUP`, `SIGINT`, `SIGTERM`) | `core/draw_things_runner.py`, `tui/app.py` (both `HANDLED_SIGNALS`) | `core/process/signals.py` |
| D18 | A number as a person writes it in YAML (`1200`, `90.5`) | `core/global_config.py` (`_number_text`), and the same test in `core/numbers.py` | `number_text` in `core/numbers.py`. `jobs/job_report.py`'s `seconds_text` (never an exponent) and `tui/text.py`'s `number_text` (`%g`, `-` when unknown) write different things and stay |

### Long units

Measured with `ast` (functions of 35 lines or more, classes of 150 or more):

| Unit | Lines | Split into |
|------|-------|------------|
| `tui/text.py` (module) | 893 | `tui/text/` package, one module per subject |
| `tui/panes.py` (module) | 635 | `tui/panes/` package, one module per pane |
| `JobService` (27 methods) | 487 | `JobExecutor`, `JobPlanner`, `CancelToken`, `RunFinisher`, `JobRecords`, `JobLogWriter` |
| `MainScreen` (50 methods) | 386 | `MainScreen` (layout, routing) and `CommandController` (`/` commands) |
| `Store` (34 methods) | 313 | `Database` and three repositories behind a thin `Store` |
| `DrawThingsApp` (31 methods) | 305 | the app, `tui/signals.py`, and `services/job_runs.py` |
| `load_job` | 82 | `JobParser`, one method per section of the job file |
| `DrawThingsProcessRunner.run` | 72 | its start, one step of the supervision loop, and its result; the timing is unchanged |
| `GenerationService.prepare` | 69 | typed `GenerateRequest`; configuration and file resolution apart |
| `generate` (CLI) | 62 | the options into a `GenerateRequest` |
| `JobService._run_runs` | 55 | the chain loop, with the records and the log lines moved out |
| `execution_text` | 55 | sections, over typed rows |
| `ExecutionRecorder._record` | 47 | one method per event type |
| `_prompt_pairs` | 46 | `JobParser`'s pair reading and its run assignment check |
| `LiveRun.apply`, `DrawThingsGenerateArguments.__post_init__` | 44 each | one method per event type; one check per group of options |

The functions of 41 to 43 lines (`interruptible_wait`, `event_text`,
`status_lines`, and others) are split the same way where they pass 40 lines.

### Layering problems

- Front-end-neutral code lives in `tui/`, where the API server may not import
  it: `JobCatalog` (listing, job IDs, finding a job), `HistoryReader`, job
  details and plans, and the command analysis in `tui/text.py`.
- `cli/app.py` builds the real tools (`create_job_service`) and hands them to
  the TUI; the server would have to copy it.
- `jobs/job_service.py` imports text from `jobs/job_report.py`, a
  presentation module.
- `tui/job_files.py` imports `SORT_KEYS` from the command table.
- `state/store.py` gets its directory from `core/run_lock.py`.
- Store reads return `dict[str, Any]` (15 uses in `tui/text.py` alone), so
  every reader knows the column names.
- Errors are plain `ValueError` strings shaped `path: 'field' problem`; the
  API needs the `field` and a stable `code`.
- Tests point paths elsewhere by patching module globals
  (`run_lock.STATE_DIRECTORY`, `generation_config.PARAMS_DIRECTORY`).

### Gaps in the tests

`tests/cli/test_job_output.py` pins the text of `validate-job` and
`run-job --dry-run`, but nothing pins the log lines of a real run (`Job ...`,
`Run 1/3 ...`, `Cooldown: ...`, the result), the manifest's JSON, the job log
file, or the order of a job's events. Step 3 moves all of them, so Step 1
pins them first.

## Target architecture

### Layers

```
cli ────────────┐
tui ────────────┼──▶ services ──▶ state ──▶ jobs ──▶ core
server (phase 3)┘
mcp_server (phase 3) ──▶ server, over HTTP only
```

- `services/` is new: the use cases every front end shares, and the wiring of
  the real tools. It holds no Typer, Textual, or FastAPI code.
- A front end may import any layer below it, never another front end (except
  that `dtc tui` starts the TUI app, as now).
- `jobs/` never imports `state/`; `core/` imports nothing of the package
  outside `core/`.
- `tests/test_architecture.py` reads every module's imports with `ast` and
  fails on an import against these rules.

### Modules

Renames drop the stutter (`jobs/job_service.py` becomes `jobs/executor.py`);
lower layers stop calling themselves services, so "service" means the new
layer.

```
src/draw_things_control/
├── core/
│   ├── paths.py              # ProjectPaths: root, config, data/jobs, data/params, state, run.lock, dtc.db (new)
│   ├── errors.py             # DtcError with a code; InputError(DtcError, ValueError) with path and field (new)
│   ├── clock.py              # Clock, local_timestamp (new)
│   ├── exit_codes.py         # 0, 1, 2, 75, 124, 128+N, and back (new)
│   ├── numbers.py            # + is_int, is_number, number_text
│   ├── yaml_files.py         # the one strict YAML reader, YAML suffixes (new)
│   ├── draw_things_config.py # configuration.py + generation_config.py, YAML only
│   ├── global_config.py      # GlobalConfig only
│   ├── cooldown.py           # CooldownPolicy, CooldownWait, parse_cooldown (from global_config.py)
│   ├── arguments.py          # draw_things_arguments.py + redact_command + the override table + argument rows
│   ├── generation.py         # generation_service.py, taking a GenerateRequest; require_executable
│   ├── run_lock.py
│   └── process/
│       ├── runner.py         # DrawThingsProcessRunner, RunnerFactory, ProcessResult
│       ├── output.py         # process_output.py
│       ├── signals.py        # HANDLED_SIGNALS, install and restore handlers, interruptible_wait, CancelToken (new)
│       └── groups.py         # process group checks and signals (new)
├── jobs/
│   ├── definition.py         # JobDefinition, GenerationMode, PromptPair, ConfigOverride
│   ├── parsing.py            # JobParser, load_job(path), load_job_text(text, path) (new)
│   ├── prompt_pairs.py       # the prompt pairs and which pair each run uses (from job_definition.py)
│   ├── overrides.py          # config_override (from job_definition.py)
│   ├── files.py              # job_files, read_job, read_settings (from job_report.py)
│   ├── planning.py           # JobPlanner, PlannedRun, JobPreview (new)
│   ├── executor.py           # JobExecutor, JobRunOptions, JobOutcome (from job_service.py)
│   ├── launcher.py           # RunLauncher: one run through draw-things-cli, and how it ended (new)
│   ├── run_finisher.py       # tag the video, extract the last frame, measure (new)
│   ├── records.py            # JobRecords: the manifest and the job log file (job_manifest.py + job_log.py)
│   ├── log_writer.py         # JobLogWriter: the job's log lines, from its events (new)
│   ├── events.py             # job_events.py + RunStatus, JobStatus, event_to_dict
│   ├── text.py               # the text of job_report.py
│   ├── output_naming.py
│   ├── inputs/               # size.py, resize.py
│   └── media/                # tools.py (find ffmpeg, ffprobe), toolkit.py (MediaTools), frames.py, info.py, video_color.py
├── state/
│   ├── schema.py             # the migrations (from store.py)
│   ├── database.py           # Database: file mode, a connection per thread, migrations, transactions; StateError (new)
│   ├── executions.py         # ExecutionRepository, NewExecution, NewRun, ExecutionRow, RunRow, ExecutionSettings (new)
│   ├── job_ids.py            # JobIdRepository (new)
│   ├── settings.py           # SettingsRepository (new)
│   ├── store.py              # Store: opens the database in a mode, holds the repositories; pruning
│   └── recorder.py, ids.py, history_import.py
├── services/                 # (new)
│   ├── toolkit.py            # Toolkit: runner factory, executable lookup, media tools; builds executors
│   ├── job_runs.py           # JobRunSession: a recorded job run, for the CLI, the TUI, and the queue worker
│   ├── job_catalog.py        # JobCatalog (from tui/job_files.py)
│   ├── job_details.py        # read_details, add_plan (from tui/job_files.py)
│   └── history.py            # HistoryReader, output paths (from tui/history.py)
├── cli/app.py
└── tui/
    ├── app.py, screens.py, controller.py (new), signals.py (new), desktop.py (reveal, pbcopy; new)
    ├── commands.py, widgets.py, job_watch.py, live_run.py, estimate.py
    ├── panes/                # base.py, status.py, cli_output.py, job_definitions.py, history.py, execution.py
    └── text/                 # jobs.py, status.py, events.py, execution.py, arguments.py, tables.py
```

Tests mirror the top-level subpackages, as the development rules say:
`tests/services/` is new, and a module in a sub-package such as
`core/process/` is tested in `tests/core/`.

### Errors and exit codes

`DtcError` carries a stable `code`. The CLI maps codes to exit codes in one
place (Step 7); Phase 3 maps the same codes to HTTP statuses. The exit codes
are today's.

| Code | Raised for | Exit code |
|------|------------|-----------|
| `invalid_input` | a bad job file, configuration, option, or input file (`InputError`, with `field` when there is one) | 2 |
| `tool_missing` | `draw-things-cli`, `ffmpeg`, or `ffprobe` not found | 2 |
| `not_found` | no such job, job ID, or execution ID | 2 |
| `busy` | the run lock is held, or an earlier run's child still runs | 75 |
| `state_unavailable` | the state database or the lock file cannot be used | 1 |

A job's own result keeps its exit code (1, 124, 128+N, or the child's).
`InputError` stays a `ValueError`, so code that catches `ValueError` today
still catches it.

## Changes

Built in the order below on a `refactor/m11` branch, which the owner merges
to `main` when the milestone is done. Each step is one commit
(`refactor(scope): ...`) that passes `make check` on its own; commits are
made when the owner asks, as always.
 Each package's modules are renamed with `git mv` in
the step that restructures it (`core/` in Steps 1 and 2, `jobs/` in 3,
`state/` in 4, `tui/` in 6), so a file's history follows it. Tests move with
their modules and change only in imports and construction unless a step
says otherwise.

### Step 1: safety net and foundations in `core/`

- Characterization tests, before any code moves, with a fake runner and a
  fixed clock: the stdout and stderr lines of `run-job` for a job that
  succeeds, one that fails in run 2, one stopped during a cooldown, and one
  stopped before a run; the manifest's JSON and the job log file of each;
  the sequence of events; and the state store rows. They pass unchanged to
  the end of the milestone.
- Pyright joins the `dev` extra with a `[tool.pyright]` section in
  `pyproject.toml` (`typeCheckingMode = "standard"`, `src` and `tests`, the
  project's `.venv`), and `make typecheck`, run by `make check`. The 226
  errors it found are fixed in `src/` and in the small test files. The test
  files whose subject a later step rewrites (`tests/state`, `test_job_service`,
  `test_service`, `test_job_definitions`, `test_state_cli`; 159 errors, mostly
  optional subscripts of store rows) are in pyright's `ignore` list, and each
  step removes its entries; the list is empty when the milestone is done.
  An ignore comment names its rule and says why. The PyPI package downloads
  Node the first time it runs, so the first check needs the network once.
  Pyright's documentation is fetched through Context7 first.
- `core/paths.py`: a frozen `ProjectPaths`, built from the project root, and
  the one place the project's directories are worked out (D1); the module
  constants that tests patch (`STATE_DIRECTORY`, `PARAMS_DIRECTORY`, ...) now
  read it. Passing it down instead of patching happens layer by layer, in the
  step that rewrites each layer: `state/` in Step 4, the CLI (which keeps it
  in Typer's context object, which a test passes with
  `CliRunner.invoke(..., obj=paths)`, so no new option is needed) and
  `services/` in Step 5, and the TUI in Step 6. Step 7 checks that no test
  patches a path.
- `core/errors.py` (see [Errors and exit codes](#errors-and-exit-codes)).
  An error's text is today's message, so the CLI prints the same line.
  `RunLockBusy`, `RunLockError`, and `StateError` become `DtcError`s.
- `core/yaml_files.py`: the strict loader reads job files and the global
  configuration too (D2, D14). Each kind keeps the start of its messages
  (`Job file is not valid YAML: ...`, `Global configuration not found:
  ...`); only the refusals are new. Checked on 2026-09-27: every file in
  `data/jobs/` and `config/global-config.yaml` passes the strict reader.
- `core/numbers.py`, `core/clock.py`, `core/exit_codes.py` (D3, D4, D9,
  D18).
- `core/cooldown.py` split from `core/global_config.py`.
- `core/process/`: the runner, output processing, signal helpers, and one
  process group module (D6, D7, D17). `redact_command` moves beside
  `SECRET_FLAGS` in `core/arguments.py`.

### Step 2: arguments and generation

- One table in `core/arguments.py` says, for each job override key, the flag
  or `--config-json` key it becomes. `JobPlanner`, `build_config_json`,
  `command_settings`, and the TUI's argument table read it (D8). The
  argument rows (`argument_rows`, `_config_value`) stay in the TUI: they
  write words for a person, and the API returns the command itself.
- `generate --config-file` and `validate-config` read YAML only (owner
  decision). A `.json` file is refused with exit code 2 and a message that
  names the YAML file of the same stem beside it when there is one, from the
  same function that tells jobs so today (a job looks in `data/params/`); the JSON file is never
  touched. `load_config`'s JSON branch goes, and the builder no longer writes
  `--config-file`: a YAML configuration always reaches `draw-things-cli` as
  `--config-json`. The flag stays in `GENERATE_FLAGS`, so saved commands
  that have it still read back. The test that compares each JSON file in
  `data/params/` with its YAML twin goes with the JSON reader.
- `GenerateRequest`, a frozen dataclass, replaces the `Mapping[str, Any]`
  that `GenerationService.prepare` takes. `prepare` splits into resolving the
  configuration and resolving the files; `generate` in the CLI builds the
  request from its options.
- `require_executable` in `core/generation.py` is the one check that
  `draw-things-cli` exists (D5); `JobPlanner` calls it too.

### Step 3: `jobs/`

- `JobService` becomes `JobExecutor` in `jobs/executor.py` and gives away:
  - planning (`preview`, the seed, a run's arguments and file names, the
    tool checks) to `JobPlanner`;
  - the stop flag and the wake-up pipe to `CancelToken` in
    `core/process/signals.py`, which `cancel()` and the cooldown wait share;
  - tagging, frame extraction, and measuring to `RunFinisher`, given one
    `MediaTools` value instead of five constructor arguments;
  - one run through draw-things-cli, and how it ended, to `RunLauncher`;
  - the manifest and the job log file to `JobRecords`;
  - the job's log lines to `JobLogWriter`, so the executor no longer imports
    text.
- `JobRecords` and `JobLogWriter` read the job's events, but the executor
  calls them itself, in a fixed order and before the caller's observer, not
  through `notify`:
  - the job log file copies every log line of the job, the child's too, so
    `JobRecords` opens it before `JobStarted` (whose event names the
    manifest and the log) and closes it after `JobFinished`;
  - a manifest that cannot be written still fails the job, as today;
    `notify` would log the error and carry on;
  - the log lines keep their order among the other lines.

  A line that needs what only the executor knows stays in the executor:
  where a stop landed (`Job stopped by SIGINT before run 2/3`), a missing
  output, and a failed measurement.
- `JobExecutor.run(job, options)` takes a `JobRunOptions` value (executable,
  shutdown grace, records, observer, child start, execution ID reservation).
  The chain loop reads a list of planned runs and a first input, which is
  the seam Phase 3's resume uses; resume itself is not built.
- `RunStatus` and `JobStatus` (`StrEnum`, the same words) replace the literals
  in Python (D16); SQL keeps its own. `event_to_dict` gives each event as
  JSON: a `kind` (`job_started`, `run_output`, ...), enums as their words, the
  cooldown policy as its mapping, and tuples as lists. No front end uses it
  yet.
- `load_job` becomes `JobParser`, one method per part of the job file, in
  `jobs/parsing.py`. `load_job_text(text, path, global_config, ...)` parses
  text (`path` only names the file in messages) and `load_job(path, ...)` reads a file and calls it, as
  [Phase 3 Milestone 01](../phase-3/milestone-01-queue-run-manager.md)
  planned. Its errors are `InputError`s naming the `field`. The rename hints
  for `batch_count` and `batches` go (owner decision); the hint for
  `cooldown_seconds` stays (owner decision).
- `jobs/job_report.py` splits: reading jobs to `jobs/files.py`, text to
  `jobs/text.py`. Inputs and media tools become the `inputs/` and `media/`
  sub-packages.

### Step 4: `state/`

- `Database` owns the file mode, one connection per thread, migrations, and
  transactions. `ExecutionRepository`, `JobIdRepository`, and
  `SettingsRepository` hold the queries. `Store` opens the database in a mode
  and exposes the repositories (D11). Every mode migrates an older database,
  as any open does today (Milestone 08):

  | Mode | Creates the file | Prunes | Used by |
  |------|------------------|--------|---------|
  | `run` | yes | yes | `run-job`, the TUI's job worker, `import-history` |
  | `write` | yes | no | giving job IDs, keeping the TUI's sort |
  | `browse` | no | no | the history, the execution detail, reading the sort |

  Phase 3 adds a queue and an audit repository beside the others instead of
  growing one class.
- Reads return frozen `ExecutionRow` and `RunRow` values, with what readers
  now compute from dictionaries: the ID text, the output directory, a run's
  file, whether it was imported, the successful runs. Writes take a
  `NewExecution` value, and one function builds its `settings` (D10).
- `ExecutionRecorder` handles each event type in its own method.
- `state/ids.py` reads every typed number (D13).

### Step 5: `services/`

- `Toolkit` (in `services/toolkit.py`) holds the real tools and builds the
  `GenerationService` and the `JobExecutor`, with `handle_signals` as a
  parameter. The CLI and the TUI get it from here, not from `cli/app.py`.
- `JobRunSession` runs a job with its execution recorded: it takes the run
  lock (or is given one the caller already holds, as the Phase 3 server
  will), opens the store in `run` mode, sweeps, gives the caller the latest
  successful run (the TUI's first estimate), reserves the execution ID, and
  runs with the recorder and the caller's observers. `run-job` and the TUI's
  worker call it (D12). It raises `DtcError`s; the CLI maps them to exit
  codes and the TUI to messages.
- `JobCatalog`, the job details, and `HistoryReader` move here from `tui/`,
  typed, and without the TUI's sort keys. They raise `DtcError`s instead of
  returning `X | str`; the TUI's worker helper turns an error into a message,
  so no Textual worker can raise (the rule stays, in one place). The sort
  choice stays a TUI setting, read and written through `SettingsRepository`.

### Step 6: `tui/`

- `tui/text/` and `tui/panes/` packages; `tui/panes/base.py` holds the
  stale-result check and the scrollbar height rule (D15).
- `CommandController` takes the `/` commands and their argument parsing from
  `MainScreen`; the screen keeps the layout and passes job events on.
- The hints for `/jobs`, `/job`, `/history`, `/execution`, and `/run` go
  with the `REPLACED` table (owner decision); each is an unknown command.
- `DrawThingsApp` runs jobs through `JobRunSession`; its signal handling
  moves to `tui/signals.py`; reveal and the clipboard to `tui/desktop.py`.

### Step 7: `cli/` and documents

- `cli/app.py` keeps only commands, options, and the one mapping from error
  codes to exit codes.
- `tests/test_architecture.py` (above).
- The size limits are measured, and any unit still over them is split.
- `docs/architecture.md`, `docs/development-rules.md`, and `AGENTS.md` show
  the new layers and modules, and `make check` with pyright. The user guide
  says `generate` and `validate-config` take YAML only, and drops the old
  commands.
- The Phase 3 documents name the moved code: `JobService.run` (M01, M02),
  `load_job_text` in `jobs/parsing.py` (M01), `install_signals`, which is
  `handle_signals` in the code (M02), and `jobs/job_queue.py` in the Phase 3
  README, which becomes a queue repository in `state/` and a worker built
  on `JobRunSession`.

## Phase 3 alignment

| Phase 3 needs | Provided by |
|---------------|-------------|
| M01: parse a queued job's stored text | `load_job_text` in `jobs/parsing.py` |
| M01: a worker that runs recorded jobs while the server holds the lock | `JobRunSession` given a held lock |
| M01: cancel a job, and the wait between jobs | `CancelToken`, `JobExecutor.cancel` |
| M01: start a chain at run *k* with another input | the executor's run list and first input |
| M01, M02: a queue table and an audit table | a repository each beside the others in `state/` |
| M02: typed responses | `ExecutionRow`, `RunRow`, `JobPreview`, events |
| M02: the event stream | `event_to_dict`, with a `kind` per event |
| M02: errors with a `code` and a `field` | `DtcError`, `InputError`, and the [code table](#errors-and-exit-codes) to map to HTTP statuses |
| M02: `GET /jobs`, `/jobs/{name}/preview`, `/history`, `/outputs` | `JobCatalog`, the job details, `HistoryReader`, `ExecutionRow`'s file paths |
| M02: the token file; M03: `.backups/` and `.trash/` | `ProjectPaths` |
| M02: tools built once for a long-running process | `Toolkit` |
| M03: validate job text before writing it | `load_job_text` |
| M05: `mcp_server` reaches only the HTTP API | `tests/test_architecture.py` |

## Risks

| Risk | Mitigation |
|------|------------|
| The log lines or the manifest change while they move out of the executor | Step 1's characterization tests; the executor calls `JobRecords` and `JobLogWriter` directly, in a fixed order |
| A manifest write failure no longer stops the job | `JobRecords` is not called through `notify` |
| A signal lands between a moved handler and the runner, and orphans `draw-things-cli` | The signal tests (`tests/tui/sigterm_app.py`, the runner and CLI stop tests) run unchanged at every step |
| Pyright's first run finds many errors in today's code | They are fixed in Step 1, before anything moves; ignores name their rule and reason |
| A long refactor collides with other work | It lives on its own branch; each step is one commit that passes `make check`, so the branch can be rebased or reviewed step by step |

## Legacy contracts

The owner allowed breaking any legacy contract; these were settled in an
interview.

| Contract | Decision |
|----------|----------|
| Module paths and the Python API between modules | **Broken**: modules renamed and split freely (owner decision on the renames; design decision otherwise) |
| Job files and the global configuration accept a duplicate key, an octal `010`, or a base-60 `1:30` silently | **Broken**: read with the strict loader, as base configurations are (owner decision). None of today's files is affected |
| JSON base configurations in `generate --config-file` and `validate-config` | **Broken**: YAML only; `data/params/*.json` stay on disk untouched (owner decision) |
| Hints for renamed things: `batch_count` and `batches` in job files, and the TUI's `/jobs`, `/job`, `/history`, `/execution`, `/run` | **Broken**: dropped; an unknown key and an unknown command (owner decision). No file in `data/jobs/` uses the old keys |
| The hint for the old `cooldown_seconds` key in job files and the global configuration | Kept (owner decision) |
| `dtc import-history` | Kept (owner decision) |
| Manifests and logs beside the outputs (`write_job_records`) and their format | Kept (owner decision) |
| The state store schema, and databases written by Milestone 10 | Kept: no migration |
| CLI commands, options, output, and exit codes; TUI behavior | Kept, apart from the rows above |

## Decisions

From the interview (owner decisions), beyond the table above:

- One milestone, built in seven steps, each passing `make check`. Splitting
  it into several milestones was offered.
- The type checker is pyright in `standard` mode, run by `make check`, and
  `src/` and `tests/` pass it when the milestone lands. `mypy` and no type
  checker were offered, and `basic` and `strict` modes.
- The new layer is `services/`; lower layers stop calling their classes
  services. `application/` and `usecases/` were offered.
- Modules are renamed to drop the stutter (`jobs/job_service.py` becomes
  `jobs/executor.py`). Keeping the names and only splitting was offered.
- The size limits are acceptance criteria only. A test that enforces them,
  and Ruff's complexity rules, were offered.

- The branch is `refactor/m11`, one commit per step, merged to `main` by the
  owner. Committing straight to `main`, and leaving the work uncommitted,
  were offered.
- `event_to_dict` is built in this milestone, though nothing uses it until
  Phase 3. Leaving it to Phase 3 was offered.
- The Phase 3 documents are updated in Step 7. Updating each when its
  milestone starts was offered.
- A small bug that a step exposes is fixed in this milestone, not kept.
  Each fix is a **Change** entry in the changelog with its own test; the
  characterization test that pinned the old behavior changes with it, in the
  same commit. Keeping the quirk, and stopping to ask each time, were
  offered. A fix that changes the CLI's output or exit codes, a file format,
  or the state store is not small: it waits for the owner.

Design decisions from the review of this plan:

- The check that `draw-things-cli` exists lives in `core/`, not
  `services/`, since `core/` and `jobs/` both call it and may not import
  `services/`.
- The manifest, the job log file, and the job's log lines are called by the
  executor, not attached as ordinary observers, for the three reasons in
  Step 3.
- The CLI passes `ProjectPaths` through Typer's context object rather than a
  new `--project-root` option or an environment variable, so tests need no
  patching and users see no new option.
- Characterization tests come first, since no test pins a real run's log
  lines, manifest, or event order today.

## Acceptance criteria

- Each duplicate D1 to D18 exists in one place, as listed.
- No module is over 400 lines, no class over 250, and no function over 40,
  except declarative tables (`GENERATE_FLAGS`, the command table, SQL).
- `tests/test_architecture.py` passes, and its own tests show it refusing
  each kind of bad import: a front end importing another, `jobs` importing
  `state`, and `core` importing the rest of the package.
- Step 1's characterization tests pass from Step 1 to the end, changed only
  by a recorded bug fix: the log lines of a real run, the manifests, the job
  log files, the events, and the state store rows are otherwise the same.
- Otherwise the CLI's output and exit codes are unchanged: the existing CLI
  tests pass with changes to their imports and setup only, apart from the
  tests of the dropped behaviors below.
- The TUI's behavior is unchanged: the existing TUI tests pass with changes to
  their imports and setup only, apart from the tests of the dropped command
  hints.
- A database written by Milestone 10 opens, reads, and records with no
  migration; `PRAGMA user_version` stays 3.
- No test patches a module global to point a path elsewhere; tests pass a
  `ProjectPaths`.
- A job file or a global configuration with a duplicate key is refused,
  naming the key and its line.
- `generate --config-file x.json` and `validate-config x.json` are refused
  with exit code 2 and a message naming the YAML file to use; no JSON file
  is changed.
- `batch_count`, `batches`, and the commands `/jobs`, `/job`, `/history`,
  `/execution`, and `/run` get the generic unknown key and unknown command
  messages; `cooldown_seconds` still gets its hint.
- Every `DtcError` has a code from the [code table](#errors-and-exit-codes),
  and the CLI's exit code for each is the one in the table.
- A manifest that cannot be written still fails the job.
- `make check` runs pyright in `standard` mode, and it reports no error.
- `event_to_dict` gives every event type as JSON that `json.dumps` accepts,
  with no credential value.
- No file in `data/params/` is changed.
- The architecture, the development rules, `AGENTS.md`, the user guide, and
  the Phase 3 documents name the new modules and the YAML-only commands.
- `make check` passes.
