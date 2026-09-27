# Milestone 02: HTTP API: Read and Run

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md)

## Goal

Expose job discovery, queueing, monitoring, cancellation, and resume over a
local HTTP API with authentication, without yet allowing any file to be
written.

## Scope

In scope:

- `dtc serve`, running the API and the queue worker in one process
- Bearer-token authentication, on loopback unless the owner allows otherwise
- Read, run, and monitor endpoints, and a live event stream
- The rules and limits every job the API runs must meet, and the audit log,
  from the first endpoint that accepts input

Out of scope:

- Creating, editing, deleting, or validating job text (Milestone 07)
- The MCP server (Milestone 08)
- TLS and users. A bind beyond loopback is the owner's explicit choice
  (`--allow-remote-bind`); an SSH tunnel is the safer way in from elsewhere.

## Planned changes

### The `serve` command

- `dtc serve [--host 127.0.0.1] [--port 8765] [--executable draw-things-cli] [--shutdown-grace 10] [--global-config PATH] [--allow-remote-bind]`
  runs in the foreground: uvicorn with the FastAPI app from `server/`.
  `--executable` and `--shutdown-grace` mean what they mean for `run-job`.
- `cli/app.py` starts the app, importing FastAPI only inside the command, as
  `dtc tui` does with Textual. That makes `dtc serve` a second allowed import
  between front ends: `FRONT_END_EXCEPTIONS` in `tests/test_architecture.py`,
  the development rules, and `AGENTS.md` name it.
- A host that is not loopback (`127.0.0.1`, `::1`, or `localhost`) is
  refused with exit code 2 unless `--allow-remote-bind` is given (owner
  decision), so a mistyped `--host` cannot expose job control. With it,
  `serve` warns that the token crosses the network in plain HTTP.
- A request whose `Host` header names neither loopback nor the bound
  address is refused, so a web page cannot reach the server by DNS
  rebinding.
- There is no `--data-dir`: the server serves the project's `data/jobs/`,
  the one directory whose files have job IDs and the one Milestone 07
  writes.
- On start it reads the global configuration (exit 2 if invalid; restart
  the server to read it again), takes the run lock (exit 75 if busy, as any
  runner), recovers the queue (Milestone 01), starts the worker, and prints
  the address and the token file's path, never the token.
