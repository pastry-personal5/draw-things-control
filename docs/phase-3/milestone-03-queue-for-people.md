# Milestone 03: Queue for People

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md), [Milestone 02: HTTP API](milestone-02-http-api.md)

## Goal

Give a person the server's queue from the CLI and the TUI, and retire every
other way to run a job: `dtc serve`'s worker becomes the only thing that
ever invokes `draw-things-cli` (owner decision). `run-job` is removed;
`dtc queue add` (with a new `--wait`) replaces it, and the TUI's `/apply`
submits to the queue instead of running the job itself. Both submit, list,
cancel, and resume through the API, and both can watch an entry update
live, over the gRPC monitoring service
([Milestone 02](milestone-02-http-api.md#monitoring-grpc)).

## Scope

In scope (owner decision):

- `dtc queue` subcommands, as a client of the API, including `add --wait`
- A Queue widget in the TUI that submits, cancels, and resumes through the
  API too, not only shows the queue; `/get queue`, and `/describe` for an
  entry
- Removing `run-job` and its direct-execution path in the TUI's `/apply`

Out of scope:

- Following a job live from any other command: `dtc queue`'s only gRPC use
  is `add --wait` watching the one entry it just submitted
- Running any job without the server, at all
- Resuming an execution recorded before this milestone, or any execution
  with no snapshot of its own (it predates the queue, or the server crashed
  before recording one)

## Planned changes

### The `queue` commands

| Command | API |
|---------|-----|
| `dtc queue add JOB [--wait]` | `POST /queue`, then `WatchQueueEntry` if `--wait` |
| `dtc queue list [--state STATE]` | `GET /queue` |
| `dtc queue show <Queue ID>` | `GET /queue/{id}` |
| `dtc queue cancel <Queue ID>` | `POST /queue/{id}/cancel` |
| `dtc queue resume <Queue ID>` | `POST /queue/{id}/resume` |

- `JOB` is a job ID or a job file's name, as the API takes, or a path to a
  file directly in `data/jobs/`, which the command turns into its name. The
  server runs only the files there.
- `--wait` (`run-job`'s replacement) submits, prints the entry's ID, then
  reads `WatchQueueEntry` and prints each run as it starts and finishes,
  until the entry reaches a final state; it exits 0 for `succeeded`, and
  with the mapped error code otherwise. Ctrl-C cancels the entry
  ([Milestone 01](milestone-01-queue-run-manager.md#cancel) rules) before
  the command exits, as `run-job`'s own Ctrl-C did. Without `--wait`, `add`
  returns as soon as the entry is queued, as before.
- A queued job meets the same
  [rules and limits](milestone-02-http-api.md#rules-for-jobs-the-api-runs)
  as an agent's (owner decision: the API cannot tell a person from an agent,
  since they share the token). The audit log names the caller `cli`.
- Each command takes `--server-url` (default `http://127.0.0.1:8765`),
  `--token-file` (default `ProjectPaths.server_token`), and
  `--allow-remote-server`, with the same rule as `dtc mcp`.
- Output is text for people, the entry's ID first
  (`Q0007 queued: walk.yaml`); `list` is a table, and `show` gives the
  state, the execution ID, the current run, the end of any wait, the error,
  and whether the entry can be resumed, from which run, or why not.
  `cancel` stops a running entry at once, losing the run in progress
  ([Milestone 01](milestone-01-queue-run-manager.md#resume)), and says so.
- An API error exits with the code the CLI gives the same error code
  (`EXIT_CODES_BY_ERROR_CODE`; the new codes exit with 2). A server that
  cannot be reached, or that refuses the token, exits with 1 and a message
  naming `dtc serve`. The token is never printed.
- The HTTP client lives in `cli/`, importing `httpx` only inside the
  commands, beside the gRPC client `add --wait` uses. `mcp_server/` and
  `tui/` keep their own: front ends never import each other, and
  `mcp_server/` imports nothing else from the package.

### Retiring `run-job`

- The `run-job` command and its module are removed from `cli/`. It ran
  `draw-things-cli` itself, taking the run lock directly
  ([Phase 1](../archive/phase-1/README.md)); `dtc queue add --wait` is its
  replacement, going through the server instead.
- `run-job`'s own `--executable` and `--shutdown-grace` have no equivalent
  on `dtc queue add`: they governed how `run-job` drove `draw-things-cli`
  directly, and only `dtc serve`'s own flags of the same name mean anything
  now, since only the server ever starts a run.
- Anything that scripted `run-job` breaks outright and must move to
  `dtc queue add --wait`, which needs a running `dtc serve` that `run-job`
  never did (**Change**, recorded in the phase-3 changelog).

### The TUI's queue

- A Queue widget in the right column, between Job Definition and Execution
  History, always shown, 5 rows (owner decision): each entry's ID, its job
  file's name without the extension, its state, and its runs (succeeded of
  total, or `run 3/7` while it runs). The running entry comes first, then
  the queued ones in order, then the finished ones, newest first.
- The minimum terminal size grows by the widget's height, to about 80x41,
  measured when it is built; the user guide gives the new size.
- While the server is up, the widget subscribes to `WatchEvents` (the same
  gRPC client the MCP server uses) and updates on every entry it names,
  instead of polling; while it is down, it falls back to the read-only
  reader in `services/`, beside `HistoryReader`, not over HTTP, on the
  history's 5-second check, exactly as before. Either way it shows the queue
  whether or not the server is up.
- `/queue add [JOB]` submits (the current Job Definition's file when `JOB`
  is omitted), `/queue cancel Q0007` and `/queue resume Q0007` act on an
  entry: all three call the API with `httpx`, the same rules and limits as
  `dtc queue` and the caller header `tui`, and show the API's error inline,
  naming `dtc serve` when it cannot be reached. `c` on a selected row is a
  shortcut for `/queue cancel` on it.
- `/apply` becomes an alias for `/queue add` with no `JOB`: it no longer
  runs the current Job Definition itself
  ([Phase 2](../archive/phase-2/README.md)), since the TUI never invokes
  `draw-things-cli` (**Change**). Its old busy message ("another run is in
  progress") no longer applies to it; a server it cannot reach is the only
  way it fails now.
- `/get queue` prints the entries in Messages. `/describe Q0007` (the noun
  inferred from the letter, as for `J` and `E`) prints one entry: its state,
  job, times, execution ID, the entry it resumes and the one that resumes
  it, and its error. Enter on a row does the same. Tab completes queue IDs,
  and moves command line, Job Definition, Queue, Execution History,
  Execution.

### Documentation

The user guide adds `dtc queue` (`add --wait` included) to the commands and
the server section, and the Queue widget, its commands, `/apply`'s new
meaning, and the new minimum size to the TUI section. `run-job`'s section is
removed.

## Acceptance criteria

- Each `dtc queue` command, run with `CliRunner` against the app (a test
  passes an `httpx` transport and a fake gRPC channel to it), calls its
  endpoint, prints the result, and exits with the mapped code on an error; a
  job that breaks a rule is refused naming the field, with exit code 2.
- `dtc queue add --wait` against a fake runner prints each run as it starts
  and finishes, exits 0 when the entry succeeds, and with the mapped code
  when it fails, is cancelled, or is interrupted; a Ctrl-C during the wait
  cancels the entry first. Without `--wait`, `add` returns immediately, as
  before.
- With no server, or a wrong token, each command, `add --wait` included,
  exits with 1 and names `dtc serve`; a non-loopback `--server-url` is
  refused without `--allow-remote-server`. The token never appears in the
  output.
- `run-job` is gone: the CLI has no command by that name, and nothing in
  `cli/` imports `draw-things-cli`'s runner directly any more.
- The Queue widget lists a test store's entries in the stated order, and
  updates live from a fake `WatchEvents` stream while the server is up, and
  from the store's reader, on the 5-second check, while it is down; `/get
  queue`, `/describe Q0007`, and Tab completion work either way.
- `/queue add`, `/apply` (its alias), `/queue cancel`, and `c` on a row call
  the API (a fake `httpx` transport) with caller `tui`, meet the same rules
  and limits as `dtc queue`, and show its error inline, naming `dtc serve`
  when the server cannot be reached. `/apply` no longer starts
  `draw-things-cli` itself under any circumstance.
- At the new minimum size, every widget keeps its rows.
- Browsing the queue writes nothing to the state store; only `/queue add`
  (and `/apply`), `/queue cancel`, and `/queue resume` do, and only through
  the API.
- `make check` passes; the user guide documents `dtc queue`, `/apply`'s new
  meaning, and the Queue widget, and no longer documents `run-job`.
