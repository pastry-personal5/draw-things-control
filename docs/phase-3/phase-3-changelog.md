# Phase 3 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 3](README.md). Newest first.

## 2026-09-27

- **Owner decision** [M01]: From an interview on resume, after the research on what `draw-things-cli` can resume ([draw-things-cli-resume.md](../research/draw-things-cli-resume.md)).
  - Stopping the server stops the running run at once, as planned; a resume reruns it from its start. Letting the first Ctrl-C finish the run (recommended), continuing an entry stopped that way on restart, and finishing the whole job were offered.
  - Cancelling a running entry stops it at once, as planned. An after-run cancel beside it (recommended), and after-run as the default, were offered.
  - No automatic retry: a failed run ends the job, and resume stays explicit. Retrying a failed run once, and resuming a failed entry once on its own, were offered.
  - A run that did not succeed keeps its leftover output file, as since Phase 1, and the API marks it incomplete. Renaming it `-partial`, and deleting it, were offered.
  - Finished entries stay pruned with the history, so an entry left past `history_retention_days` can no longer be resumed. Keeping resumable entries until they are resumed or dismissed was offered.
- **Owner decision**: From the same interview.
  - No pause: stopping the server stops the queue. Pause and unpause, and a `serve --paused` start option, were offered.
  - The API shows every execution in the history with its prompts, whichever front end ran it; showing only the queue's executions was offered.
  - Milestone 03 is built right after Milestone 02 (order 01, 02, 03, 07, 08, 09), so the owner has the queue before agents can write jobs, and Milestone 09's review covers `dtc queue`. Building it last was offered.
- **Design decision** [M01]: Resume, re-planned from the research.
  - The smallest unit that can be resumed is one run. `draw-things-cli` writes a run's output only when the run ends, and has no resume, checkpoint, or partial output of its own, so a resume reruns the run that was cut short, and only a stop during a cooldown between runs loses nothing.
  - A resume starts only from a succeeded run's file: a killed run may leave a truncated video at its output path.
  - The rerun keeps the seed, but identical pixels are not promised, since that was not verified.
  - A model download cut short is continued by `draw-things-cli` itself, since jobs never turn `--download-missing` off.
  - The shutdown grace is kept, though this build of `draw-things-cli` dies at once on `SIGTERM`.
- **Design decision** [M02, M08]: What an agent needs to act on this.
  - `GET /executions/{id}/outputs` says whether each output is complete, which it is only when its run succeeded.
  - `GET /queue/{id}` says whether the entry can be resumed, from which run, or why not.
  - `GET /inputs` lists images in sub-directories too, by the relative path a job's `input` takes.
  - The MCP server gains `get_capabilities`, which its tool descriptions already pointed agents to. Tool descriptions say that a cancel loses the run in progress and that a resume reruns it.

- **Owner decision**: From an interview on the revised plans.
  - Agents do not continue a finished chain with a new job: no output becomes a new job's input, and only a resume continues from one, so an agent writes every prompt of a chain into one job. This settles the open question below, which the phase document drops. A `continue_from: E0012` job key and inputs from the output directory were offered.
  - People get the queue too, in a new Milestone 03: `dtc queue add`, `list`, `show`, `cancel`, and `resume` call the server over HTTP, and the TUI shows a read-only Queue widget with `/get queue` and `/describe Q0007`. Keeping the queue out of the CLI and the TUI (recommended), only one of the two, top-level commands, and a `dtc queue watch` were offered.
  - The Queue widget is always shown, 5 rows, in the right column between Job Definition and Execution History, and the minimum terminal size grows by its height (to about 80x41). Taking the draw-things-cli pane's place while the server holds the lock (recommended), and showing the widget only while the queue has entries, were offered.
  - A job queued with `dtc queue` meets the API's rules and limits, as an agent's does, since the API cannot tell them apart: they share the token, and the caller header names itself. A second token that only `dtc queue` reads, exempting it, was offered.
  - The wait between queued jobs follows only a job that succeeded, and only when the next entry is already queued as it finishes; a job submitted later starts at once. This keeps the first plan and supersedes the [M01] design decision below that waited after any job that ran, measured from its finish (recommended). Waiting after any job but a cancel was offered too.
  - The server keeps `--allow-remote-bind`: a non-loopback `--host` is refused without it. This supersedes the [M02] design decision below that dropped it, so the [M02] design decision of 2026-09-25 stands. Loopback only (recommended) was offered.
  - Confirmed as recommended: limits of 100 runs and 48 h (50 runs and 12 h, and no time cap, were offered); resume of `interrupted`, `failed`, and `cancelled` entries (without `cancelled`, and `interrupted` only, were offered); `POST /validate` without `--allow-write` (only with it was offered); and no resume of executions that `run-job` or the TUI started (resuming any execution through the queue was offered).
