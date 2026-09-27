# Milestone 03: Queue for People

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md), [Milestone 02: HTTP API](milestone-02-http-api.md)

## Goal

Give a person the server's queue from the CLI and the TUI. While the server
is up, neither can start a run (owner decision), so the CLI submits,
lists, cancels, and resumes through the API, and the TUI shows the queue.

## Scope

In scope (owner decision):

- `dtc queue` subcommands, as a client of the API
- A read-only Queue widget in the TUI, `/get queue`, and `/describe` for an
  entry

Out of scope:

- Submitting, cancelling, or resuming from the TUI: it only shows the queue
- Following a job live from the CLI (`dtc queue watch` was offered and not
  chosen)
- Running queued jobs without the server
- Resuming an execution that `run-job` or the TUI started

## Planned changes

### The `queue` commands

| Command | API |
|---------|-----|
| `dtc queue add JOB` | `POST /queue` |
| `dtc queue list [--state STATE]` | `GET /queue` |
| `dtc queue show <Queue ID>` | `GET /queue/{id}` |
| `dtc queue cancel <Queue ID>` | `POST /queue/{id}/cancel` |
| `dtc queue resume <Queue ID>` | `POST /queue/{id}/resume` |

- `JOB` is a job ID or a job file's name, as the API takes, or a path to a
  file directly in `data/jobs/`, which the command turns into its name. The
  server runs only the files there.
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
  commands. `mcp_server/` keeps its own: front ends never import each other,
  and `mcp_server/` imports nothing else from the package.

### The TUI's queue

- A Queue widget in the right column, between Job Definition and Execution
  History, always shown, 5 rows (owner decision): each entry's ID, its job
  file's name without the extension, its state, and its runs (succeeded of
  total, or `run 3/7` while it runs). The running entry comes first, then
  the queued ones in order, then the finished ones, newest first.
- The minimum terminal size grows by the widget's height, to about 80x41,
  measured when it is built; the user guide gives the new size.
- It reads the queue from the state store through a reader in `services/`,
  beside `HistoryReader`, not over HTTP, so it shows the queue whether or not
  the server is up, as the history shows executions. It never writes. While
  another process holds the run lock, it is read again with the history's
  5-second check.
- `/get queue` prints the entries in Messages. `/describe Q0007` (the noun
  inferred from the letter, as for `J` and `E`) prints one entry: its state,
  job, times, execution ID, the entry it resumes and the one that resumes
  it, and its error. Enter on a row does the same. Tab completes queue IDs,
  and moves command line, Job Definition, Queue, Execution History,
  Execution.
- No key or command cancels, resumes, or submits; `dtc queue` does.

### Documentation

The user guide adds `dtc queue` to the commands and the server section, and
the Queue widget, its commands, and the new minimum size to the TUI section.

## Acceptance criteria

- Each `dtc queue` command, run with `CliRunner` against the app (a test
  passes an `httpx` transport to it), calls its endpoint, prints the
  result, and exits with the mapped code on an error; a job that breaks a
  rule is refused naming the field, with exit code 2.
- With no server, or a wrong token, each command exits with 1 and names
  `dtc serve`; a non-loopback `--server-url` is refused without
  `--allow-remote-server`. The token never appears in the output.
- The Queue widget lists a test store's entries in the stated order,
  updates while another process holds the lock, and offers no action;
  `/get queue`, `/describe Q0007`, and Tab completion work.
- At the new minimum size, every widget keeps its rows.
- Browsing the queue writes nothing to the state store.
- `make check` passes; the user guide documents `dtc queue` and the Queue
  widget.
