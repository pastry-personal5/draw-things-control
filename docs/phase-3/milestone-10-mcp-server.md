# Milestone 10: MCP Server

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 02: HTTP API](milestone-02-http-api.md), [Milestone 07: Job file management](milestone-07-job-file-management.md)
(its write endpoints, `sha256`, and the `writes_off` answer), and the queue
actions of [Milestone 05](milestone-05-park-and-hold.md) (built before this
one)

## Goal

Give AI agents typed tools and resources for the API, so an agent can do the
whole draft, create, queue, watch, cancel, and resume workflow of a long
chain without knowing HTTP.

## Scope

In scope:

- `dtc mcp`, an MCP server over stdio, with the three client options
  `dtc queue` and `dtc tui` take
- Tools that map one to one to API endpoints, with the API's names and
  bounds
- Write tools listed only while the API server has writes on, and a
  list-changed notification when that changes
- A bounded wait in `get_queue_entry`, over gRPC
- Job files as read-only resources
- Clear errors when the API server is not running or refuses the token

Out of scope:

- Any generation, queue, or file logic of its own. It only calls the API.
- Direct access to the GPU, the database, the global configuration, or job
  files
- Transports other than stdio, or network exposure
- Streaming events to the agent (`WatchEvents`), or notifications of queue
  changes: an agent asks, with `get_queue_entry`'s wait
- Reshaping the API's answers: a tool returns the API's JSON as it is
- The audit log (`GET /v1/audit`) as a tool: it is the owner's record of
  what agents did, not something an agent needs to do its work
- Deleting executions ([Milestone 06](milestone-06-delete-executions.md)):
  pending the [open questions](#open-questions)

## Behavior

### Starting it

- `dtc mcp [--server-url http://127.0.0.1:8765] [--token-file PATH]
  [--allow-remote-server]` runs an MCP server on stdio, built with the
  official `mcp` Python SDK. The three options are `dtc queue`'s and
  `dtc tui`'s (`cli/api_client.py`'s `ServerUrlOption`, `TokenFileOption`,
  and `AllowRemoteServerOption`), with the same defaults. The token file
  defaults to the project's (`ProjectPaths.server_token`), found from the
  package's own place (`core/paths.py`'s `PROJECT_ROOT`), not from the
  directory the MCP client starts the command in.
- `cli/app.py` checks `--server-url` with `check_server_host` before it
  starts anything, as `dtc tui` does: a URL that is not loopback is refused
  (exit 2) unless `--allow-remote-server` is given, the client's side of
  `serve --allow-remote-bind`, so a mistyped URL cannot send the token
  elsewhere. It then passes the URL and the token file's path to
  `mcp_server/`, which it imports only inside the command.
- `dtc mcp` starts whether or not the API is up, and whether or not the
  token file exists yet: an MCP client starts it with its session, often
  before `dtc serve`. Nothing is read at start. The token file is read at
  the first call, and again after any failure from either client (a refused
  token, a connection error), so a regenerated token or a restarted server
  needs no restart of `dtc mcp`. The token is never printed, logged, or
  returned.
- `dtc mcp` reads neither the global configuration nor the state store;
  what it needs to know of the server, it asks the API.
- Stdout carries the protocol and nothing else. The command removes the two
  sinks `main()` installs (`configure_logging` sends child output to stdout)
  and logs to stderr only, as `dtc tui` removes them while it owns the
  terminal. Nothing is written before the transport starts.
- Because it is a client, several MCP sessions can share one server, and the
  server remains the only process that owns the GPU (owner decision).

### Talking to the server

- HTTP goes through `httpx`'s async client, every request with the bearer
  token and `X-Dtc-Caller: mcp`, a caller `server/caller.py` already knows,
  so the audit log records `mcp` for every action an agent takes.
- gRPC goes through `mcp_server/`'s own generated client stubs, sending the
  same token as `authorization` metadata, for `get_queue_entry`'s wait only;
  every other tool is plain HTTP. The target is derived as
  `dtc queue add --wait` derives it: `GET /v1/health`'s `grpc_port`, on the
  host of `--server-url`.
