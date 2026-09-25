# Milestone 02: State Store and Run Lock

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Milestone 01: Job events and cancellation](milestone-01-job-events-cancel.md)

## Goal

Give every front end one place to record and read run history, and make sure
only one process drives the GPU at a time.

## Scope

In scope:

- A SQLite database for job runs and per-run records
- A run lock shared by `run-job`, `generate`, the TUI, and (Phase 3) the
  server
- Recording every `run-job` run from its events
- A one-time, repeatable import of phase 1 manifests

Out of scope:

- The queue and its states (Phase 3, Milestone 01)
- Storing job definitions; they stay as YAML files in `data/`
- Recording the one-off `generate` command
- Any change to the manifest or log files beside the outputs

## Planned changes

### Location

- The database is `state/dtc.db` and the lock is `state/run.lock`, under the
  project root. `state/` is added to `.gitignore`.
- The global configuration gains two optional keys, validated when the
  configuration loads like the others, and documented in
  `config/global-config.example.yaml`:
  - `state_directory`: absolute or `~`-relative path, default `state/` in
    the project.
  - `history_retention_days`: whole number of days from 0 to 3650, default
    14; 0 keeps history forever. See "Retention" below. The app never edits
  `config/global-config.yaml`.

### Store (`state/store.py`)

- Standard library `sqlite3`, WAL mode, foreign keys on. One connection per
  thread, since `sqlite3` connections are not thread-safe.
- Schema version in `PRAGMA user_version`. Migrations are forward-only
  functions run when the store opens. Opening a database with a newer
  version than the code knows fails with a clear message.
- Tables:
  - `job_runs`: id, job name, job file, mode, status, seed, seed source,
    cooldown seconds and source, total runs, started at, finished at, exit
    code, the signal if any, and the manifest path if one was written. The
    exact job file text (`job_yaml`) and the settings it was resolved with
    (`settings` JSON: input and output directories, cooldown and its
    source) are stored, so history still shows what ran after the YAML file
    changes. A parsed `JobDefinition` is not stored: it holds paths and a
    resize plan that are not meant to be serialized.
  - `runs`: job run id, batch, pair, positive, negative, input, output,
    last frame, command (redacted JSON list), started at, seconds, exit
    code, status, cooldown after seconds.
- Times are stored as local ISO 8601 text with an offset, like the
  manifest.
- The store exposes small functions used by the recorder and, later, the
  TUI and server: start a job run, record a run start and finish, finish a
  job run, list job runs (newest first, with paging and an optional status
  or name filter), and get one job run with its runs.

### Retention

History older than `history_retention_days` (default 14) is pruned.

- A job run is pruned when its `finished_at` is older than the cutoff; its
  `runs` rows go with it. Rows still `running` are never pruned.
- Pruning runs when a process opens the store to record or read history
  (`run-job`, the TUI, and the Phase 3 server), and, in the server, once
  every 24 hours. It is a plain `DELETE`, in one transaction.
- Only database rows are deleted. Output files, last frames, manifests, and
  logs beside the outputs are never touched.
- The `audit_log` table (Phase 3) is not pruned by this setting.

### Recorder

An event observer (`state/recorder.py`) writes to the store as events
arrive: `JobStarted` creates the `job_runs` row, `RunStarted` and
`RunFinished` write the `runs` row, and `JobFinished` closes the job run.
It is combined with any other observer (such as a UI's) with
`combine_observers`.

- Recording happens for every `run-job` run, regardless of
  `write_job_records`. That key still controls only the manifest and log
  files.
- Dry runs record nothing.
- A crash leaves a `job_runs` row with status `running`. A process that
  can take the run lock (non-blocking, released at once) knows no runner is
  alive, so it marks every `running` row `interrupted`. When the lock is
  held, `running` rows are real and are left alone. Read-only screens do the
  check without writing when the lock is held.
- A recording failure (for example, a locked or read-only database) is
  logged and does not stop the generation, as with any observer error.

### Run lock (`core/run_lock.py`)

- A non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on `state/run.lock`. The
  operating system releases it when the process exits, however it exits, so
  a crash cannot leave a stale lock.
- The holder writes a one-line description into the file (its command name
  and PID) so the failure message can say who holds it. The description is
  informational; the lock itself is the flock.
- Taken by `run-job`, `generate`, and the TUI's run action, after
  validation and before anything is started. Not taken for `--dry-run`,
  `validate-job`, or `validate-config`.
- Held for the whole job, including cooldowns.
- When it is busy, the command exits with code 75 (`EX_TEMPFAIL`) and
  prints, for example: `Another run is in progress (run-job, PID 4123). Try again when it finishes.`
- Provided as a context manager, so tests can hold it in-process.

### History import

`dtc import-history` reads phase 1 manifests (the `*.json` records beside
outputs, in the configured output directory; they exist only for runs made
with `write_job_records: true`) and inserts them as
`job_runs` and `runs`. It is idempotent: a manifest already imported, keyed
by its path, is skipped. A manifest whose job started before the retention
cutoff is skipped and counted as expired, so it is not imported only to be
pruned at once. A `*.json` file that is not a manifest (wrong shape) is counted as
unreadable, not imported. It reports how many were imported, skipped, and
unreadable. It never modifies or deletes a manifest.

## Acceptance criteria

- A `run-job` with a fake runner leaves one `job_runs` row and one `runs`
  row per run, matching the manifest, with no credential value in either.
- A run with `write_job_records: false` is still recorded.
- Starting a second run-locked command while the lock is held exits with 75
  and the message above; after the holder exits, including by being killed,
  the next start succeeds.
- `--dry-run`, `validate-job`, and `validate-config` work while the lock is
  held.
- A `running` row left by a killed process is shown as `interrupted` after
  the next open.
- A database with a newer schema version is refused with a clear error.
- `import-history` run twice imports once; unreadable files are reported and
  skipped, and manifests older than the retention period are counted as
  expired, not imported.
- With `history_retention_days: 14`, a job run finished 15 days ago is
  removed and one finished 13 days ago is kept, along with its runs; a
  `running` row is never removed; with 0, nothing is removed. No file
  outside the database is deleted. An invalid value fails the global
  configuration with the file and key named.
- `.gitignore` covers `state/`.
- `make check` passes.
