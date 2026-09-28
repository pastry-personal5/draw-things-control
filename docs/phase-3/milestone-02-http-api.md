# Milestone 02: HTTP API: Read and Run

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md)

## Goal

Expose job discovery, queueing, monitoring, cancellation, and resume over a
local HTTP API and a gRPC monitoring service, both authenticated, without
yet allowing any file to be written.

## Scope

In scope:

- `dtc serve`, running the API, the gRPC monitoring service, and the queue
  worker in one process
- Bearer-token authentication on both, on loopback unless the owner allows
  otherwise
- Read and run endpoints over HTTP; watching for change over gRPC
- The rules and limits every job the API runs must meet, and the audit log,
  from the first endpoint that accepts input

Out of scope:

- Creating, editing, deleting, or validating job text (Milestone 07)
- The MCP server (Milestone 08)
- TLS and users. A bind beyond loopback is the owner's explicit choice
  (`--allow-remote-bind`); an SSH tunnel is the safer way in from elsewhere.

## Planned changes

### The `serve` command

- `dtc serve [--host 127.0.0.1] [--port 8765] [--grpc-port 8766] [--executable draw-things-cli] [--shutdown-grace 10] [--global-config PATH] [--allow-remote-bind]`
  runs in the foreground: uvicorn with the FastAPI app from `server/`, and a
  `grpc.aio.server()` for monitoring ([below](#monitoring-grpc)), started
  and stopped together. `--executable` and `--shutdown-grace` mean what they
  mean for `run-job`.
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
  rebinding. This check is HTTP-only ([below](#monitoring-grpc): gRPC has no
  equivalent gap).
- There is no `--data-dir`: the server serves the project's `data/jobs/`,
  the one directory whose files have job IDs and the one Milestone 07
  writes.
- On start it reads the global configuration (exit 2 if invalid; restart
  the server to read it again), takes the run lock (exit 75 if busy, as any
  runner), and recovers the queue (Milestone 01). It then binds both ports
  (`--port` and `--grpc-port`) before starting the worker: either taken is
  an `invalid_input` error (exit 2) naming the flag, raised before the
  worker could claim a queued entry that the refusal would otherwise abandon
  as `interrupted`. It then prints the address and the token file's path,
  never the token.
- uvicorn installs its own signal handlers, so the worker's executor is
  built with `handle_signals=False` and uvicorn's shutdown hook stops the
  worker as in [Milestone 01](milestone-01-queue-run-manager.md#shutdown).
  The gRPC server is stopped, and open watch streams cancelled, first, so
  neither can hold shutdown up.
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
| `GET /queue/{id}` | One entry: state, execution ID, current run and its elapsed time, the current run's diffusion step, the last run's time, `cooldown_until`, error, and whether it can be resumed, from which run, or why not |
| `POST /queue/{id}/cancel` | Cancel ([Milestone 01](milestone-01-queue-run-manager.md#cancel) rules) |
| `POST /queue/{id}/resume` | Resume ([Milestone 01](milestone-01-queue-run-manager.md#resume) rules); returns the new entry |
| `GET /executions` | Executions, newest first, filterable by job reference (resolved like `{job}`) and status |
| `GET /executions/{id}` | One execution (`E0012`) with its runs |
| `GET /executions/{id}/outputs` | Each run's output and last frame: path, whether it exists, whether it is complete, bytes, measured width, height, and frames; never the content |
| `GET /audit` | The audit log (below) |

Watching for change is not HTTP: it is the gRPC monitoring service
(below). There is no `GET /events` and no `?wait=` parameter.

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
- `GET /health` always answers 200 while the HTTP process itself is up; a
  dead worker thread ([Milestone
  01](milestone-01-queue-run-manager.md#worker)) is reported only in the
  body (`worker_alive: false`), not as a different status code, so a caller
  must read the field, not just the status.
- `GET /jobs`, `GET /executions`, `GET /inputs`, and `GET /audit` are paged
  with `limit` (default and maximum 200, matching `services/history.py`'s
  existing `PAGE_SIZE`) and a `cursor`: an opaque, server-chosen token a
  client passes back unmodified to get the next page and never constructs
  itself, so the scheme behind it can change later without breaking a
  client. `GET /queue` pages the same way, but only its finished entries,
  newest first: queued and running ones always come back in full, on the
  first page only, ahead of the finished page (`?state=` naming one of them
  is unpaged either way, as before). Queued and running entries are bounded
  by `max_queued_jobs` (and there is at most one running), but finished ones
  are not, once `history_retention_days: 0` keeps them forever, so unlike
  the queue itself, its history needs the same paging the other list
  endpoints already have. `GET /capabilities` is not paged: it has no list
  to page.

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

A missing or wrong token is 401. A rejected `Host` header is the table's one
exception: `invalid_input`, but 400, not 422 — the ordinary status for a
request naming the wrong server, which 422 is not a natural fit for (owner
decision). The new codes (`timeout_required`,
`outside_directory`, `limit_exceeded`, `invalid_state`) are `DtcError`
subclasses in `core/errors.py`. `invalid_state` needs a code of its own:
`CancelRefusedError` and `ResumeRefusedError`
([Milestone 01](milestone-01-queue-run-manager.md)) are plain `InputError`
subclasses today, with no `code` of their own, so without a change here they
would read `invalid_input` (422) instead of the `invalid_state` (409) this
table already calls for; this milestone gives both `code = "invalid_state"`.
`EXIT_CODES_BY_ERROR_CODE` (`core/exit_codes.py`) gains a row for each of the
four new codes, all exiting 2 (`EXIT_INVALID_INPUT`), since
[Milestone 03](milestone-03-queue-for-people.md)'s `dtc queue` already maps
an API error to an exit code through that same table.

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

### Monitoring (gRPC)

Watching for change (the whole event firehose, and waiting on one queue
entry) is a `grpc.aio.server()` beside uvicorn in the same `dtc serve`
process, not an HTTP endpoint (design decision, superseding the SSE plan of
2026-09-25: see the phase-3 changelog). `--grpc-port` (default 8766) shares
`--host` and `--allow-remote-bind` with the HTTP port: one loopback-or-not
decision for the whole process, not two to remember.

- The `.proto` file lives at `server/proto/monitor.proto` (only `server/`
  imports the generated server code from it); `mcp_server/`, `tui/`, and
  `cli/` (`dtc queue add --wait`) each import their own generated client
  stubs from the same file. Nothing generated is committed: `make proto`
  (`grpcio-tools`, dev-only) regenerates every one of them into a
  `.gitignore`d directory, and `make check` depends on that step, so it
  always runs against the `.proto` file's current shape.
- `Monitor` has two server-streaming RPCs, unary request, streamed response,
  since nothing here needs a reply channel back (writes stay HTTP `POST`):
  - `WatchEvents(last_event_id, include_output) returns (stream Event)`
    replaces `GET /events`: the Phase 2 job events (`event_to_dict`) and the
    queue's own (an entry's state change, and the start and end of a
    between-jobs wait, each naming the entry). `Event.id` increases within
    one run of the server; a client reconnecting with an earlier run's ID,
    or one older than the server's bounded in-memory backlog, gets a
    `Reset` event and should read the state again over HTTP before
    resubscribing. `include_output` asks for child output lines
    (`run_output`) too, since they are many.
  - `WatchQueueEntry(id) returns (stream QueueEntrySnapshot)` replaces
    `GET /queue/{id}?wait=`: one message whenever the entry's state,
    execution ID, current run, current step, `cooldown_until`, or error
    changes, until the client cancels the call. `dtc mcp`'s `get_queue_entry` tool (with
    `wait_seconds`) and the TUI's Queue widget both read this instead of
    polling.
- Authentication is a server interceptor reading the `authorization`
  metadata key (`Bearer <token>`, `hmac.compare_digest`, the same token file
  as the HTTP API); missing or wrong, the call ends `UNAUTHENTICATED`, never
  logged. A caller identifies itself the same way the HTTP API's header does
  (`mcp`, `tui`), for the busy message and error text only, never audited:
  neither RPC writes, so nothing here reaches the audit log.
  DNS-rebinding-style spoofing does not apply the way it does to a browser
  hitting `GET /events`: a gRPC channel dials the bound host and port
  directly, with no virtual-hosting `Host` header to fake, so there is no
  second check to add.
- No TLS (unchanged non-goal): `grpc.aio.server()` binds an insecure port, as
  `--allow-remote-bind` already warns the HTTP token does in plain text.
- Shutdown stops accepting new calls and cancels open streams before the
  worker itself stops, as the SSE plan already ended open streams first.

### Audit log

Built with the first endpoints that accept input, so no submission or write
goes unrecorded.

- An `audit_log` table in the state store (schema 5): time, action
  (`submit`, `cancel`, `resume`, and, from
  [Milestone 07](milestone-07-job-file-management.md), `create_job`,
  `replace_job`, `delete_job`), the target (a job reference or an entry ID),
  the outcome (`ok` or the error code), and the caller. `X-Dtc-Caller`, set
  by the MCP server, `dtc queue`, and the TUI's Queue widget (`mcp`, `cli`,
  `tui`), is checked on every endpoint the audit log covers, after
  authentication: absent, it defaults to `api`; any other value is refused
  with `invalid_input` naming the field. The caller still only names itself,
  so the column informs rather than proves, but the check keeps it to a
  known, closed set.
- One entry for every such request, refused ones included. An
  unauthenticated request is rejected before it is recorded. Two refusals
  happen before an endpoint's own body ever runs -- an unknown
  `X-Dtc-Caller` and, for `POST /queue`, a body FastAPI's own validation
  refuses -- and are still recorded, with the documented default caller
  `api` (owner decision): the value that failed is not trustworthy enough to
  store in the caller column itself.
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

The user guide gains a "Server" section: starting `serve`, the token, the
gRPC port, what `--allow-remote-bind` exposes, the endpoints, the rules and
limits, that a stop or a cancel loses the run in progress and a resume
reruns it, and that the CLI and the TUI cannot start runs while the server
is up. `docs/architecture.md` lists the `server/` modules, the worker, and
the gRPC service.

## Acceptance criteria

- Without the token, or with it only in the query string, every endpoint
  but `/health` returns 401; with it, each works. The token file is created
  with mode 0600, and a looser existing file is refused.
- `serve` refuses a non-loopback `--host` without `--allow-remote-bind`
  (exit code 2), and accepts it with the flag and a warning. The API refuses
  a `Host` header that names neither loopback nor the bound address.
- A job is listed, previewed, submitted, watched to the end through
  `WatchEvents` and `WatchQueueEntry` (a fake gRPC channel against the
  service, no network socket), and its outputs read over HTTP with a fake
  runner (`TestClient`). A run the fake runner stops mid-way leaves a file
  that `/executions/{id}/outputs` lists as incomplete, and the entry says it
  can be resumed from that run.
- A reference like `../x`, `a/b`, or `/etc/passwd`, or one naming a symbolic
  link, returns 4xx and reads no file.
- Cancel and resume behave as in Milestone 01, with the status codes above.
- `WatchEvents` delivers events in order, honors `last_event_id`, and sends
  `Reset` when the backlog is exceeded or the ID is from another run. A
  gRPC call without the token, or with it wrong, ends `UNAUTHENTICATED`;
  `--grpc-port` follows the same loopback rule as `--port`.
- Each submit, cancel, and resume leaves one audit entry, refused ones
  included; an unauthenticated request leaves none.
- Each limit refuses an entry over it with the `code`, key, limit, and
  value, and accepts one at it. A job without `run_timeout_seconds`, or with
  its input or output directory outside, is refused over the API, naming
  the field, and still runs with `run-job`.
- No response, event, or log line contains the token, and a stored command
  holding a credential is served redacted.
- `make check` passes; the user guide documents `serve`.
