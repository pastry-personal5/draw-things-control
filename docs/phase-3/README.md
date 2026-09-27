# Phase 3: API Server and MCP Server for AI

**Status:** planned

## Goal

Let an AI agent, or any local program, drive long-horizon video generation
safely: draft, validate, queue, monitor, cancel, and resume chained jobs. A
long-running server owns the queue and the GPU; an MCP server gives agents
typed tools on top of it.

## Scope

- A persistent job queue in the SQLite state store, run by one worker, with
  the same cooldown between queued jobs as between runs. Each entry is a
  snapshot of the job and its base configuration, so what runs is what was
  validated.
- Restart handling, and an explicit resume that continues an interrupted
  chain from its last succeeded run. The run is the smallest unit that can
  be resumed, since `draw-things-cli` keeps nothing of a run it did not
  finish ([research](../research/draw-things-cli-resume.md)).
- An HTTP API (`dtc serve`), on loopback unless the owner passes
  `--allow-remote-bind`, with bearer-token auth, job control, history, and a
  live event stream
- Rules and limits for every job the API runs or writes (a run timeout, the
  input and output directories, runs, worst-case time, file size), and an
  audit log, built with the first endpoints that accept input
- Job file management for agents: validate a draft, and create, edit, and
  delete jobs in `data/jobs/` behind an explicit write flag, with backups
  and a trash folder
- An MCP server (`dtc mcp`) that is a thin client of the HTTP API
- A security review and test suite over the whole agent-facing surface
- The queue for people: `dtc queue` commands through the API, and a
  read-only Queue widget in the TUI

## Non-goals

- Editing `config/global-config.yaml` or anything in `data/params/` through
  any interface
- Parallel generation, TLS, or multi-user hosting
- Uploading images, or using a generated output as a new job's input
  (owner decision). Jobs can use only files already in the configured input
  directory; only a resume continues from an output, so an agent writes
  every prompt of a chain into one job.
- Returning file contents or thumbnails. Agents get paths and metadata.
- Letting a caller set arbitrary `draw-things-cli` flags. Only what a job
  file can express is allowed.
- A background daemon manager (`serve start/stop`, launchd files). The server
  runs in the foreground.
- Changing the queue from the TUI, and resuming an execution that `run-job`
  or the TUI started. While the server is up, the CLI and the TUI cannot
  start runs; `dtc queue` submits to the server instead.
- Resuming within a run, letting a run finish before a stop or a cancel
  takes effect, retrying a failed run on its own, and pausing the queue
  (owner decisions). A stop or a cancel loses the run in progress; stopping
  the server stops the queue.
- More than one level of access: whoever holds the token sees every job and
  every execution.

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 01 | [Queue and run manager](milestone-01-queue-run-manager.md) | planned |
| 02 | [HTTP API: read and run](milestone-02-http-api.md) | planned |
| 03 | [Queue for people](milestone-03-queue-for-people.md) | planned |
| 07 | [Job file management](milestone-07-job-file-management.md) | planned |
| 08 | [MCP server](milestone-08-mcp-server.md) | planned |
| 09 | [Safety hardening](milestone-09-safety-hardening.md) | planned |

They are built in the order 01, 02, 03, 07, 08, 09 (owner decision):

1. Milestone 01 builds the queue and the worker.
2. After Milestone 02, a program can run and resume jobs that already
   exist, within the rules and limits.
3. Milestone 03 gives people the queue in the CLI and the TUI, before
   agents can write jobs.
4. After Milestone 07, agents can draft and write jobs.
5. Milestone 08 adds MCP.
6. Milestone 09 reviews the whole surface, `dtc queue` included, adds the
   security suite, and closes any gaps it finds.

The rules, the limits, and the audit log arrive with Milestone 02, before
anything can be submitted or written. Each milestone documents what it adds
in the user guide and `docs/architecture.md` as it lands.

## Depends on

[Phase 2](../archive/phase-2/README.md): job events and `cancel()`, the state
store, and the run lock, in the layout that
[Phase 2 Milestone 11](../archive/phase-2/milestone-11-clean-architecture.md)
gives them. The server is one more front end beside the CLI and the TUI.
What changes below it: the state store gains tables by forward migration
(schemas 4 and 5), `JobExecutor` can start a chain at run *k*, the job
parser can take a stored base configuration, and a job's log file is scoped
to that job.

## New dependencies

- `fastapi` and `uvicorn` (the HTTP API)
- `httpx` (the clients in `dtc mcp` and `dtc queue`, and API tests)
- `mcp`, the official Python MCP SDK (the MCP server)

All are installed with a plain `uv sync` (owner decision in the Phase 2
changelog). Their documentation is fetched through Context7 when each
milestone is built, and versions are chosen then.

## Code layout

Inside `src/draw_things_control/`:

- `services/` holds what the server does that is not HTTP, so it is tested
  without a web framework: submitting with the API's rules and limits, the
  queue worker (built on `JobRunSession`), resume, and the event backlog the
  stream reads.
- `state/` gains a queue repository and an audit repository beside the
  others.
- `server/` holds the FastAPI app: authentication, routes, typed responses,
  the event stream, and the error-code-to-status table.
- `mcp_server/` holds the MCP server, which imports nothing else from the
  package and reaches the rest over HTTP only.
- `cli/app.py` starts both (`dtc serve`, `dtc mcp`), the two new allowed
  imports between front ends beside `dtc tui`. `cli/` also holds the
  `dtc queue` commands' own HTTP client.
- `tui/` gains the Queue widget, which reads the queue from the state store
  through a reader in `services/`.
- `ProjectPaths` names the token file, `.trash/`, and `.backups/`.

The parts a server shares with the CLI and the TUI (`Toolkit`,
`JobRunSession`, `JobCatalog`, `HistoryReader`, `event_to_dict`, the error
codes) are in `services/`, `jobs/`, and `core/` already, from
[Phase 2 Milestone 11](../archive/phase-2/milestone-11-clean-architecture.md).

## Changelog

Decisions and notable changes are recorded in
[phase-3-changelog.md](phase-3-changelog.md).

## Exit criteria

- An agent connected over MCP can list jobs and inputs, validate a draft,
  create the job, queue it, watch its status, cancel it, resume it, and read
  its output paths.
- A person can queue, list, cancel, and resume jobs with `dtc queue`, and
  see the queue in the TUI.
- The server holds the run lock while it is up, so nothing else starts a run.
- The server restarts without losing the queue: queued jobs run,
  interrupted jobs are marked, and an explicit resume continues one from its
  last succeeded run with the original seed, never from a run's leftover
  file.
- A queued job runs its snapshot: editing or deleting its job file or its
  base configuration afterwards changes nothing.
- Invalid or out-of-bounds input (bad names, paths outside `data/jobs/`, the
  input directory, or the output directory, oversized jobs) is rejected with
  an error naming the field, and touches nothing.
- Write endpoints and tools do not exist unless the server was started with
  the write flag.
- Delete and overwrite are always recoverable from `.trash/` and `.backups/`.
- Only one `draw-things-cli` runs at a time across the CLI, the TUI, and the
  server.
- No credential value (the API token, `--api-key`, `--remote-shared-secret`)
  appears in any response, event, log, or database row.
- The user guide documents `serve`, `mcp`, `dtc queue`, the Queue widget,
  the write flag, and the limits.
- `make check` passes.
