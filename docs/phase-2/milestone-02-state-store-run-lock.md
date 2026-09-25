# Milestone 02: State Store and Run Lock

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Milestone 01: Job events and cancellation](milestone-01-job-events-cancel.md)

## Goal

Give every front end one place to record and read execution history, and make sure
only one process drives the GPU at a time.

## Scope

In scope:

- A SQLite database for executions and per-run records
- A run lock shared by `run-job`, `generate`, the TUI, and (Phase 3) the
  server
- Recording every `run-job` execution from its events
- A one-time, repeatable import of phase 1 manifests

Out of scope:

- The queue and its states (Phase 3, Milestone 01)
- Storing job definitions; they stay as YAML files in `data/`
- Recording the one-off `generate` command
- Any change to the manifest or log files beside the outputs

## Planned changes

### Location

- The database is `state/dtc.db` and the lock is `state/run.lock`, under the
  project root. `state/` is added to `.gitignore`. The location is fixed and
  not configurable: the lock must be the same file for every process on the
  machine, whatever global configuration it loads, or two runs could drive the
  GPU at once (see the changelog).
- The global configuration gains one optional key, validated when the
  configuration loads like the others (added to `GLOBAL_CONFIG_KEYS`) and
  documented in `config/global-config.example.yaml`:
  - `history_retention_days`: whole number of days from 0 to 3650, default
    14; 0 keeps history forever. See "Retention" below. The app never edits
    `config/global-config.yaml`.
- The project lives on an external volume, and SQLite's WAL mode and `flock`
  are unreliable on network and some external filesystems. On open the store
  requests WAL and checks that SQLite reports `wal`; otherwise it fails with a
  message naming the `state/` directory and the problem, and nothing is
  recorded or locked silently.
- `state/` is created on demand, and `dtc.db` and `run.lock` are created with
  mode 0600: history holds prompts and paths.
- A failure to open the store or take the lock for a reason other than "busy"
  (an unwritable `state/`, an unsupported filesystem, a newer schema) is not
  ignored. See "Failures before a run starts".

### Dependency direction

The recorder consumes `jobs/job_events.py`, so `state` depends on `jobs`. The
rule in [development-rules.md](../development-rules.md), `AGENTS.md`, and
[architecture.md](../architecture.md) becomes `cli`, `tui`, `server` ->
`state` -> `jobs` -> `core`; `jobs` never imports `state`, so the CLI wires the
recorder in as an observer. Moving the event types into `core/` was rejected:
they describe `JobService` concepts. `core/run_lock.py` stays in `core/`.

### Store (`state/store.py`)

- Standard library `sqlite3`, WAL mode, foreign keys on, and a `busy_timeout`
  (5 seconds), since the recorder and the TUI read and write concurrently. One
  connection per thread, since `sqlite3` connections are not thread-safe.
- Schema version in `PRAGMA user_version`. Migrations are forward-only
  functions run when the store opens, each in one `BEGIN IMMEDIATE`
  transaction, so two processes opening a fresh or old database at once do not
  both migrate it. Opening a database with a newer
  version than the code knows fails with a clear message.
- Tables:
  - `executions`: id, job name, job file, mode, status, model, seed, seed
    source, cooldown seconds and source, total runs, started at, finished at,
    exit code, the signal if any, the manifest path if one was written (unique
    when not null), the log path if one was written, `config_file`, and
    `recovered_at` (set only when a crash sweep closed the row, see
    "Recorder"). The exact job file text (`job_yaml`) and the settings it was
    resolved with (`settings` JSON: input file, output directory, cooldown and
    its source, config file, config override, input resize) are stored, so
    history still shows what ran after the YAML file or the configuration
    changes. A parsed `JobDefinition` is not stored: it holds paths and a
    resize plan that are not meant to be serialized. Columns that a phase 1
    manifest cannot fill (`job_yaml`, `model`, `signal`, `exit_code`, log
    path, total runs beyond the runs listed) are nullable.
  - `runs`: execution id, run number, pair, positive, negative, input, resized
    input, output, last frame, command (redacted JSON list), started at,
    seconds, exit code, status, cooldown after seconds.
- Times are stored as local ISO 8601 text with an offset, like the manifest,
  and also as a UTC epoch (`started_epoch`, `finished_epoch`). Ordering
  ("newest first") and the retention cutoff use the epoch columns, never a
  comparison of the text, which misorders across daylight-saving changes.
- The store exposes small functions used by the recorder and, later, the
  TUI and server: start an execution, record a run start and finish, finish an
  execution, list executions (newest first, with paging and an optional status
  or name filter), and get one execution with its runs.
