# Phase 3: API server and MCP server for AI

**Status:** done (2026-10-04)

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
  finish ([research](../../research/draw-things-cli-resume.md)).
- An HTTP API (`dtc serve`), on loopback unless the owner passes
  `--allow-remote-bind`, with bearer-token auth, job control, and history
- A gRPC monitoring service, alongside the HTTP API in the same `dtc serve`
  process, streaming job and queue events and answering "watch until this
  changes" requests for the TUI and `dtc queue add --wait`; MCP agents
  watch one entry over the API's SSE watch instead
  ([Milestone 10](milestone-10-mcp-server.md#watching-one-entry-over-sse))
- Rules and limits for every job the API runs or writes (a run timeout, the
  input and output directories, runs, worst-case time, file size), and an
  audit log, built with the first endpoints that accept input
- Job file management for agents: validate a draft, and create, edit, and
  delete jobs in `data/jobs/` behind an explicit write flag, with backups
  and a trash folder
- An MCP server (`dtc mcp`) that is a thin client of the HTTP API, its SSE
  watch of a queue entry included
- A security review and test suite over the whole agent-facing surface
- The queue for people: `dtc queue` commands through the API (`add` gains
  `--wait`, replacing `run-job`), and a Queue widget in the TUI that
  submits, cancels, and resumes through the API too, watching entries live
  over gRPC, the same subscription that streams `draw-things-cli`'s own
  output into the TUI's `draw-things-cli` pane, live, since the TUI no
  longer runs it to read that output directly
- Retiring direct chained jobs everywhere but the server: `run-job` is removed,
  and the TUI's `/apply` submits to the queue instead of running the job
  itself. `dtc generate` is a bounded one-off client of that queue
- Parking a running job from the TUI and `dtc queue`: it ends once its
  current run finishes, keeps every run it finished, reads `parked`, and can
  be resumed at the next run. Parking holds the queue until a release, and
  the queue can also be held on its own
  ([Milestone 05](milestone-05-park-and-hold.md))
- Deleting executions from the history, one, several, every one the
  filters show, or, written out as `all`, the whole history, from the TUI (after a confirmation dialog) and `dtc history
  delete`, through the API, and, while writes are on, from MCP after a
  required dry run; never a running one, nor one a queued or
  running entry uses ([Milestone 06](milestone-06-delete-executions.md),
  [Milestone 10](milestone-10-mcp-server.md))
- Video jobs write ProRes 4444 by default (`output.video_format`), the last
  frame has no alpha, and its color decode and the video's `colr` tag both
  follow the color space the video's own stream states
  ([Milestone 08](milestone-08-video-format-and-color.md))
- A chain's colors held to its first image: an unbiased handoff, the first
  image's gamut mapped, drift measured in every run, and, when a job asks, a
  region-aware correction of each handoff and a corrected copy of each clip,
  re-anchored where the prompt pair changes unless the job says not to
  ([Milestone 09](milestone-09-color-preservation.md))

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
- Running a chained job directly from the CLI or the TUI, whether or not a
  server happens to be up (owner decision): `run-job` is retired; `dtc
  queue add [--wait]` and the TUI's `/apply` submit to the queue and
  require the server to be running. Resuming an execution recorded before
  this change, or any execution with no snapshot of its own.
- Resuming within a run, and retrying a failed run on its own (owner
  decisions). A stop or a cancel loses the run in progress; stopping the
  server stops the queue. Letting a run finish first is what parking is for,
  and holding the queue is how to pause it
  ([Milestone 05](milestone-05-park-and-hold.md)).
- More than one level of access: whoever holds the token sees every job and
  every execution. Milestone 10 keeps agents off the queue entries and holds
  people made, by the caller a request declares, which guards agents using
  their tools and limits no one holding the token.

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 01 | [Queue and run manager](milestone-01-queue-run-manager.md) | done |
| 02 | [HTTP API: read and run](milestone-02-http-api.md) | done |
| 03 | [Queue for people](milestone-03-queue-for-people.md) | done |
| 04 | [TUI verbose mode](milestone-04-tui-verbose-mode.md) | done |
| 05 | [Park and hold](milestone-05-park-and-hold.md) | done |
| 06 | [Delete executions](milestone-06-delete-executions.md) | done |
| 07 | [Job file management](milestone-07-job-file-management.md) | done |
| 08 | [Video format and color](milestone-08-video-format-and-color.md) | done |
| 09 | [Color preservation](milestone-09-color-preservation.md) | done |
| 10 | [MCP server](milestone-10-mcp-server.md) | done |
| 11 | [Safety hardening](milestone-11-safety-hardening.md) | done |
| 12 | [Generate through the queue](milestone-12-generate-through-the-queue.md) | done |
| 13 | [MCP over Streamable HTTP](milestone-13-mcp-over-http.md) | done |

The implementation order was 01, 02, 03, 04, 05, 06, 08, 09, 07, 10, 11,
13, 12 (owner decisions). Each milestone document records its own rationale
and implementation; decisions and remaining work are in [open issues](open-issues.md).

## Depends on

[Phase 2](../phase-2/README.md): job events and `cancel()`, the state
store, and the run lock, in the layout that
[Phase 2 Milestone 11](../phase-2/milestone-11-clean-architecture.md)
gives them. The server is one more front end beside the CLI and the TUI.
What changes below it: the state store gains tables and columns by forward
migration (schemas 4 to 9), `JobExecutor` can start a chain at run *k*,
the job parser can take a stored base configuration, and a job's log file is
scoped to that job.

## New dependencies

- `fastapi` and `uvicorn` (the HTTP API)
- `httpx` (the clients in `dtc mcp` and `dtc queue`, and API tests)
- `mcp`, the official Python MCP SDK (the MCP server)
- `grpcio` (the monitoring service and its clients in the TUI and
  `dtc queue add --wait`); `grpcio-tools` and `protobuf`, dev-only, to
  generate the typed stubs from the checked-in `.proto` file. Nothing
  generated is committed: `make check` regenerates the stubs first (a
  `make proto` step it depends on), into a `.gitignore`d directory, so the
  `.proto` file stays the single source of truth with no generated code to
  drift from it

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
- `server/` holds the FastAPI app (authentication, routes, typed responses,
  the error-code-to-status table) and the gRPC monitoring service
  (`grpc.aio.server()`, its own token interceptor, on its own loopback port),
  started and stopped together by `dtc serve`.
- `mcp_server/` holds the MCP server, which imports nothing else from the
  package and reaches the rest over HTTP only, SSE included.
- `cli/app.py` starts both (`dtc serve`, `dtc mcp`), the two new allowed
  imports between front ends beside `dtc tui`. `cli/` also holds the
  `dtc queue` commands' own HTTP client, and a gRPC client `add --wait`
  uses to watch the entry it just submitted to completion. `run-job` and
  its module are removed.
- `tui/` gains the Queue widget: an HTTP client for submit, cancel, and
  resume, and a gRPC client for live updates while the server is up; while
  it is down, the widget falls back to reading the queue from the state
  store through a reader in `services/`, read-only, as before. `/apply` no
  longer runs a job itself; it submits to the queue. The same gRPC
  subscription, with `include_output` on, now also feeds the
  `draw-things-cli` pane, replacing the `JobExecutor` the TUI used to run
  jobs with; nothing under `tui/` starts `draw-things-cli` any more.
- `ProjectPaths` names the token file, `.trash/`, and `.backups/`.

The parts a server shares with the CLI and the TUI (`Toolkit`,
`JobRunSession`, `JobCatalog`, `HistoryReader`, `event_to_dict`, the error
codes) are in `services/`, `jobs/`, and `core/` already, from
[Phase 2 Milestone 11](../phase-2/milestone-11-clean-architecture.md).

## Changelog

Decisions and notable changes are recorded in
[phase-3-changelog.md](phase-3-changelog.md).

## Exit criteria

- An agent connected over MCP can list jobs and inputs, validate a draft,
  create the job, queue it, watch its status, park it, cancel it, resume it,
  hold and release the queue, and read its output paths; while writes are
  on, it can also delete executions
  ([Milestone 10](milestone-10-mcp-server.md)). It cannot cancel, park,
  unpark, or resume an entry a person submitted, or release a person's hold.
- A person can queue, list, cancel, and resume jobs with `dtc queue` (`add
  --wait` blocks until the entry finishes and exits with its outcome code,
  replacing `run-job`) or the TUI's Queue widget, and watch either update
  live while the server runs them.
- A person can park a running entry from the TUI or `dtc queue`. It ends
  `parked` after its current run with nothing lost, and a resume continues
  it at the next run. The queue can be held and released, and a hold
  survives a server restart.
- A person can delete executions from the TUI, after a confirmation dialog,
  or with `dtc history delete`: one, several, every one the filters show, or,
  written out as `all`, the whole history.
  The row, its runs, its log, and its manifest go, its outputs stay. A
  running execution, or one a queued or running entry uses, is refused, and
  a deletion that ends a parked or failed entry's resume says so first.
- `dtc serve`'s worker starts queued jobs and one-off generations; `run-job`
  is retired, the TUI submits to the server, and `dtc generate` queues a
  bounded generation rather than starting one directly.
- The server restarts without losing the queue: queued jobs run (once
  released, if the queue is held), interrupted jobs are marked, and an explicit resume continues one from its
  last succeeded run with the original seed, never from a run's leftover
  file.
- A queued job runs its snapshot: editing or deleting its job file or its
  base configuration afterwards changes nothing.
- Invalid or out-of-bounds input (bad names, paths outside `data/jobs/`, the
  input directory, or the output directory, oversized jobs) is rejected with
  an error naming the field, and touches nothing.
- Malformed caller input receives a typed refusal; a server fault receives a
  generic HTTP 500 `internal_error`. Authenticated writes and submissions,
  refused ones and crashes included while the store is available, leave audit
  rows with a known name or canonical ID or a null target, never raw caller text.
- Write endpoints and tools do not exist unless the server was started with
  the write flag, and neither does the MCP tool that deletes executions.
- Deleting or overwriting a job file is always recoverable from `.trash/` and
  `.backups/`. Deleting an execution is final: the TUI asks first, and MCP
  offers it only while writes are on, with a required dry run.
- Only one `draw-things-cli` runs at a time, machine-wide: only the
  server's worker ever starts one.
- No credential value (the API token, `--api-key`, `--remote-shared-secret`)
  appears in any response, event, gRPC stream, log, or database row.
- The user guide documents `serve`, `mcp`, `dtc queue`, the Queue widget,
  the write flag, and the limits.
- `make check` passes.
