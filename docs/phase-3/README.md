# Phase 3: API Server and MCP Server for AI

**Status:** planned

## Goal

Let an AI agent, or any local program, create, validate, queue, monitor, and
cancel generation jobs safely. A long-running server owns the queue and the
GPU; an MCP server gives agents typed tools on top of it.

## Scope

- A persistent job queue in the SQLite state store, run by one worker, with
  the same cooldown between queued jobs as between runs
- Restart handling and an explicit resume for interrupted jobs
- An HTTP API (`dtc serve`) with bearer-token auth, job control, history,
  and a live event stream
- Job file management for agents: create, edit, and delete jobs in `data/`,
  behind an explicit write flag, with backups and a trash folder
- An MCP server (`dtc mcp`) that is a thin client of the HTTP API
- Limits on submissions, path confinement, redaction, and an audit log
  (built with the first API endpoints)

## Non-goals

- Editing `config/global-config.yaml` or `dt-config/*.json` through any
  interface
- Parallel generation, remote or multi-user hosting, or a public network
  bind
- Uploading images. Jobs can use only files already in the configured input
  directory.
- Returning file contents or thumbnails. Agents get paths and metadata.
- Letting a caller set arbitrary `draw-things-cli` flags. Only what a job
  file can express is allowed.
- A background daemon manager (`serve start/stop`, launchd files). The server
  runs in the foreground.

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 01 | [Queue and run manager](milestone-01-queue-run-manager.md) | planned |
| 02 | [HTTP API: read and run](milestone-02-http-api.md) | planned |
| 03 | [Job file management](milestone-03-job-file-management.md) | planned |
| 04 | [MCP server](milestone-04-mcp-server.md) | planned |
| 05 | [Safety hardening](milestone-05-safety-hardening.md) | planned |

Milestones are built in order. After Milestone 02, a program can run jobs
that already exist, within limits; after Milestone 03, agents can write
them; Milestone 04 adds MCP; Milestone 05 adds the security test suite and
closes any gaps it finds. The limits and the audit log arrive with Milestone 02,
before anything can be submitted or written.

## Depends on

[Phase 2](../phase-2/README.md): job events and `cancel()`, the state store,
and the run lock. Those are used unchanged; the server is one more front end
beside the CLI and the TUI.

## New dependencies

- `fastapi` and `uvicorn` (the HTTP API)
- `httpx` (the MCP server's client, and API tests)
- `mcp`, the official Python MCP SDK (the MCP server)

All are installed with a plain `uv sync` (owner decision in the Phase 2
changelog). Their documentation is fetched through Context7 when each
milestone is built, and versions are chosen then.

## Code layout

Inside `src/draw_things_control/`, `server/` holds the API and queue worker,
and `mcp_server/` holds the MCP server. Shared code goes in `core/`, `jobs/`,
or `state/` as fits (for example `jobs/job_queue.py`).

## Changelog

Decisions and notable changes are recorded in
[phase-3-changelog.md](phase-3-changelog.md).

## Exit criteria

- An agent connected over MCP can list jobs and inputs, create and validate a
  job, queue it, watch its status, cancel it, and read its output paths.
- The server holds the run lock while it is up, so nothing else starts a run.
- The server restarts without losing the queue: queued jobs run, interrupted
  jobs are marked, and an explicit resume continues one.
- Invalid or out-of-bounds input (bad names, paths outside `data/` or the
  input directory, oversized jobs) is rejected with an error naming the
  field, and touches nothing.
- Write tools do not exist unless the server was started with the write flag.
- Delete and overwrite are always recoverable from `.trash/` and `.backups/`.
- Only one `draw-things-cli` runs at a time across the CLI, the TUI, and the
  server.
- No credential value appears in any response, event, log, or database row.
- `make check` passes.