- Manifest paths are stored absolute and resolved, the same way the job's
  output directory is (`job.output_directory` is already resolved).

### Retention

History older than `history_retention_days` (default 14) is pruned.

- An execution is pruned when its `finished_epoch` is older than the cutoff; its
  `runs` rows go with it. Rows still `running` are never pruned. A swept row
  (see "Recorder") gets `finished_at` set to the sweep time, so it is pruned
  like any other.
- Pruning runs when a process opens the store to record or read history
  (`run-job`, the TUI, and the Phase 3 server), and, in the server, once
  every 24 hours. It is a plain `DELETE`, in one transaction.
- Only database rows are deleted. Output files, last frames, manifests, and
  logs beside the outputs are never touched.
- The `audit_log` table (Phase 3) is not pruned by this setting.

### Recorder

An event observer (`state/recorder.py`) writes to the store as events
arrive: `JobStarted` creates the `executions` row, `RunStarted` and
`RunFinished` write the `runs` row, `CooldownEnded` sets the finished run's
`cooldown after seconds` to the seconds actually waited (the cooldown's
planned length is in `CooldownStarted` and is not stored), and `JobFinished`
closes the execution. It is combined with any other observer (such as a UI's)
with `combine_observers`.

- Recording happens for every `run-job` execution, regardless of
  `write_job_records`. That key still controls only the manifest and log
  files.
- `--dry-run` records nothing.
- **Crash recovery.** A crash leaves an `executions` row with status
  `running`. The sweep runs inside the run lock: `run-job` and the TUI's run
  action take the lock first, then mark every `running` row `interrupted`
  (setting `recovered_at`, and `finished_at` to the sweep time) and every
  `running` run under them `interrupted`, then insert their own row. Holding
  the lock is
  what proves no runner is alive, and it stops a new run's row from being
  swept. Read-only screens never write: they try the lock and release it at
  once (the lock's retry window below keeps a starting run from failing on
  that probe), and if it is free they show `running` rows as `interrupted` in the display
  only (the next run's sweep makes it permanent); if it is held, `running`
  rows are real. A swept row and a user cancel both read `interrupted`;
  `recovered_at` tells them apart.
- A recording failure (for example, a locked or read-only database) is
  logged once, and the recorder then stops recording for that execution
  instead of updating rows that were never created. It does not stop the
  generation, as with any observer error.

### Run lock (`core/run_lock.py`)

- A non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on `state/run.lock`. The
  operating system releases it when the process exits, however it exits, so
  a crash cannot leave a stale lock.
- The holder writes a one-line description into the file (its command name
  and PID) so the failure message can say who holds it. The description is
  informational; the lock itself is the flock.
- Taken by `run-job`, `generate`, and the TUI's run action, after
  validation and before anything is started, including before the store is
  opened for recording. Not taken for `--dry-run`, `validate-job`,
  `validate-config`, or `import-history`.
- Held for the whole job, including cooldowns.
- A busy lock is retried for at most 250 ms before it is reported busy, so a
  read-only screen's brief probe never makes a starting run fail spuriously.
  A run that is really in progress is reported right after that window;
  nothing queues.
- When it is busy, the command exits with code 75 (`EX_TEMPFAIL`) and
  prints, for example: `Another run is in progress (run-job, PID 4123). Try again when it finishes.`
- Provided as a context manager, so tests can hold it in-process. The TUI
  (Milestone 04) catches the busy error and shows the same message instead of
  exiting.
- **Orphaned child guard.** The child `draw-things-cli` runs in its own
  process group, so if `dtc` is killed with `SIGKILL` the lock is released
  while the child may still be driving the GPU. The lock file therefore has a
  second line, the child's PID (its process group id) and executable name, written when a run's
  child starts and cleared when the holder releases the lock normally. After
  taking the lock, a starter reads that line; if the process group is still
  alive and its command name is the executable the run started (checked with
  `ps`, so a reused PID is not mistaken for it), it releases the lock and exits with
  code 75 and `A draw-things-cli from an earlier run is still running (PID
  4123). Wait for it to end or stop it.`. It never kills the process itself.
  A `draw-things-cli` the user started by hand is not recorded and never
  blocks a run. To get the PID, the process runner gains an optional
  `on_start(pid)` callback. `RunnerFactory` is unchanged: `create_runner` in
  `cli/app.py` passes the held lock's `record_child(pid)` to the runner, since
  it is the one place that knows which lock the process holds.

### Failures before a run starts

A real `run-job` takes the lock and opens the store before anything is
started, and a problem there ends the command with exit code 1 and a message
naming the cause, before any generation: an unwritable `state/`, an
unsupported filesystem, a database with a newer schema. `generate` takes only
the lock and fails the same way on a lock problem. Running unrecorded while
history silently falls behind was rejected: it would leave the TUI and the
Phase 3 server showing a false history. A failure after the run has started
(see "Recorder") never stops the generation.

### History import

`dtc import-history [--directory PATH]` reads phase 1 manifests (the `*.json`
records beside job outputs, which are in `<output_directory>/<job name>/` unless
a job sets `output.directory`; it searches the configured output directory
recursively, or `PATH` when given, to reach outputs elsewhere; they exist only
for jobs executed with `write_job_records: true`) and inserts them as
`executions` and `runs`. It skips hidden files and directories. It needs no
run lock, but it opens the store and prunes like any other opener.

- A file is a manifest when it is a JSON object with the keys of a
  `JobManifest` (`job_file`, `name`, `mode`, `seed`, `started_at`, `runs`).
  Any other `*.json` file is counted as unreadable, not imported.
- It is idempotent: a manifest already in the store, matched on the unique
  `manifest_path` column, is skipped. Executions the recorder wrote after this
  milestone store the same path, so a live-recorded run is never imported a
  second time.
- Retention uses the same rule as pruning: a manifest whose `finished_at` (or,
  when it has none, `started_at`) is older than the cutoff is skipped and
  counted as expired, so it is not imported only to be pruned at once.
- A manifest still `running` (its process crashed in phase 1) is imported as
  `interrupted`, with `recovered_at` set.
- Columns the manifest lacks are left null (see "Store").
- It reports how many were imported, skipped, expired, and unreadable. It
  never modifies or deletes a manifest.

Phase 1 manifests record each run with a `batch` field, always equal to the
run's position. The importer ignores it and takes the run number from the
position in the manifest's `runs` list, so new and old manifests import the
same way.

## Documentation

In the same change: add exit code 75 to the exit-code list in
[architecture.md](../architecture.md) and the [user guide](../user-guide.md);
document `import-history`, the `state/` directory, `history_retention_days`,
and the orphaned-child guard in the user guide; update the source layout and the
import-direction line in architecture, `development-rules.md`, and `AGENTS.md`;
update `config/global-config.example.yaml`; and add the entries listed in
[phase-2-changelog.md](phase-2-changelog.md) dated the day it lands.

## Acceptance criteria

- A `run-job` with a fake runner leaves one `executions` row and one `runs`
  row per run, matching the manifest, with no credential value in either.
- An execution with `write_job_records: false` is still recorded.
- Starting a second run-locked command while the lock is held exits with 75
  and the message above; after the holder exits, including by being killed
  (tested with a real subprocess), the next start succeeds.
- After a holder is `SIGKILL`ed while its (fake, long-running) child is alive,
  the next start exits 75 naming the child; once that child ends, the start
  succeeds. A stale PID in the lock file whose process is not
  `draw-things-cli`, or is gone, does not block. The guard never kills
  anything.
- `--dry-run`, `validate-job`, `validate-config`, and `import-history` work
  while the lock is held. A read-only probe of the lock never makes a starting
  run exit 75.
- A `run-job` whose state directory is unwritable exits 1 with the cause and
  starts nothing.
- A `running` row left by a killed process is closed as `interrupted`, with
  `recovered_at` and `finished_at` set and its `running` runs closed too, by
  the next run's sweep, and is displayed as
  `interrupted` by a read-only screen without being written. A run that starts
  while the sweep's process holds the lock never has its own row swept.
- A database with a newer schema version is refused with a clear error. A
  state directory where WAL cannot be enabled is refused with a clear error.
- A failed `JobStarted` insert is logged once, the generation completes, and
  no later event raises.
- `import-history` run twice imports once; a manifest already recorded live is
  not imported again; unreadable files are reported and skipped; manifests
  older than the retention period are counted as expired, not imported; a
  manifest left `running` imports as `interrupted`; manifests in a job's
  subdirectory of the output directory are found.
- With `history_retention_days: 14`, an execution finished 15 days ago is
  removed and one finished 13 days ago is kept, along with its runs, including
  across a daylight-saving offset change; a `running` row is never removed; with 0,
  nothing is removed. No file outside the database is deleted. An invalid
  value fails the global configuration with the file and key named.
- `.gitignore` covers `state/`.
- `make check` passes.
