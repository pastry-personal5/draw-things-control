# Phase 3 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 3](README.md). Newest first.

## 2026-09-25

- **Change**: Phase 3 is planned. The phase document and five milestone
  documents are written; no code yet.

- **Design decision** [M01]: A queued job stores its exact YAML text and
  resolved settings, and the worker re-parses the text when the job starts
  (`load_job_text`). Serializing a parsed `JobDefinition` was rejected (see
  the Phase 2 changelog). Input files are checked at start, so a file removed
  while queued fails the job with its path named.
- **Design decision** [M02]: uvicorn owns signal handling; jobs run with
  `install_signals=False`, and uvicorn's shutdown hook calls `cancel()`.
- **Design decision** [M02]: The server refuses a non-loopback bind unless
  `--allow-remote-bind` is given, so a mistyped `--host` cannot expose job
  control on the network.
- **Design decision** [M03]: Agents submit job YAML text, not structured
  fields, and the server validates it with the same code as `validate-job`
  before writing the text unchanged. The alternative, a structured JSON job
  schema, was rejected because it would need a second definition of a job.
- **Design decision** [M01]: The resolved job definition is snapshotted into
  the database when a job is queued. Editing or deleting the YAML file
  afterwards cannot change a queued or running job, and editing or deleting a
  queued or running job's file is refused anyway.
- **Design decision** [M01]: Resume creates a new queue entry linked to the
  interrupted one, starting at the first unfinished run with the last
  succeeded run's last frame as its input. It is refused when that file is
  gone. Automatic resume was rejected (below).
- **Design decision** [M02]: Limits (queue length, runs per job, total
  runtime, file size) are built with the first submission endpoint, not
  after the write tools, and apply even with the write flag on, because a
  valid job written by an agent can still run for many hours. (They were
  first planned for M05; moved so nothing that accepts agent input ships
  without them.)
- **Design decision** [M02]: Every write action and every submission is
  recorded in an `audit_log` table (time, action, job name, outcome), without
  credentials or prompt text. Built with the first endpoints, like the limits
  (first planned for M05); M05 tests that coverage is complete.

- **Owner decision**: The server holds the run lock for its whole lifetime,
  not per job. While `serve` is up, the CLI and the TUI cannot start runs,
  even when the queue is idle; to run a job by hand, stop the server.
  Holding the lock per job (idle server does not block, worker waits when a
  hand-run holds it) was recommended and not chosen.
- **Owner decision**: A job submitted or written through the API must set
  `run_timeout_seconds`, so its worst-case runtime is bounded. There is no
  global default timeout. CLI and TUI runs are unaffected. A global default
  and an assumed per-run cost were the alternatives.
- **Owner decision**: The API token lives in `config/server-token`
  (0600, gitignored, generated on first `serve`), not in
  `global-config.yaml`, so the secret stays out of a file that may be shared
  or committed. The owner first said "token in global config"; the file
  was proposed and chosen. The MCP server reads the same file.
- **Owner decision**: History retention (14 days by default, overridable
  with `history_retention_days`) also applies to finished queue entries. The
  audit log is exempt; see the Phase 2 changelog for the setting.
- **Owner decision**: Interrupted jobs are marked `interrupted`
  after a crash or restart and can be resumed with an
  explicit action. Jobs that were queued and never started are re-queued
  automatically. Automatic resume of interrupted jobs was rejected: an
  unattended restart must not start heavy GPU work by surprise. Resume is a
  new feature; phase 1 listed resuming a job as a non-goal.
- **Owner decision**: The cooldown also applies between queued jobs: after a
  job finishes, the server waits that job's cooldown before starting the
  next one, holding the run lock. A separate `job_gap_seconds` setting was not
  chosen.
- **Owner decision**: The MCP server is a thin client of the HTTP API, so
  one server process owns the GPU and the queue. Calling the service
  in-process from MCP was not chosen because a second process could collide
  on the GPU.
- **Owner decision**: The queue and job state live in the SQLite state store
  from Phase 2, and the queue survives restarts.
- **Owner decision**: Agents may create, edit, and delete job files, confined
  to `data/`. Delete moves the file to `data/.trash/`, and every edit or
  overwrite keeps the previous version under `data/.backups/`. These
  actions need an explicit server option (`--allow-write`); without it the
  write tools and endpoints do not exist.
- **Owner decision**: The server runs in the foreground, binds to 127.0.0.1,
  and requires a bearer token. Wrapping it in launchd is left to the user.
- **Owner decision**: Agents may use only input files already in the
  configured input directory. There is no upload endpoint and no reuse of
  outputs as inputs.
- **Owner decision**: Agents get paths and metadata for outputs, not file
  contents or thumbnails.
