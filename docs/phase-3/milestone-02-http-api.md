# Milestone 02: HTTP API: Read and Run

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md)

## Goal

Expose job discovery, queueing, monitoring, and cancellation over a local
HTTP API with authentication, without yet allowing any file to be written.

## Scope

In scope:

- `dtc serve`, running the API and the queue worker in one process
- Bearer-token authentication
- Read, run, and monitor endpoints, and a live event stream

Out of scope:

- Creating, editing, or deleting job files (Milestone 03)
- The MCP server (Milestone 04)
- TLS, users, or any network exposure

## Planned changes

### The `serve` command

- `dtc serve [--host 127.0.0.1] [--port 8765] [--global-config PATH] [--data-dir PATH] [--allow-remote-bind]`.
  Runs in the foreground (uvicorn with the FastAPI app from `server/`).
- A host that is not loopback is refused unless `--allow-remote-bind` is
  given.
- On start it takes the run lock (Milestone 01), recovers state, starts the
  worker, and prints the address and the token file path. If the lock is
  busy it exits with 75, as any runner does.
- Graceful shutdown as in Milestone 01. uvicorn installs its own signal
  handlers, so the app runs jobs with `install_signals=False` and stops the
  current job from uvicorn's shutdown hook through `cancel()`.

### Authentication

- The token is 32 random bytes, hex-encoded, in `config/server-token`,
  created with mode 0600 on first start if absent, and added to
  `.gitignore`. An existing file with looser permissions is refused with a
  message, not silently fixed.
- Every endpoint except `GET /health` requires
  `Authorization: Bearer <token>`. The token is compared with
  `hmac.compare_digest`. A missing or wrong token is 401 with no detail.
- The token is never logged or returned.

### Endpoints (all under `/v1`)

| Method and path | Purpose |
|-----------------|---------|
| `GET /health` | Liveness (no auth): server up, version |
| `GET /capabilities` | Whether writes are enabled, limits in force |
| `GET /jobs` | Jobs in `data/`: name, mode, runs, validity and first error |
| `GET /jobs/{name}` | The resolved job (prompt pairs, cooldown and source) |
| `POST /jobs/{name}/validate` | Validate the file on disk |
| `GET /jobs/{name}/preview` | The dry-run plan, commands redacted |
| `GET /inputs` | Files in the input directory (name, size, modified time) |
| `POST /queue` | Submit a job by name; returns the queue entry |
| `GET /queue` | Entries, oldest first, filterable by state |
| `GET /queue/{id}` | One entry: state, current run, cooldown end, error |
| `POST /queue/{id}/cancel` | Cancel (Milestone 01 rules) |
| `POST /queue/{id}/resume` | Resume an interrupted or failed entry |
| `GET /history` | Executions, paged, filterable by name and status |
| `GET /history/{id}` | One execution with its runs, redacted |
| `GET /outputs/{execution_id}` | Output paths and metadata (existence, size), no content |
| `GET /events` | Server-sent events for queue and run events |
| `GET /audit` | The audit log, paged (see Audit log below) |

- Names in paths are validated against `[a-z0-9-]` and resolved inside the
  data directory; nothing else can be named.
- Errors are JSON with a stable `code`, a `message`, and, for validation,
  the offending `field`, like the CLI's messages.
- Lists are paged with `limit` and `cursor`.

### Event stream

- `GET /events` streams the Phase 2 job events (as JSON) plus queue state
  changes, with an increasing id per event, so a client can resume with
  `Last-Event-ID`. The server keeps a bounded in-memory backlog; a client
  that falls behind it gets a `reset` event and should re-read state.
- Child output lines (`RunOutput`) are included only when the request asks
  for them (`?output=1`), since they are large.

### Limits

Bounded work is enforced from the first submission, not added later. Optional
global configuration keys, validated like the existing ones and documented in
`config/global-config.example.yaml` (the app never edits
`config/global-config.yaml`):

| Key | Meaning | Default |
|-----|---------|---------|
| `max_queued_jobs` | Entries in `queued`, at once | 20 |
| `max_job_runs` | `run_count` a job submitted through the API may have | 50 |
| `max_job_seconds` | Worst case for one job: `run_count` times `run_timeout_seconds`, plus cooldowns between runs | 43200 (12 h) |
| `max_job_file_bytes` | Size of a job YAML written through the API (Milestone 03) | 65536 |

- The limits apply to `POST /queue` and, for the size, to `PUT /jobs/{name}`,
  whoever calls, and with `--allow-write` on too; that flag is not a way
  around them. The CLI and the TUI are not limited.
- A job submitted through the API must set `run_timeout_seconds`; without
  it the worst case is unbounded, so the submission is refused with
  `code: timeout_required` naming the field. (There is no global default
  timeout, and none is added; owner decision.)
- A limit failure is a 4xx with `code: limit_exceeded`, the key that was
  exceeded, and the limit. `GET /capabilities` reports the limits so an
  agent can plan.

### Audit log

Built with the first endpoints that accept input, so no submission or write is
unrecorded.

- An `audit_log` table in the state store: id, time, action (`submit`,
  `cancel`, `resume`, and, from [Milestone 03](milestone-03-job-file-management.md),
  `create_job`, `replace_job`, `delete_job`), target name or id, outcome
  (`ok` or a refusal code), and the caller (`api`, or `mcp` from a header the
  MCP server sets).
- One entry for every such request, including refused ones (bad token
  excepted: an unauthenticated request is rejected before it is recorded).
- No prompt text, YAML content, command, or credential is stored in it.
- It is not pruned by `history_retention_days`: its rows are small and it is
  the record of what agents did.
- `GET /audit` reads it, paged. Nothing can write to it but the server, and
  no endpoint deletes from it.

### Serialization

Responses come from typed models. Commands are always redacted. Prompts are
shown, since they are job content the caller can already read from the job.

## Acceptance criteria

- Without the token every endpoint except `/health` returns 401; with it,
  each works. The token file is created with mode 0600, and a looser
  existing file is refused.
- `serve` refuses `--host 0.0.0.0` without `--allow-remote-bind`.
- A job listed, previewed, submitted, watched to the end, and its outputs
  read, all over HTTP, with a fake runner (`TestClient`).
- An invalid name or a path-like name returns 4xx and reads no file.
- Cancel and resume behave as in Milestone 01.
- `GET /events` delivers events in order, honors `Last-Event-ID`, and sends
  `reset` when the backlog is exceeded.
- Each submit, cancel, and resume leaves one audit entry, refused ones
  included; an unauthenticated request leaves none.
- Each limit rejects an over-limit submission with the `code`, key, and
  limit, and accepts one at the limit. A job without `run_timeout_seconds`
  is refused over the API and still runs from the CLI.
- No response or event contains a credential value.
- `make check` passes.