- uvicorn installs its own signal handlers, so the worker's executor is
  built with `handle_signals=False` and uvicorn's shutdown hook stops the
  worker as in [Milestone 01](milestone-01-queue-run-manager.md#shutdown).
  Open event streams are ended first, so they cannot hold shutdown up.
- Log lines go to stderr through Loguru. None holds a request header.

### Authentication

- The token is 32 random bytes, hex-encoded, in `config/server-token`
  (`ProjectPaths.server_token`), created with mode 0600 on first start if
  absent, and added to `.gitignore`. An existing file that others can read,
  or that another user owns, is refused with a message, not silently fixed.
  To change the token, delete the file and restart the server.
- Every endpoint except `GET /v1/health` requires
  `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. A
  missing or wrong token is 401 with no detail. A token in the query string
  is never accepted.
- The token is never logged or returned. FastAPI's interactive docs are
  off; the OpenAPI schema is served at `/v1/openapi.json`, behind the token.
- There is one level of access: whoever holds the token (an agent through
  MCP, `dtc queue`, any program) sees every job file in `data/jobs/` and
  every execution in the history with its prompts, whichever front end ran
  it (owner decision).

### Job references

- `{job}` in a path is a job ID (`J0001`) or the name of a job file in
  `data/jobs/` (`walk.yaml`, or `walk` when only one file has that stem),
  resolved with `JobCatalog.find` against the directory's listing. It is
  never joined to a path, so no reference can reach a file elsewhere.
- A job's `name:` is not an identifier: two files can share one, and a
  file's name need not match it.
- The server reads regular files only. A symbolic link in `data/jobs/` is
  listed as invalid (`is a symbolic link`) and never read, since a YAML
  error quotes lines of the file it read.

### Endpoints

All under `/v1`:

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | Liveness, without auth: the server is up, its version, and whether the worker is alive |
| `GET /capabilities` | Whether writes are enabled, and the limits in force |
| `GET /jobs` | The job files: job ID, file name, job name, mode, runs, and validity or the first error with its field |
| `GET /jobs/{job}` | The file's text, and the resolved job (prompt pairs, size, seed, cooldown and their sources) or its error |
| `GET /jobs/{job}/preview` | The dry-run plan, commands redacted |
| `GET /inputs` | The images in the input directory: path relative to it, bytes, width and height, modified time |
| `POST /queue` | Submit a job by reference; returns the entry |
| `GET /queue` | Entries, oldest first, filterable by state; the worker's state (`idle`, `running`, `cooling_down`) and `cooldown_until` |
| `GET /queue/{id}` | One entry: state, execution ID, current run and its elapsed time, the last run's time, `cooldown_until`, error, and whether it can be resumed, from which run, or why not; `?wait=N` (at most 30 seconds) answers as soon as any of these changes |
| `POST /queue/{id}/cancel` | Cancel ([Milestone 01](milestone-01-queue-run-manager.md#cancel) rules) |
| `POST /queue/{id}/resume` | Resume ([Milestone 01](milestone-01-queue-run-manager.md#resume) rules); returns the new entry |
| `GET /executions` | Executions, newest first, filterable by job name and status |
| `GET /executions/{id}` | One execution (`E0012`) with its runs |
| `GET /executions/{id}/outputs` | Each run's output and last frame: path, whether it exists, whether it is complete, bytes, measured width, height, and frames; never the content |
| `GET /events` | Server-sent events (below) |
| `GET /audit` | The audit log (below) |

- A queue entry is named by its ID (`Q0007`) and an execution by its
  execution ID (`E0012`), typed as in the TUI: any letter case, leading
  zeros optional. The store's row numbers never appear.
- `GET /inputs` lists the regular files under the input directory whose
  header Pillow reads as an image, by the relative path a job's `input`
  takes; symbolic links are skipped. The size tells an agent whether the
  image matches a job's size or needs `desired_input_width` or
  `desired_input_height`.
- An output is complete only when its run succeeded. A run stopped or failed
  mid-way may leave a truncated file at its output path, which is kept (as
  since Phase 1) but marked incomplete
  ([research](../research/draw-things-cli-resume.md)); a resume never
  starts from one.
- Lists are paged with `limit` (at most 200) and an opaque `cursor`.

### Errors

Errors are JSON with a stable `code`, a `message`, and, when there is one,
the offending `field`, like the CLI's messages. The codes are `DtcError`
codes, so the CLI and the API share them; the server maps them to statuses
in one table, as `EXIT_CODES_BY_ERROR_CODE` does to exit codes:

| Code | Status |
|------|--------|
| `invalid_input`, `timeout_required`, `outside_directory`, `limit_exceeded` | 422 |
| `not_found` | 404 |
| `invalid_state` (cancel a finished entry; a refused resume), `busy` | 409 |
| `tool_missing`, `state_unavailable` | 503 |

A missing or wrong token is 401. The new codes are `DtcError` subclasses in
`core/errors.py`.

### Rules for jobs the API runs

A service in `services/` checks them at every submission and resume, and
[Milestone 07](milestone-07-job-file-management.md) applies the same ones
to every write, so a job an agent can write is a job it can run:

- It sets `run_timeout_seconds`, since without it the worst case is
  unbounded; otherwise `timeout_required`, naming the field (owner
  decision; there is no global default timeout).
- Its `input`, with symbolic links resolved, is inside the input directory,
  and its `output.directory` inside the global `output_directory`; otherwise
  `outside_directory`, naming the field. The first follows the owner
  decision that agents use only files already in the input directory; the
  second keeps a job from creating directories anywhere else. A job file
  that breaks either still runs with `run-job` while no server is up.
- It is within the limits below.

### Limits

Bounded work is enforced from the first submission, not added later. An
optional `api_limits` mapping in the global configuration, validated like
the rest of the file and documented in `config/global-config.example.yaml`
(the app never edits `config/global-config.yaml`):

| Key | Meaning | Default |
|-----|---------|---------|
| `max_queued_jobs` | Entries `queued` at once | 20 |
| `max_job_runs` | Runs a submitted job may have, or a resume may have left | 100 |
| `max_job_seconds` | One entry's worst case: its runs times `run_timeout_seconds`, plus the longest wait between runs times one less than its runs | 172800 (48 h) |
| `max_job_file_bytes` | The size of job text the API accepts (Milestone 07) | 65536 |

- A wait follows only a run that succeeded, so within `run_timeout_seconds`,
  and a longer run never waits less. The longest wait is therefore the
  job's cooldown applied to `run_timeout_seconds`
  (`CooldownPolicy.wait_after`): `manual`'s `seconds`, 0 for `off`, and for
  `auto` its share of the timeout within its bounds. The example job (7 runs,
  `run_timeout_seconds: 3600`, the default `auto`) comes to
  7 × 3600 + 6 × 1800 = 36,000 s.
- The limits apply whoever calls, `dtc queue` included (owner decision: the
  API cannot tell a person from an agent), and with `--allow-write` on too;
  that flag is not a way around them. `run-job` and the TUI, which run jobs
  themselves when no server is up, are not limited.
- A limit failure has `code: limit_exceeded`, the key, the limit, and the
  job's value. A job exactly at a limit is accepted. `GET /capabilities`
  reports the limits so an agent can plan.

### Event stream

- `GET /events` streams the Phase 2 job events (`event_to_dict`) and the
  queue's own: an entry's state change, and the start and end of a
  between-jobs wait, each naming the entry.
- Each event has an ID that increases within one run of the server and names
  that run, so a client can resume with `Last-Event-ID`. The server keeps a
  bounded backlog in memory; a client that falls behind it, or that sends an
  ID from an earlier run of the server, gets a `reset` event and should read
  the state again.
- Child output lines (`run_output`) are sent only when the request asks for
  them (`?output=1`), since they are many. An idle stream gets a comment
  line every 15 seconds.

### Audit log

Built with the first endpoints that accept input, so no submission or write
goes unrecorded.

- An `audit_log` table in the state store (schema 5): time, action
  (`submit`, `cancel`, `resume`, and, from
  [Milestone 07](milestone-07-job-file-management.md), `create_job`,
  `replace_job`, `delete_job`), the target (a job reference or an entry ID),
  the outcome (`ok` or the error code), and the caller (`api`, or `mcp` or
  `cli` from a header the MCP server and `dtc queue` set; the caller names
  itself, so the column informs rather than proves).
- One entry for every such request, refused ones included. An
  unauthenticated request is rejected before it is recorded.
- No prompt text, YAML, command, or credential is stored in it.
- It is not pruned by `history_retention_days`: its rows are small and it is
  the record of what agents did.
- `GET /audit` reads it, newest first. Only the server writes to it, and
  nothing deletes from it.

### Serialization

Responses come from typed models built from the store's rows, the queue's
entries, and `JobPreview`. Commands were redacted when stored, and are
redacted again as they are served (`redact_command`), so an older row
cannot leak a credential. Prompts are shown: they are job content the caller
can already read.

### Documentation

The user guide gains a "Server" section: starting `serve`, the token, what
`--allow-remote-bind` exposes, the endpoints, the rules and limits, that a
stop or a cancel loses the run in progress and a resume reruns it, and that
the CLI and the TUI cannot start runs while the server is up. `docs/architecture.md` lists the `server/`
modules and the worker.

## Acceptance criteria

- Without the token, or with it only in the query string, every endpoint
  but `/health` returns 401; with it, each works. The token file is created
  with mode 0600, and a looser existing file is refused.
- `serve` refuses a non-loopback `--host` without `--allow-remote-bind`
  (exit code 2), and accepts it with the flag and a warning. The API refuses
  a `Host` header that names neither loopback nor the bound address.
- A job is listed, previewed, submitted, watched to the end through
  `/events` and `/queue/{id}?wait=`, and its outputs read, all over HTTP
  with a fake runner (`TestClient`). A run the fake runner stops mid-way
  leaves a file that `/executions/{id}/outputs` lists as incomplete, and the entry says it
  can be resumed from that run.
- A reference like `../x`, `a/b`, or `/etc/passwd`, or one naming a symbolic
  link, returns 4xx and reads no file.
- Cancel and resume behave as in Milestone 01, with the status codes above.
- `GET /events` delivers events in order, honors `Last-Event-ID`, and sends
  `reset` when the backlog is exceeded or the ID is from another run.
- Each submit, cancel, and resume leaves one audit entry, refused ones
  included; an unauthenticated request leaves none.
- Each limit refuses an entry over it with the `code`, key, limit, and
  value, and accepts one at it. A job without `run_timeout_seconds`, or with
  its input or output directory outside, is refused over the API, naming
  the field, and still runs with `run-job`.
- No response, event, or log line contains the token, and a stored command
  holding a credential is served redacted.
- `make check` passes; the user guide documents `serve`.