- An argument that becomes part of a URL path (a job reference, a job name,
  a queue or execution ID) is refused when it is empty or made only of dots,
  and percent-encoded otherwise (`quote(value, safe="")`), so no argument
  can change which endpoint is called. Encoding alone is not enough:
  `quote` leaves `.` and `..` as they are and `httpx` removes dot segments,
  so `..` would reach another route, and an empty argument would reach
  `/v1/jobs/`, a slash redirect `httpx` does not follow. The refusal is
  `invalid_input` naming the argument, in the API's error shape, and no
  request is made. Query values go through `httpx`'s `params`, never into
  the URL's text.
- Beyond that, the MCP server does not check IDs and names itself: the API
  accepts `q7` as well as `Q0007`, for one, and a stricter copy of its
  grammar would refuse what the API takes. The API validates every argument
  and answers `not_found` or `invalid_input`.

### Tools

Read and run (always listed):

| Tool | API | Arguments |
|------|-----|-----------|
| `get_capabilities` | `GET /v1/capabilities` | none |
| `list_jobs` | `GET /v1/jobs` | `limit`, `cursor` |
| `get_job` | `GET /v1/jobs/{job}` | `job` |
| `preview_job` | `GET /v1/jobs/{job}/preview` | `job` |
| `validate_job_text` | `POST /v1/validate` | `yaml`, `name` (optional) |
| `list_inputs` | `GET /v1/inputs` | `limit`, `cursor` |
| `submit_job` | `POST /v1/queue` | `job` |
| `get_queue` | `GET /v1/queue` | `state`, `limit`, `cursor` |
| `get_queue_entry` | `GET /v1/queue/{id}`, after `WatchQueueEntry` with `wait_seconds` | `queue_id`, `wait_seconds` (optional) |
| `cancel_queue_entry` | `POST /v1/queue/{id}/cancel` | `queue_id` |
| `resume_queue_entry` | `POST /v1/queue/{id}/resume` | `queue_id` |
| `list_executions` | `GET /v1/executions` | `status`, `job`, `name`, `limit`, `cursor` |
| `get_execution` | `GET /v1/executions/{id}` | `execution_id` |
| `list_outputs` | `GET /v1/executions/{id}/outputs` | `execution_id` |