- **Design decision** [M01]: The wait between jobs lives in the worker. Shutdown ends it, so a restart starts the next queued job without it, and a wait that finds no entry queued any more (the one waiting was cancelled) ends, since no wait starts for an empty queue. An entry keeps no `cooldown_until`; `GET /queue` reports the worker's.
- **Design decision** [M02, M08, M03]: Since the server may listen beyond loopback, its clients take `--server-url`, and `dtc mcp` and `dtc queue` refuse a non-loopback one unless `--allow-remote-server` is given, the client's side of `--allow-remote-bind`, so a mistyped URL cannot send the token elsewhere. With `--allow-remote-bind`, `serve` warns that the token crosses the network in plain HTTP, and the `Host` check accepts the bound address.
- **Design decision** [M03]: The TUI reads the queue from the state store through a reader in `services/`, not over HTTP, so it shows the queue whether or not the server is up, as it shows the history. `dtc queue` goes through the API, so the rules, the limits, and the audit log apply (caller `cli`). Its HTTP client is its own, in `cli/`: front ends never import each other, and `mcp_server/` imports nothing else from the package. It takes a job ID, a job file's name, or a path to a file directly in `data/jobs/`.
- **Change** [M03]: Milestone 03, "Queue for people", is planned.

- **Change**: The Phase 3 plans were reviewed against the code and revised; still no code. The decisions below record each change. The phase document gains an open question for the owner, continuing a chain with a new job, which the owner decision against reusing outputs as inputs rules out today. Owner decisions are unchanged.

- **Design decision** [M01]: A queue entry's snapshot also holds the text of its base configuration, and the parser takes it from there instead of `data/params/`. `load_job_text` reads `config_file` from disk, so the stored job text alone did not make "what runs is what was validated" true: an edited base configuration would change a queued job, and a resume would continue a chain with other settings. Storing the parsed `JobDefinition` stays rejected, as before.
- **Design decision** [M01]: The worker, resume, the submission rules and limits, and the event backlog live in `services/`, not `server/`, so they have no web framework and are tested without one; `server/` holds only HTTP. This replaces the phase document's "`server/` holds the API and queue worker".
- **Design decision** [M01]: Queue entries have public IDs (`Q0007`) from a counter, as executions and jobs do, and the API names executions by `E0012`; store row numbers stay internal, as Phase 2 decided. The `position` column is dropped: the order is by ID, and nothing reorders the queue.
- **Design decision** [M01]: The wait between queued jobs follows every job that started a run, however it ended. It is measured from when the job finished, so a job submitted during it starts when it ends, a restart honors what is left, and an empty queue does not end it. The first plan skipped it after a failed or cancelled job and when the queue was empty, which let the next job start on a hot GPU; the owner decision (the server waits the finished job's cooldown before starting the next) names no exception. A job that failed before run 1 is followed by no wait. Cancelling during the wait cancels the queued entry named, not the wait.
- **Design decision** [M01]: Resume, completed.
  - A `cancelled` entry can be resumed too, so stopping a long chain to free the GPU does not lose it.
  - It starts after the last succeeded run of the chain, following earlier resumes back, and an entry can be resumed once.
  - It is refused when the job's own first input is gone (parsing the snapshot resolves the job's size from it) or when its execution was pruned.
  - `JobRunOptions` gets a resume point with the seed, since the planner draws a new random seed for a job that sets none, and the executor skips run 1's resized copy.
  - A cancelled entry's execution reads `interrupted`, as a Ctrl-C does, and a run stopped from outside makes the entry `failed`.
- **Design decision** [M01]: Three gaps the code shows.
  - `JobExecutor.cancel()` does nothing before the job has begun, so a cancel that lands between the worker's claim and the job's start is kept and applied at the start, as the TUI does ([Phase 2 Milestone 04](../archive/phase-2/milestone-04-tui-live-run.md)).
  - The job log file is a Loguru sink that copies every message of the process; in the server it would take in API lines, so it is scoped to its job.
  - The busy message names the server and how to free the lock, instead of `Another run is in progress` while the queue is idle.
- **Design decision** [M02]: The server binds to loopback only, and `--allow-remote-bind` is dropped. This supersedes the [M02] design decision of 2026-09-25. Beyond loopback the bearer token would cross the network over plain HTTP, which the non-goals already exclude; an SSH tunnel serves remote use. A `Host` header check keeps web pages out through DNS rebinding.
- **Design decision** [M02]: `serve` gains `--executable` and `--shutdown-grace`, since it runs jobs as `run-job` and `tui` do. It loses `--data-dir`: the server serves the project's `data/jobs/` only, the one directory with job IDs and the one Milestone 07 writes.
- **Design decision** [M02]: A job is named by its job ID or its file name, resolved against the listing of `data/jobs/` with `JobCatalog.find`, never joined to a path. Its `name:` field is not an identifier, since two files can share one and a file's name need not match it. Symbolic links in `data/jobs/` are not read, since a YAML error quotes the file.
  - `/history` becomes `/executions`, and `/outputs/{execution_id}` becomes `/executions/{id}/outputs`.
  - `POST /jobs/{name}/validate` is dropped: `GET /jobs/{job}` returns validity, and Milestone 07 adds `POST /validate` for drafts.