Queue control (pending the [open questions](#open-questions)):

| Tool | API | Arguments |
|------|-----|-----------|
| `park_queue_entry` | `POST /v1/queue/{id}/park` | `queue_id` |
| `unpark_queue_entry` | `POST /v1/queue/{id}/unpark` | `queue_id` |
| `hold_queue` | `POST /v1/queue/hold` | none |
| `release_queue` | `POST /v1/queue/release` | none |

Write (listed only while `GET /v1/capabilities` reports writes on):

| Tool | API | Arguments |
|------|-----|-----------|
| `create_job` | `PUT /v1/jobs/{name}` | `name`, `yaml` |
| `replace_job` | `PUT /v1/jobs/{name}?overwrite=1` | `name`, `yaml`, `expected_sha256` |
| `delete_job` | `DELETE /v1/jobs/{name}?expected_sha256=...` | `name`, `expected_sha256` |

- Tool arguments have the API's names and bounds. No argument is a path, a
  `draw-things-cli` flag, or a credential; paths inside job text are
  confined by [Milestone 07](milestone-07-job-file-management.md#validation-before-writing).
  `yaml` is sent as `{"yaml": ...}`, the body
  [Milestone 07](milestone-07-job-file-management.md#endpoints) takes, and
  `expected_sha256` goes in `replace_job`'s body and `delete_job`'s query.
- An unknown argument, a missing one, or one of the wrong type is refused
  with `invalid_input` naming it, before any request; each tool's input
  schema says so (`additionalProperties: false`). Whether the SDK refuses
  an unknown argument itself or each tool checks is settled when it is
  built.
- Tool descriptions carry what an agent needs to succeed the first time:
  - inputs must already be in the input directory and match the job's size
    (`list_inputs` shows both);
  - `run_timeout_seconds` is required and the limits apply
    (`get_capabilities`);
  - `validate_job_text` checks a draft without writing, and, given `name`,
    checks it as a write to that name would;
  - `replace_job` and `delete_job` need the `sha256` that `get_job` last
    returned, and a `conflict` with `current_sha256` means a person changed
    the file since: read it again and redo the edit;
  - `delete_job` moves the file to the trash, and `replace_job` keeps a
    backup;
  - a queued or running job's file cannot be changed;
  - write tools are missing when the server was started without
    `--allow-write`;
  - cancelling a running entry loses the run in progress;
  - a stopped chain continues with `resume_queue_entry`, which reruns the
    run that was cut short (`get_queue_entry` says from which run);
  - an output marked incomplete is a leftover, not a result.
- Each tool carries MCP's annotations, which inform a client and enforce
  nothing: read-only for the reads and `validate_job_text`; destructive for
  `cancel_queue_entry`, which loses the run in progress, and for
  `replace_job` and `delete_job`, which change a file a person may be
  using, recoverably.
- A result is the API's JSON body as it is, as the tool's structured
  result, with no projection: the API stays the one contract, and a field
  it gains reaches agents with no change here. Long lists are paged by the
  API's own `limit` and `cursor`, passed through.
- An error is a tool error whose body is the API's error shape as it is
  (`code`, `message`, and, when the API sends them, `field`, `limit`,
  `value`, and `current_sha256`). The MCP server adds two codes of its own,
  for failures that never reached a route (below), and `invalid_input` for
  an argument it refuses itself.

### The tool list

- The write tools do not exist unless the server has writes on, as the
  endpoints do not (phase exit criterion). The MCP server reads
  `GET /v1/capabilities` when the client asks for the tool list, and again
  before a tool call when its last read is more than 5 seconds old or
  failed. When `allow_write` differs from the list it last gave (a server
  restarted with or without `--allow-write`, or an API that was down when
  the list was read and is up now), it tells the client that the tool list
  changed, with MCP's list-changed notification, and lists the new set when
  asked.
- With the API down, only the read-and-run tools (and the queue control
  tools, if they are offered) are listed.
- A client that does not act on the notification keeps its old list. A
  write tool it calls after writes were turned off gets the API's 405
  `writes_off`, naming `--allow-write`
  ([Milestone 07](milestone-07-job-file-management.md#enabling-writes));
  write tools turned on appear only when it reads the list again. The user
  guide says so.

### Waiting for a change

`get_queue_entry` takes an optional `wait_seconds`, from 1 up to a cap
(30 in the first draft; see the [open questions](#open-questions)). With
it:

- An entry already finished (`succeeded`, `failed`, `cancelled`,
  `interrupted`, or `parked`) is answered at once: nothing more will change.
  `mcp_server/` keeps its own copy of these states, since it imports nothing
  from the package, and a test checks it against `state/queue.py`'s
  `FINISHED_STATES`.
- Otherwise it opens `WatchQueueEntry`. The service always sends the
  entry's current snapshot first, so the first message is the baseline, not
  a change. The tool waits for a later message that differs from it in a
  field an agent acts on: `state`, `execution_id`, `current_run`, `error`,
  `park_requested`, `queue_held`, or `cooldown_until`. `current_step` and
  `current_step_total` are ignored: they change with every diffusion step,
  and would end every wait within seconds while a run is going.
- When such a message arrives, or the time is up, it closes the stream and
  answers with `GET /v1/queue/{id}`, the same result as without a wait, with
  `changed` (`true` or `false`) added. The snapshot is not returned: it
  lacks `last_run_seconds`, `resumable`, `resume_from_run`, and
  `resume_refused_reason`, which an agent needs to choose its next step.
- A change that lands between two calls is not lost: the next call's
  baseline already holds it, and its answer shows it; the call only waits
  longer to return.
- No tool waits for a generation to finish. The agent makes one call and
  gets one JSON result, never a stream; the result's current run, its
  elapsed time, the last run's time, and `cooldown_until` let it choose when
  to look again.

### Resources

Each job file's text, read-only, as `job://{job}`, from `GET /v1/jobs/{job}`'s
`text`, and listed from `GET /v1/jobs`, every page. They come from the API,
never from disk, and `{job}` follows the path rules above.

### When the API is down

Each tool returns an error that says what failed, and the MCP server keeps
running; the next call tries again:

| Code | When | Message names |
|------|------|---------------|
| `server_unreachable` | The HTTP or gRPC connection fails | `--server-url`, and `dtc serve` |
| `unauthorized` | The token file is missing, empty, or unreadable, or the API answers 401, or gRPC `UNAUTHENTICATED` | The token file's path, and `dtc serve` |

Neither carries a stack trace or the token.

## Open questions

For the owner, left open on 2026-09-30 to be revisited before the milestone
is built. The rest of the plan does not wait on them: each answer adds or
removes tools, or changes one number.

### 1. Which newer actions agents get

The first draft predates [Milestone 05](milestone-05-park-and-hold.md) and
[Milestone 06](milestone-06-delete-executions.md), so it offers none of
their actions. Any of these may be chosen, alone or together:

| Option | What it gives an agent | The catch |
|--------|------------------------|-----------|
| Park and unpark | Stopping its chain at the end of the current run, with nothing lost, and withdrawing that before the run ends | A park holds the queue, and only a release ends the hold: without release, the agent's own resume, and every other entry, waits until a person releases it |
| Hold and release | Getting past the hold its own park made, and pausing the queue | It can also release a hold a person made, and a release starts the oldest queued entry at once, which may be a person's |
| Deleting executions | Clearing the history of its own failed tries | Final: the row, its runs, its log, and its manifest go. The TUI asks first; MCP has no confirmation dialog |

Recommended: park, unpark, hold, and release, all four together; not
deleting executions. Every call is audited with the caller `mcp`, so a
release an agent made shows in the audit log.

Once decided:

- The [queue control table](#tools) keeps the chosen tools or goes, and a
  tool for deleting executions (`POST /v1/executions/delete`, with its
  `dry_run`) is added if chosen, destructive by its annotation.
- Park and release get descriptions saying a park holds the queue and a
  release starts the oldest queued entry; the out-of-scope line on deleting
  executions is kept or removed.
- The [phase document](README.md)'s exit criterion for MCP, and
  [Milestone 11](milestone-11-safety-hardening.md)'s lists of MCP actions
  and audited actions, name any tool added, so no two documents disagree.
- The changelog records the choice as an owner decision.

### 2. The cap on `wait_seconds`

A run takes minutes, and `get_queue_entry` returns at the cap when nothing
changed. A higher cap means fewer calls per run, and more risk of reaching
an MCP client's own timeout for one tool call, which would end the call with
the client's error instead of the entry:

| Cap | Calls to watch a 10-minute run | Risk |
|-----|--------------------------------|------|
| 30 seconds | about 20 | Well under the 60-second default request timeout of MCP's TypeScript SDK, which many clients use |
| 120 seconds | about 5 | Times out in a client that keeps a 60-second default |
| 300 seconds | about 2 | Works only in clients with long tool-call timeouts, such as Claude Code |

Recommended: keep 30 seconds, as first drafted. Once decided, the cap goes
into [Waiting for a change](#waiting-for-a-change), the tool's input schema,
and its acceptance criterion, and the changelog records it.

### Checked when built, not for the owner

Context7 was not available while planning, so these are stated by what the
SDK must do, and checked against its documentation (2.x line; 2.2.0 on PyPI
on 2026-09-30, for the 2026-07-28 protocol) when the milestone is built:

- A tool list that changes at runtime, and how the server sends the
  list-changed notification, including between calls; if the high-level
  server cannot, its low-level server gives the list.
- Whether an unknown tool argument is refused by the SDK, or must be by each
  tool.
- The in-memory client the tests use, and the stdio client the process test
  uses.
- Tool annotations (read-only, destructive) and structured results.

## Planned changes

### `mcp_server/` (new)

Imports nothing else from the package (`tests/test_architecture.py`), and
nothing from it imports `mcp_server/` but `cli/app.py`.

- `app.py`: builds the MCP server with the SDK, registers the tools and the
  resources, gives the tool list from the last capabilities read, sends the
  list-changed notification, and runs it on stdio. `run(server_url,
  token_path)` is what `cli/app.py` calls. If the SDK's high-level server
  cannot give a tool list that changes at runtime, its low-level server is
  used for the list.
- `api.py`: the HTTP client: reading and re-reading the token, the caller
  header, the path rules, and turning a transport failure, a 401, or an API
  error into a tool error.
- `watch.py`: `get_queue_entry`'s wait: the gRPC target from
  `GET /v1/health`, the baseline, the compared fields, and the finished
  states.
- `tools.py`: each tool's name, arguments, description, and annotations,
  and the endpoint it calls.
- `generated/`: the gRPC client stubs, generated by `make proto` and
  ignored by git, as `cli/generated/` and `tui/generated/` are.

### `cli/`

- `app.py`: `dtc mcp`, with the three client options. It checks
  `--server-url` with `check_server_host`, resolves the token file's path,
  removes `main()`'s log sinks and adds one on stderr, and imports
  `mcp_server.app` only inside the command.

### `core/`

- `client_config.py`: its docstring says `mcp_server/` holds its client
  "around" these helpers; it is corrected to say `cli/app.py` checks the
  URL for it, and `mcp_server/` reads the token itself. No code changes.

### Repository

- `pyproject.toml`: the `mcp` dependency, on its 2.x line (2.2.0 is current
  on PyPI), with the version chosen when the milestone is built. Ruff's
  `extend-exclude` and pyright's `exclude` gain
  `src/draw_things_control/mcp_server/generated`.
- `Makefile`: `PROTO_OUTS` gains `src/draw_things_control/mcp_server/generated`,
  as its comment already says it will.
- `.gitignore`: `src/draw_things_control/mcp_server/generated/`, beside the
  other three copies.
- `tests/test_architecture.py`: `FRONT_END_EXCEPTIONS` gains
  `("cli", f"{PACKAGE}.mcp_server.app")`, the third import between front
  ends, and its docstring and comment say so.

### Tests

In `tests/mcp_server/`, never starting `draw-things-cli`. The SDK's
in-memory client talks to the MCP server; the MCP server's `httpx` client
sends its requests straight to `create_app` over a fake runner, and its gRPC
client to an in-process `Monitor` service on a loopback port. `tests/` may
import `server/`; `mcp_server/` may not.

- Each acceptance criterion below.
- For every tool that takes a path argument: `.`, `..`, `a/b`, `?x`, `#x`,
  `%2F`, and an empty string never reach another endpoint.
- The wait: it returns on a change of state or run, not on a change of
  step; at once for a finished entry; with `changed: false` when the time
  is up; and the copy of the finished states equals `FINISHED_STATES`.
- A server restarted with the other `--allow-write` setting sends one
  list-changed notification, and the next list has the other set of tools.
- A token rewritten while `dtc mcp` runs is read again after the 401, and
  the call after it succeeds.
- `dtc mcp` started as a process (`python -m draw_things_control mcp`, with
  `--server-url` on a port nothing listens on) and driven over stdio by the
  SDK's client.

### Documentation, when it lands

- The user guide gains an MCP section: starting `dtc mcp`, a sample client
  entry (`uv run --directory <project> dtc mcp`, so it works from any
  directory), the tools, the wait, what the write flag changes and when a
  client sees it, and the two errors of its own.
- `docs/architecture.md`: `mcp_server/`, its own gRPC stubs, and the third
  import between front ends.
- `AGENTS.md` and [the development rules](../development-rules.md#project-layout):
  the third import between front ends, beside `dtc tui` and `dtc serve`.
- The phase document marks the milestone done, and the changelog records
  what was built against this plan.

## Acceptance criteria

- Through the SDK's in-memory client, with the API and the gRPC service in
  process and a fake runner, each tool calls its endpoint and returns its
  body, and an error keeps the API's `code`, `field`, `limit`, `value`, and
  `current_sha256`.
- An agent's whole flow works that way: list inputs, validate a draft,
  create it, submit it, watch it with `get_queue_entry` (with and without
  `wait_seconds`), cancel it, resume it, and list its outputs; and read a
  job, replace it with its `sha256`, and delete it.
- The audit rows of that flow all record the caller `mcp`.
- With writes off on the server, the write tools are absent from the tool
  list; with writes on, they are present; a server restarted with the other
  setting sends a list-changed notification and changes the list, without
  restarting `dtc mcp`. A write tool called on a server with writes off
  returns `writes_off`.
- `get_queue_entry` with `wait_seconds` returns when the entry's state or
  run changes, not when its step does, returns at once for a finished entry,
  and never waits longer than the cap.
- No tool accepts a path, a `draw-things-cli` flag, a credential, or an
  unknown argument, and no argument, dots and slashes included, reaches an
  endpoint other than its tool's.
- With the API unreachable, the token file missing, or the token wrong,
  tools return `server_unreachable` or `unauthorized` naming `dtc serve`,
  and the MCP server keeps running.
- A non-loopback `--server-url` is refused without `--allow-remote-server`.
- `dtc mcp`, started as a process with no API running and driven over stdio
  by the SDK's client, connects, lists its tools, and answers a call with
  `server_unreachable`; its stdout holds protocol messages only.
- The token never appears in a tool result, log line, or error.
- `make check` passes; the user guide documents `dtc mcp`.