- **Design decision** [M02]: One set of rules for every job the API runs or writes, checked at submission, resume, and write: `run_timeout_seconds` is set (owner decision), the input is inside the input directory (owner decision), and, new, `output.directory` is inside the global output directory. A job's `output.directory` may be absolute or climb out with `..`, so an agent-written job could otherwise create directories anywhere. The first plan checked the input directory only on writes.
- **Design decision** [M02]: The limits move under an `api_limits` mapping in the global configuration, with defaults for long chains: `max_job_runs` 100 (was 50) and `max_job_seconds` 172800, 48 h (was 43200, 12 h). The worst case counts each wait between runs as the job's cooldown applied to `run_timeout_seconds`, since a wait follows only a run that succeeded within it. At 12 h, a job with the example job's settings (`run_timeout_seconds: 3600`, the default `auto` cooldown, so at most 1800 s a wait) could have at most 8 runs over the API (8 × 3600 + 7 × 1800 = 41,400 s); at 48 h it can have 32, which suits the long chains Phase 3 is for. A resume counts the runs it has left.
- **Design decision** [M02]: Smaller API decisions.
  - Error codes map to HTTP statuses in one table in `server/`, as they map to exit codes for the CLI. The new codes are `DtcError`s.
  - `GET /queue/{id}?wait=N` waits at most 30 seconds for a change, so agents need not poll fast; no call waits for a generation.
  - Event IDs name the server's run, so a `Last-Event-ID` from before a restart gets `reset`.
  - The OpenAPI schema is behind the token, and the interactive docs are off.
  - Each milestone writes its part of the user guide as it lands, as the documentation rules require. The first plan left it all to Milestone 09.
- **Design decision** [M07]: Drafts and writes.
  - `POST /validate` checks job text exactly as a write would and is always available, since it writes nothing, so an agent can fix a draft before writes are on.
  - Writes and deletes reach only `data/jobs/<name>.yaml` for a valid job name, never a file the owner named otherwise.
  - A file with the same stem in another letter case or suffix makes a create or replace fail with 409, since the catalog finds jobs by stem and macOS file systems usually ignore case.
  - A replace with unchanged text makes no backup.
  - `.gitignore` gains `data/jobs/.trash/` and `data/jobs/.backups/`: `data/**` ignores them, but `!data/**/*example*` would bring back a trashed example job.
- **Change** [M07]: `.trash/` and `.backups/` are under `data/jobs/`, where Phase 2 moved the job files. The owner decision of 2026-09-25 named `data/.trash/` and `data/.backups/` when job files were in `data/`; the decision itself is unchanged.
- **Design decision** [M08]: `mcp_server/` imports nothing else from the package, so `cli/app.py` passes it the server URL and the token file's path, and `dtc serve` and `dtc mcp` become allowed imports between front ends, as `dtc tui` is.
  - `dtc mcp` keeps stdout for the protocol.
  - The tool list is read from `/capabilities` whenever a client asks for it, not once at start, so it follows a server restarted with or without `--allow-write`.
  - Tools are named after the endpoints (`get_queue_entry`, `validate_job_text`).
  - The full flow is tested in process, through the SDK's in-memory session and `httpx` sent straight to the app, with one test over stdio against a process. An end-to-end stdio test against a live API would need a listening server in the tests.
- **Design decision** [M09]: The credential test changes. A job cannot produce `--api-key` or `--remote-shared-secret`, since its keys reach only the override flags, so "submit a job whose command would include them" cannot be written. A structural test checks that no job key reaches a secret flag, and a run stored with an unredacted command must be served redacted. The acceptance criterion that `README.md` documents the server becomes the user guide, since the README stays short.

- **Change**: Phase 2 is done and archived under `docs/archive/phase-2/`; links from these documents now point there. The plans were checked against the code: `load_job_text` and the `lock` parameter of `JobRunSession.run` already exist, so Milestone 01 no longer lists them as new. `start_run` and the input override in `JobRunOptions`, the `queue` table, and the `fastapi`, `uvicorn`, `httpx`, and `mcp` dependencies are still to build. No decision changed.

- **Change**: [Phase 2 Milestone 11](../archive/phase-2/milestone-11-clean-architecture.md) renamed and moved the code these plans name: `JobService` is `JobExecutor` (with `JobRunOptions`), `jobs/job_definition.py` is `jobs/parsing.py`, and `install_signals` is `handle_signals`. The queue is a repository in `state/` with a worker on `JobRunSession`, not `jobs/job_queue.py`. The milestone documents were updated to match; no decision changed.

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
- **Design decision** [M07]: Agents submit job YAML text, not structured
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
  first planned for M09; moved so nothing that accepts agent input ships
  without them.)
- **Design decision** [M02]: Every write action and every submission is
  recorded in an `audit_log` table (time, action, job name, outcome), without
  credentials or prompt text. Built with the first endpoints, like the limits
  (first planned for M09); M09 tests that coverage is complete.

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
