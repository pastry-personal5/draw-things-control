# Milestone 10: MCP server

**Phase:** [Phase 3: API server and MCP server for AI](README.md)
**Status:** done (2026-10-03; built in increments A to E, all reviewed, though not each before the next started; see [As built](#as-built))
**Depends on:** [Milestone 02: HTTP API](milestone-02-http-api.md), [Milestone 07: Job file management](milestone-07-job-file-management.md)
(its write endpoints, `sha256`, and the `writes_off` answer), the queue
actions of [Milestone 05](milestone-05-park-and-hold.md), and deleting
executions, from [Milestone 06](milestone-06-delete-executions.md) (all built
before this one)

## Goal

Give AI agents typed tools and resources for the API, so an agent can do the
whole draft, create, queue, watch, cancel, and resume workflow of a long
chain without knowing HTTP.

## Scope

In scope:

- `dtc mcp`, an MCP server over stdio, with the three client options
  `dtc queue` and `dtc tui` take
- Tools that map one to one to API endpoints, with the API's names and
  bounds: reading and running jobs, parking entries and holding the queue,
  and, while writes are on, writing job files and deleting executions
- Write tools listed only while the API server has writes on, and a
  list-changed notification when that changes, for clients of either
  protocol era
- A wait in `get_queue_entry` that ends at the next change an agent acts on,
  of up to 7200 seconds, so watching a chain takes about a call a run
- An SSE watch of one queue entry, added to the API, which `dtc mcp` waits
  on instead of gRPC
- A brief view of an execution and a read of one of its runs, and a brief
  view of a job, added to the API, so answers fit what an agent can read
  at a small cost in tokens
- Keeping agents off the queue entries and holds people made: the queue
  records who submitted each entry and who made the hold, and the API
  refuses an agent's cancel, park, unpark, or resume of a person's entry,
  and its release of a person's hold
- Job files as read-only resources
- A checked-in `.mcp.json` that registers `dtc mcp` with Claude Code
- Clear errors when the API server is not running or refuses the token

Out of scope:

- Any generation, queue, or file logic of its own. It only calls the API.
- Direct access to the GPU, the database, the global configuration, or job
  files
- Transports other than stdio, or network exposure
- Streaming events to the agent (`WatchEvents`), or notifications of queue
  changes: an agent asks, with `get_queue_entry`'s wait
- Pushing events into a Claude Code session through its channels (owner
  decision): a research preview behind
  `--dangerously-load-development-channels`, on the older handshake only
  ([research](../../research/mcp-tokens-and-events.md#claude-codes-channels))
- Other SSE (owner decision): the whole event stream (`GET /v1/events`), and
  MCP over HTTP from `dtc serve`. gRPC stays the TUI's and `dtc queue add
  --wait`'s way to watch.
- Dropping null fields from results (owner decision): a tool returns the
  API's JSON as it is
- A summary of the run just ended in `GET /v1/queue/{id}` (owner decision):
  an agent that wants a run's checks calls `get_execution_run`
- Keeping agents off executions people ran (owner decision): an agent with
  writes on may delete any execution the API lets it
- Retiring gRPC for the TUI and `dtc queue add --wait`: the owner decided to
  keep gRPC alongside the API's SSE watch
- Reshaping the API's answers: a tool returns the API's JSON as it is. Where
  an answer is too big for an agent, the API gains a smaller view that every
  client can use, as for [executions](#executions-in-brief).
- The audit log (`GET /v1/audit`) as a tool: it is the owner's record of
  what agents did, not something an agent needs to do its work
- Deleting the whole history: the API takes 1 to 200 execution IDs, and
  `all` belongs to `dtc history delete` and the TUI
- Asking the person to confirm a deletion through MCP's elicitation: clients
  differ in what they support, and the 2026-07-28 protocol replaces the
  pushed form with a retry the client must drive. The gate is
  `--allow-write`, and the tool's required `dry_run`.
- Prompts, sampling, and completions

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
  terminal. Nothing is written before the transport starts, and nothing slow
  runs before it: Claude Code gives a server 30 seconds to start
  (`MCP_TIMEOUT`).
- Because it is a client, several MCP sessions can share one server, and the
  server remains the only process that owns the GPU (owner decision).

### Registering it with Claude Code

- `.mcp.json` at the project root is checked in (owner decision):

  ```json
  {
    "mcpServers": {
      "dtc": {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "dtc", "mcp"]
      }
    }
  }
  ```

- `uv run` finds the project from any directory below it, and `dtc mcp`
  finds the token from the package's own place, so the entry names no path.
  It passes no `--allow-remote-server` and no token. An interactive Claude
  Code session asks once before it uses a server a project's `.mcp.json`
  names; `claude -p` runs, Agent SDK sessions, and cloud sessions load it
  without asking, and `disabledMcpjsonServers` keeps it out of any session.
- So every Claude Code session in this repository, development sessions
  included, is offered the tools, and acts on the owner's real queue and
  history whenever `dtc serve` runs. `AGENTS.md` says to use them only when
  the owner asks. The write tools, deleting executions among them, stay
  unlisted unless the owner started `dtc serve --allow-write`.
- Hiding a tool is not a security boundary. A session that can run a shell
  can read `config/server-token` and call any endpoint itself,
  `POST /v1/executions/delete` included, with or without `--allow-write`.
  The tool list keeps an agent that follows its tools from destructive
  actions the owner did not turn on; the token, the loopback bind, and the
  audit log are what guard the API
  ([Milestone 11](milestone-11-safety-hardening.md)).
- A test reads the file and checks the entry: `uv run dtc mcp`, with no
  other argument.
- The user guide gives the same entry for other clients, as `uv run
  --directory <project> dtc mcp`, since they may start it from anywhere.

### Talking to the server

- HTTP goes through `httpx`'s async client, every request with the bearer
  token and `X-Dtc-Caller: mcp`, a caller `server/caller.py` already knows,
  so the audit log records `mcp` for every action an agent takes.
- `get_queue_entry`'s wait reads the API's [SSE watch](#watching-one-entry-over-sse)
  with the same client, so `dtc mcp` speaks HTTP alone: no gRPC, no
  generated stubs, and no `grpc` import in `mcp_server/` (owner decision).
  It reads the stream itself (`event:` and `data:` lines, a blank line
  ending each message), with no SSE client library.
- `mcp_server/` imports nothing from the package, so it keeps its own copy of
  reading the token (`core/client_config.py`'s `read_client_token`: the
  file's ASCII text, stripped, refused when missing or empty). A test checks
  the copy against the original, as for the finished states below.
- An argument that becomes part of a URL path (a job reference, a job name,
  a queue or execution ID, a run number) is refused when it is empty or made
  only of dots, and percent-encoded otherwise (`quote(value, safe="")`), so
  no argument can change which endpoint is called. Encoding alone is not
  enough: `quote` leaves `.` and `..` as they are and `httpx` removes dot
  segments, so `..` would reach another route, and an empty argument would
  reach `/v1/jobs/`, a slash redirect `httpx` does not follow. The refusal is
  `invalid_input` naming the argument, in the API's error shape, and no
  request is made. Query values go through `httpx`'s `params`, never into
  the URL's text.
- Beyond that, the MCP server does not check IDs and names itself: the API
  accepts `q7` as well as `Q0007`, for one, and a stricter copy of its
  grammar would refuse what the API takes. The API validates every argument
  and answers `not_found` or `invalid_input`.
- A list tool sends `limit` 50 when the agent gives none, not the API's
  default of 200. An execution's row is about 360 bytes, so a page of 200 is
  about 70 KB, near the 25,000 tokens past which Claude Code saves an MCP
  result to a file and hands the agent its path to read in parts. The API's
  own bounds stand: at least 1, and at most 200 returned.

### Tools

Read and run (always listed):

| Tool | API | Arguments |
|------|-----|-----------|
| `get_capabilities` | `GET /v1/capabilities` | none |
| `list_jobs` | `GET /v1/jobs` | `limit`, `cursor` |
| `get_job` | `GET /v1/jobs/{job}?brief=1`, or without `brief` when `brief` is false | `job`, `brief` (optional, default true) |
| `preview_job` | `GET /v1/jobs/{job}/preview` | `job` |
| `validate_job_text` | `POST /v1/validate` | `yaml`, `name` (optional) |
| `list_inputs` | `GET /v1/inputs` | `limit`, `cursor` |
| `submit_job` | `POST /v1/queue` | `job` |
| `get_queue` | `GET /v1/queue` | `state`, `limit`, `cursor` |
| `get_queue_entry` | `GET /v1/queue/{id}`, and `GET /v1/queue/{id}/watch` with `wait_seconds` | `queue_id`, `wait_seconds` (optional) |
| `cancel_queue_entry` | `POST /v1/queue/{id}/cancel` | `queue_id` |
| `resume_queue_entry` | `POST /v1/queue/{id}/resume` | `queue_id` |
| `list_executions` | `GET /v1/executions` | `status`, `job`, `name`, `limit`, `cursor` |
| `get_execution` | `GET /v1/executions/{id}?brief=1` | `execution_id` |
| `get_execution_run` | `GET /v1/executions/{id}/runs/{run}` | `execution_id`, `run` |
| `list_outputs` | `GET /v1/executions/{id}/outputs` | `execution_id` |

Queue control (always listed; owner decision):

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
| `delete_executions` | `POST /v1/executions/delete` | `executions`, `dry_run` |

- Tool arguments have the API's names and bounds. No argument is a path, a
  `draw-things-cli` flag, or a credential; paths inside job text are
  confined by [Milestone 07](milestone-07-job-file-management.md#validation-before-writing).
  `yaml` is sent as `{"yaml": ...}`, the body
  [Milestone 07](milestone-07-job-file-management.md#endpoints) takes, and
  `expected_sha256` goes in `replace_job`'s body and `delete_job`'s query.
  `executions` is a list of 1 to 200 execution IDs.
- `delete_executions` is behind `--allow-write` in MCP alone (owner
  decision). The endpoint stays registered always, as
  [Milestone 06](milestone-06-delete-executions.md) built it, for
  `dtc history delete` and the TUI. The MCP server reads the capabilities
  afresh before each call to this tool, and refuses it with `writes_off`
  unless writes are on, with a message of its own (deleting executions
  through MCP requires `dtc serve --allow-write`), not the API's, which names
  `data/jobs/`; no request reaches the endpoint. Its `dry_run` is required, where the API's defaults to false, so
  an agent always says which it means.
- The MCP server checks each call against the input schema it declares
  (`additionalProperties: false`, the required arguments, their types, and
  the bounds the API refuses rather than clamps: `limit` and `run` at least
  1, `wait_seconds` 1 to 7200, `executions` 1 to 200 strings). An unknown,
  missing, or wrong argument is `invalid_input` naming it, and no request is
  made. If the SDK checks the schema first, its own refusal stands, as long
  as it names the argument and makes no request.
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
  - parking ends the entry after its current run with nothing lost, and
    holds the queue: nothing else starts, the entry's own resume included,
    until `release_queue`. `unpark_queue_entry` withdraws a park before the
    run ends, and ends the hold if the park made it;
  - `release_queue` starts the oldest queued entry at once, which may be a
    person's; `hold_queue` lets the running job finish and starts nothing
    after it;
  - an agent may cancel, park, unpark, and resume only entries submitted
    through MCP (`submitted_by`), and release only a hold an agent made
    (`hold_caller`); the rest answer `not_permitted`;
  - a stopped chain continues with `resume_queue_entry`, which reruns the
    run that was cut short (`get_queue_entry` says from which run);
  - `get_job` is brief unless asked: it leaves out a valid job's YAML text,
    and `brief: false` returns it, with the `sha256` an edit needs;
  - `get_execution` is brief: each run's command and each check's details
    come from `get_execution_run`, one run at a time;
  - an output marked incomplete is a leftover, not a result;
  - `delete_executions` is final: each execution's row, runs, log, and
    manifest go, and its outputs stay. Call it with `dry_run: true` first:
    the answer says what would be deleted, what is refused (a running
    execution, or one a queued or running entry uses), and which parked or
    failed entries could no longer be resumed;
  - `wait_seconds` waits for the next change an agent acts on, usually a
    run's end, so one call with a long wait follows a run; a client that
    ends a tool call after 60 seconds, as many do, needs 50 or less.
- Each tool carries MCP's annotations, which inform a client and enforce
  nothing: read-only for the reads, `validate_job_text`, and
  `get_execution_run`; destructive for `cancel_queue_entry`, which loses the
  run in progress, for `replace_job` and `delete_job`, which change a file a
  person may be using, recoverably, and for `delete_executions`, which
  cannot be undone; idempotent for `hold_queue` and `release_queue`, whose
  second call answers `changed: false`.
- A result is the API's JSON body as it is, with no projection: the API
  stays the one contract, and a field it gains reaches agents with no change
  here. It is the tool's structured content, and the same JSON, compact, is
  the result's one text block, for clients that read only the text. Claude
  Code passes the structured content and drops a text block that repeats
  it, so the two forms cost its agent one copy (documented for the Agent
  SDK's tools; checked for a stdio server when built).
  No output schema is declared: the SDK's client raises on a result that
  does not match one, and the API, not a copy of its shapes here, is the
  contract. Long lists are paged by the API's own `limit` and `cursor`,
  passed through.
- An error is a tool error (`is_error`) whose body, in the same two forms,
  is the API's error shape as it is (`code`, `message`, and, when the API
  sends them, `field`, `limit`, `value`, and `current_sha256`). The MCP
  server makes some of its own, for failures that never reached a route:
  `server_unreachable` and `unauthorized` (below), `invalid_input` for an
  argument it refuses, and `writes_off` for `delete_executions` while writes
  are off.

### The tool list

- The write tools do not exist unless the server has writes on, as the
  endpoints do not (phase exit criterion). The MCP server reads
  `GET /v1/capabilities` when the client asks for the tool list, and again
  before a tool call when its last read is more than 5 seconds old or
  failed, and always before `delete_executions`.
- When `allow_write` differs from the list it last gave (a server restarted
  with or without `--allow-write`, or an API that was down when the list was
  read and is up now), it tells the client the tool list changed, in the way
  the session's protocol era has, and lists the new set when asked:
  - A session on 2025-11-25 or earlier, opened with the `initialize`
    handshake: the server declares `tools.listChanged`, and sends
    `notifications/tools/list_changed` on the session
    (`send_tool_list_changed`).
  - A session on 2026-07-28: change notifications reach a client only on a
    `subscriptions/listen` stream it opened. The server serves that method
    (the SDK's `ListenHandler` over an `InMemorySubscriptionBus`), which is
    what makes it declare `listChanged` to such a client, and publishes
    `ToolsListChanged` on the bus. Its `tools/list` answers also carry a
    cache hint (the low-level server's `cache_hints`) of at most 5 seconds,
    so a client that does not listen keeps a stale list no longer than the
    MCP server would.
  - Both are sent on every change; each reaches only its own era's
    sessions.
- With the API down, only the read-and-run and queue control tools are
  listed.
- A client that does not act on the notification keeps its old list. A job
  file write tool it calls after writes were turned off gets the API's 405
  `writes_off`, naming `--allow-write`
  ([Milestone 07](milestone-07-job-file-management.md#enabling-writes)), and
  `delete_executions` the MCP server's own; write tools turned on appear
  only when it reads the list again. The user guide says so.

### Waiting for a change

`get_queue_entry` takes an optional `wait_seconds`, from 1 to 7200, and
waits for the next change an agent acts on (owner decision). Every call is a
turn that reads the agent's whole conversation again, so the number of calls
is what costs, more than their size
([research](../../research/mcp-tokens-and-events.md#turns-per-job)). The
owner's chains take about 75 minutes a run, cooldown included (E0018: 6
runs in 7.5 hours). A 600-second cap would take about 45 calls for 6 runs
and 225 for 30; a wait that ends at the next change takes about one a run.
7200 seconds is above the longest gap between two such changes, a
3600-second run and an 1800-second cooldown.

Claude Code puts no per-request timer on a stdio server, moves a call that
runs past 2 minutes to the background and tells the agent when it ends, and
ends a stdio call after 30 minutes with no answer and no progress, which the
progress notifications below prevent. A client on the TypeScript SDK's
default ends any request at 60 seconds; the tool's description tells an
agent in such a client to pass 50 or less.

With it:

1. It reads `GET /v1/queue/{id}` first. An unknown entry is the API's
   `not_found`. An entry already finished (`succeeded`, `failed`,
   `cancelled`, `interrupted`, or `parked`) is answered at once, with
   `changed: false`: nothing more will change. `mcp_server/` keeps its own
   copy of these states, since it imports nothing from the package, and a
   test checks it against `state/queue.py`'s `FINISHED_STATES`.
2. Otherwise it opens `GET /v1/queue/{id}/watch`. The API always sends the
   entry's current snapshot first, so the first message is the baseline, not
   a change. The tool waits for a later message that differs from it in a
   field an agent acts on: `state`, `execution_id`, `error`,
   `park_requested`, `queue_held`, `cooldown_until`, or `current_run`
   becoming null, the end of a run. `current_run` taking the next run's
   number does not count: the worker clears it when a run finishes and sets
   it again when the next one starts, after the cooldown, so counting both
   would wake the agent twice a run. `current_step` and
   `current_step_total` are ignored: they change with every diffusion step,
   and would end every wait within seconds while a run is going.
3. While it waits, it sends a progress notification every 15 seconds when
   the client gave a progress token (`ctx.session.report_progress`, which
   does nothing otherwise), so a client that restarts its timer on progress
   does not end the call. A call that came without a progress token waits at
   most 1500 seconds, whatever `wait_seconds` says: with nothing to reset
   it, Claude Code's 30-minute idle limit would end a longer one.
4. When such a message arrives, or the time is up, it closes the stream and
   answers with `GET /v1/queue/{id}`, the same result as without a wait, with
   `changed` (`true` or `false`) added. The snapshot is not returned: it
   lacks `last_run_seconds`, `resumable`, `resume_from_run`, and
   `resume_refused_reason`, which an agent needs to choose its next step.
5. A client that cancels the call, or times it out (the TypeScript SDK
   cancels a request it times out), ends the wait: the stream is closed and
   nothing is answered.
6. A watch that cannot open, or ends early, ends the wait as a tool error:
   the API's own answer (`not_found`, or a 401 as `unauthorized`), or
   `server_unreachable` for a connection refused or dropped (the server
   stopped). A watch that sends nothing, not even its keep-alive, for 60
   seconds counts as dropped.

- The session answers its other tool calls while a wait runs.
- A change that lands between two calls is not lost: the next call's
  baseline already holds it, and its answer shows it; the call only waits
  longer to return.
- A call can now wait as long as a run, which supersedes Milestone 02's
  rule that no call waits for a generation; none waits for a whole job. The
  agent makes one call and gets one JSON result, never a stream.

### Executions in brief

`GET /v1/executions/{id}` answers 76 KB for E0018, a 6-run chain, about
20,000 tokens. Of each run's 12.6 KB, its command is 2.5 KB and its checks'
`facts` about 8 KB. A 30-run chain would be about 380 KB, four times the
25,000 tokens past which Claude Code saves an MCP result to a file and
hands the agent its path, which the agent then reads in parts, a turn
each. So the API gains, for every client (owner decision):

- `GET /v1/executions/{id}?brief=1`: the same answer, but each run without
  its `command`, and each check, a run's or the execution's own, as its
  `stage` and `verdict` alone (and `run`, for the execution's own). About
  0.6 KB a run, so 30 runs are about 5,000 tokens. Another value of `brief`
  is 422 `invalid_input`, field `brief`, as `overwrite` is.
- `GET /v1/executions/{id}/runs/{run}`: one run, as the full answer gives it,
  its command (redacted, as everywhere) and its checks in full. `run` is the
  run's number in the chain, so a resumed execution's first is not 1; a run
  the execution does not have is 404 `not_found`, and a `run` that is not a
  positive integer is 422 `invalid_input`.
- Both are behind the token and, like every read, not audited. `dtc history`
  and the TUI read as they do now.

### Jobs in brief

`GET /v1/jobs/{job}` for `duo-blend-i8x` is 4.6 KB: its YAML text is
2.5 KB, and the resolved job, whose prompt pairs repeat the text, the rest.
An agent reads a job far more often than it edits one, so the API gains
(owner decision):

- `GET /v1/jobs/{job}?brief=1`: a valid job's answer without `text`, 2.1 KB
  for that job; `sha256` stays. An invalid job's answer keeps its `text`,
  since that is what the agent must fix. Another value of `brief` is 422
  `invalid_input`, field `brief`.
- `get_job` asks for it unless the agent passes `brief: false`, which it
  does to edit a job. The `job://` resources serve the text alone.

### Watching one entry over SSE

`GET /v1/queue/{id}/watch` (owner decision) is `WatchQueueEntry` over HTTP,
for `dtc mcp`, which then needs neither gRPC nor stubs of its own. It
supersedes, for MCP only, the 2026-09-25 decision that gRPC is the one way
to watch for change; the TUI and `dtc queue add --wait` keep gRPC, and
`WatchEvents` has no SSE counterpart.

- A `text/event-stream` response, with FastAPI's own `EventSourceResponse`
  (no new dependency). Each message is `event: snapshot`, its `data` the
  snapshot as JSON: the fields of `monitor.proto`'s `QueueEntrySnapshot`,
  with the same names, an unset one as null.
- The same rule as `WatchQueueEntry`: the current snapshot first, then one
  whenever a field the contract names changes, the elapsed seconds aside.
  Both read one snapshot function in `server/`, so the two cannot drift.
- A comment line (`: keep-alive`) every 15 seconds while nothing changes,
  so a client can tell a quiet watch from a dead connection.
- No event IDs and no `Last-Event-ID`: a client that reconnects gets a new
  baseline, which is all a watch of one entry needs.
- The stream ends when the client disconnects, when the entry is gone, or
  when the server shuts down: uvicorn waits for open connections when it
  stops, so every watch ends itself on shutdown, before the worker stops,
  as gRPC's streams are cancelled.
- An unknown entry is 404 `not_found` before the stream starts. Behind the
  token and the `Host` check like every route but `/v1/health`, and, like
  every read, not audited. A browser's `EventSource` cannot send the token
  in a header, and the token is never taken in the query string, so the
  watch serves clients that send headers.

### People's entries and holds

The queue is shared, and the token gives every caller every action. An agent
should not cancel, park, or resume what a person started, or end a person's
hold (owner decision):

- Each queue entry records its submitter, `submitted_by`: the caller of the
  `POST /v1/queue` or the resume that made it (`cli`, `tui`, `mcp`, or
  `api`, from `X-Dtc-Caller`). An entry made before this milestone has none,
  and counts as a person's. A resume makes a new entry, whose submitter is
  whoever resumed it.
- The hold records the caller that made it, beside `held_by`, the entry
  whose park made it. A park's hold is the parker's. A person's direct hold
  on a queue an agent's park holds makes the hold the person's, as a direct
  hold already makes it its own; an agent's direct hold on a queue a person
  holds changes nothing, so an agent never comes to own a person's hold.
- The API refuses a request whose caller is `mcp` to cancel, park, unpark,
  or resume an entry whose submitter is not `mcp`, or to release a hold
  whose caller is not `mcp`. The refusal is 403 `not_permitted`, a new code,
  naming the entry or the hold and who made it, and is audited like any
  refusal. A person's own commands are not limited.
- The check sits in the API, not in `mcp_server/`: there it is one step with
  the action, under the same lock, so a hold cannot change hands between
  the check and the release, and the refusal reaches the audit log. The MCP
  server keeps no queue logic of its own.
- `GET /v1/queue`, `GET /v1/queue/{id}`, and the queue entry the actions
  answer gain `submitted_by`; the hold fields gain `hold_caller`.
- All MCP sessions share the caller `mcp`, so this keeps agents off people's
  entries, not one agent off another's. `X-Dtc-Caller` names itself, so
  this guards agents that use their tools, not the API: a client that sends
  another caller, or none, is not limited. Deleting executions is not
  covered (owner decision).

### Resources

Each job file's text, read-only, as `job://{job}`, from `GET /v1/jobs/{job}`'s
`text`, and listed from `GET /v1/jobs`, every page. They come from the API,
never from disk, and `{job}` follows the path rules above. A listing carries
the same short cache hint as the tool list, since a job file can appear at
any time and no notification says so.

### When the API is down

Each tool returns an error that says what failed, and the MCP server keeps
running; the next call tries again:

| Code | When | Message names |
|------|------|---------------|
| `server_unreachable` | The connection fails, or a watch drops | `--server-url`, and `dtc serve` |
| `unauthorized` | The token file is missing, empty, or unreadable, or the API answers 401 | The token file's path, and `dtc serve` |

Neither carries a stack trace or the token.

## Order of work

Built in increments, each ending with `make check` and a code review before
the next starts (owner decision):

- **A. The API.** The brief views of an execution and a job, the run
  endpoint, the SSE watch and its shared snapshot, the submitter and the
  hold's caller (schema 9) with the `not_permitted` refusals, and the
  `--allow-write` help and log line. Usable by any client before
  `dtc mcp` exists.
- **B. `dtc mcp` with the read, run, and queue control tools.** The command,
  stdio, the HTTP client, the token, the path rules, the argument checks,
  the errors, results in both forms, the instructions, and the resources.
- **C. The write tools.** The job file tools, `delete_executions` behind
  the fresh capabilities read, and the tool list's changes in both protocol
  eras.
- **D. The wait.** `get_queue_entry` over the SSE watch: the compared
  fields, progress, the 1500-second limit without a token, cancellation,
  and drops.
- **E. Registration and documents.** `.mcp.json`, the process test over
  stdio, the user guide, the architecture, `AGENTS.md`, and the development
  rules.

## As built

Built as planned, but where the [changelog](phase-3-changelog.md) of 2026-10-02 and 2026-10-03 says otherwise:

- A path argument holding a slash is refused too: an ASGI server decodes `%2F` before it routes, so percent-encoding
  alone let `Q0001/watch` reach the SSE watch.
- A request sent but not answered within 30 seconds is `server_timeout`, a fourth error of the MCP server's own: it
  may have taken effect, so it is not `server_unreachable`.
- One rule for a hold on a held queue: a person's hold, direct or a park's, makes an agent's hold the person's, and an
  agent's direct hold over a person's park detaches the hold from the park and leaves it the person's, where the plan
  said it changes nothing. An agent's unpark is refused while the hold its entry's park made is a person's.
- The SSE watch's keep-alive is its own `: keep-alive` comment, and `dtc serve` ends open watches as uvicorn begins to
  shut down; FastAPI's generator routes could not answer 404 before the stream.
- An optional argument given as null is left out, and an integer may come as `50.0`.
- The reviews: A's ran before B began. B's first review was cut short by a usage limit, so B and C were reviewed
  together, and D and E, whose code and documents were written while that review ran, together after.
- Checked when built: a 2026-07-28 client over stdio, as an older one, sends the progress token the wait reads.
  Not checked: that Claude Code drops a text block repeating a stdio server's structured content, which its
  documentation says of the Agent SDK's tools.

## Follow-ups

The owner decided on 2026-10-04 to keep gRPC for the TUI and `dtc queue
add --wait` alongside the API's SSE watch used by MCP. There is no transport
migration or gRPC retirement follow-up.

## Planned changes

### `mcp_server/` (new)

Imports nothing else from the package (`tests/test_architecture.py`), and
nothing from it imports `mcp_server/` but `cli/app.py`.

- `app.py`: builds the SDK's low-level `Server`, with `on_list_tools`,
  `on_call_tool`, `on_list_resources`, `on_list_resource_templates`,
  `on_read_resource`, and `on_subscriptions_listen`, a short `instructions`
  text, and the `cache_hints`; sends the list-changed notifications; and
  runs it on stdio. `build_server(server_url, token_path, *,
  http_transport=None)` is what the tests call, as the TUI takes
  `http_transport`, and `run(server_url, token_path)` is what `cli/app.py`
  calls. The low-level server, not
  `MCPServer`, because the tools need input schemas written by hand with the
  API's names, a list that changes at runtime, and error results that carry
  the API's error shape as structured content: `MCPServer` derives a schema
  from a function's signature, and its `ToolError` gives the model text
  alone.
- The `instructions` name the workflow in a few lines: `get_capabilities`
  and `list_inputs`, then `validate_job_text`, `create_job`, `submit_job`,
  `get_queue_entry` with a long `wait_seconds`, and `list_outputs`; and that
  people share the queue. Claude Code keeps the instructions and the tool
  names in context on every turn and loads a tool's schema only when it is
  used, so the instructions stay short and each description holds only what
  the agent must know to succeed.
- `api.py`: the HTTP client: reading and re-reading the token, the caller
  header, the path rules, and turning a transport failure, a 401, or an API
  error into a tool error.
- `watch.py`: `get_queue_entry`'s wait: the read first, the SSE watch and
  its lines, the 60-second silence that counts as a drop (the stream's own
  `httpx` read timeout, 60 seconds, not the 30 of a plain request: a
  shorter one than the keep-alive would drop every quiet watch), the
  1500-second limit without a progress token, the baseline, the
  compared fields, the finished states, the progress notifications, and the
  cancellation.
- `tools.py`: each tool's name, arguments, input schema, description, and
  annotations, the endpoint it calls, and the check of a call's arguments
  against its schema.
- No `generated/`: `mcp_server/` has no gRPC client.

### `server/`

- `routes_executions.py`: `GET /v1/executions/{id}` takes `?brief=1`, and
  `GET /v1/executions/{id}/runs/{run}` is new.
- `serializers.py`: `execution_detail` takes `brief`, and one run's answer
  comes from `run_summary`, as the full answer's runs do.
- `routes_jobs.py`: `GET /v1/jobs/{job}` takes `?brief=1`.
- `routes_queue.py`: `GET /v1/queue/{id}/watch`, the SSE watch; submit,
  resume, cancel, park, unpark, hold, and release pass the caller on.
- `serializers.py`: `queue_entry` gains `submitted_by`, and `queue_hold`
  gains `hold_caller`.
- `errors.py`: `not_permitted` maps to 403 in `STATUS_BY_ERROR_CODE`.
- A module of its own for an entry's snapshot, which `grpc_service.py`'s
  `WatchQueueEntry` and the SSE watch both read: the fields, and the rule for
  when one is sent.
- `serve.py`: the line logged at start with `--allow-write` says writes to
  `data/jobs/`, and deleting executions through MCP, are on. Shutdown ends
  every open SSE watch before it stops uvicorn and the worker, as it cancels
  gRPC's streams.
- `proto/monitor.proto`: its comments stop naming `mcp_server/` as a client
  of `WatchQueueEntry`.

### `cli/`

- `app.py`: `dtc mcp`, with the three client options. It checks
  `--server-url` with `check_server_host`, resolves the token file's path,
  removes `main()`'s log sinks and adds one on stderr, and imports
  `mcp_server.app` only inside the command.
- `app.py`: `serve --allow-write`'s help says it also lets agents delete
  executions through MCP, beside creating, replacing, and trashing job
  files.

### `state/` and `services/`

- `schema.py`: schema 9, a forward migration: `queue.submitted_by`, null for
  the entries already there.
- `queue.py`: the column on `QueueRow`, written by the insert.
- `queue_submit.py` and `queue_resume.py`: take the caller and store it.
- `queue_hold.py`: the hold's saved setting and `HoldState` gain `caller`;
  `hold` takes it, and keeps a person's hold when an agent holds directly.
- `queue_cancel.py`, `queue_park.py`, `queue_resume.py`, and the release:
  given the request's caller, refuse `mcp` on a person's entry or hold, in
  the same step as the action.

### `core/`

- `errors.py`: `NotPermittedError(DtcError)`, code `not_permitted`.
- `exit_codes.py`: `not_permitted` maps to `EXIT_INVALID_INPUT`, beside
  `conflict`.
- `client_config.py`: its docstring says `mcp_server/` holds its client
  "around" these helpers; it is corrected to say `cli/app.py` checks the
  URL for it, and `mcp_server/` reads the token itself. No code changes.

### Repository

- `pyproject.toml`: `mcp>=2.2.0`, the 2.x line, which speaks the 2026-07-28
  protocol and the handshake-era ones before it. It brings `httpx2`,
  `mcp-types`, `jsonschema`, `pyjwt[crypto]`, `opentelemetry-api`, and
  `sse-starlette`, beside what FastAPI already brings.
- `Makefile`: `PROTO_OUTS` is unchanged, and its comment, which says
  `mcp_server/generated` joins it in Milestone 10, says `mcp_server/`
  watches over SSE and has no stubs.
- `.gitignore`: the comment above the three copies of the stubs stops
  naming `mcp_server/` (it says Milestone 08).
- `.mcp.json` (new): the entry [above](#registering-it-with-claude-code).
- `tests/test_architecture.py`: `FRONT_END_EXCEPTIONS` gains
  `("cli", f"{PACKAGE}.mcp_server.app")`, the third import between front
  ends, and its docstring and comment say so. A test checks that
  `mcp_server/` imports no `grpc`. If `mcp_server/` imports `mcp_types`
  itself, `FRAMEWORKS` gains it.

### Tests

In `tests/mcp_server/`, with `unittest`'s `IsolatedAsyncioTestCase` (not
pytest, which the SDK's examples use), never starting `draw-things-cli`.
The SDK's in-memory `Client(server)` talks to the MCP server, in its default
mode (2026-07-28), and with `mode="legacy"` (2025-11-25) for the tool list
and its notification. The MCP server's `httpx` client reaches `create_app`
through `httpx.ASGITransport`, over a fake runner, with a base URL that
matches the context's bound host and port, which the `Host` check otherwise
answers 400. `ASGITransport` may collect a whole response before it returns
one, which an endless watch never finishes: that is checked first, and if
so, the wait's tests run the app under uvicorn on a loopback port. `tests/`
may import `server/`; `mcp_server/` may not.

- Each acceptance criterion below.
- For every tool that takes a path argument: `.`, `..`, `a/b`, `?x`, `#x`,
  `%2F`, and an empty string never reach another endpoint.
- The wait: it returns on a change of state or at a run's end, not at the
  next run's start or on a change of step; at once for a finished entry;
  within 1500 seconds without a progress token; with `changed: false` when the time
  is up; with a progress notification every 15 seconds when given a token;
  as `server_unreachable` when the watch drops or falls silent for 60
  seconds; a cancelled call closes its stream; another call is answered
  while it waits; and the copy of the finished states equals
  `FINISHED_STATES`.
- The copy of the token read gives what the original gives, for a missing,
  an empty, and a padded token file.
- A server restarted with the other `--allow-write` setting sends one
  list-changed notification in each era (a listen stream's event, and the
  session's notification), and the next list has the other set of tools.
- `delete_executions` is absent, and refused with `writes_off` without a
  request to the endpoint, while writes are off; without `dry_run` it is
  refused.
- A token rewritten while `dtc mcp` runs is read again after the 401, and
  the call after it succeeds.
- `dtc mcp` started as a process (`python -m draw_things_control mcp`, with
  `--server-url` on a port nothing listens on) and driven over stdio by the
  SDK's client.
- `.mcp.json` names `uv run dtc mcp` and nothing else.

In `tests/server/`:

- An execution's `?brief=1` drops each run's command and shrinks each
  check to its stage and verdict; another value of `brief` is 422; one run
  comes back in full; a run the execution does not have is 404, and one that
  is not a positive integer 422.
- A job's `?brief=1` drops a valid job's text and keeps an invalid one's,
  and `sha256` either way.
- With the caller `mcp`, cancel, park, unpark, and resume of an entry a
  person submitted, or one from before the migration, are 403
  `not_permitted` and change nothing, and so is a release of a person's
  hold; on an agent's own entry and hold they work. A person's commands on
  an agent's entry work. An agent's direct hold over a person's leaves it
  the person's. Each refusal has its audit row. Schema 9 migrates a store
  at schema 8 with queue entries in it.
- The SSE watch sends the current snapshot first, then one on a change and
  none on the elapsed seconds alone, the same snapshots `WatchQueueEntry`
  sends for the same changes; a keep-alive while quiet; 404 for an unknown
  entry, 401 without the token; and it ends on shutdown.

### Documentation, when it lands

- The user guide gains an MCP section: starting `dtc mcp`, the checked-in
  `.mcp.json` and the entry for other clients, the tools, the wait and the
  client timeouts that bound it (Claude Code moves a long wait to the
  background only in the main conversation of an interactive session: in a
  subagent, or a `claude -p` run without `CLAUDE_AUTO_BACKGROUND_TASKS=1`, a
  long wait blocks for as long as it waits), what the write flag changes and when a
  client sees it, the errors of its own, and that hiding a tool is not a
  security boundary. Its server section gains the two `?brief=1` views, the
  run endpoint, and the SSE watch, and its text on `--allow-write` says the
  flag also lets agents delete executions through MCP.
- `docs/architecture.md`: `mcp_server/`, which speaks HTTP alone, the third
  import between front ends, the brief views, and the SSE watch beside
  gRPC's.
- `AGENTS.md` and [the development rules](../../development-rules.md#project-layout):
  the imports between front ends, `dtc tui`, `dtc serve` (which the rules do
  not name today), and `dtc mcp`. `AGENTS.md` also says the `.mcp.json`
  tools act on the owner's real queue and history, to be used only when the
  owner asks.
- The phase document marks the milestone done, and the changelog records
  what was built against this plan.

## Acceptance criteria

- Through the SDK's in-memory client, with the API in process and a fake
  runner, each tool calls its endpoint and returns its
  body, as structured content and as text, and an error keeps the API's
  `code`, `field`, `limit`, `value`, and `current_sha256`.
- An agent's whole flow works that way: list inputs, validate a draft,
  create it, submit it, watch it with `get_queue_entry` (with and without
  `wait_seconds`), park and unpark it, hold and release the queue, cancel
  it, resume it, and list its outputs; read a job in brief, read its text,
  replace it with its `sha256`, and delete it; read an execution in brief
  and one of its runs in full; and delete an execution after a dry run.
- The audit rows of that flow all record the caller `mcp`.
- With writes off on the server, the write tools, `delete_executions`
  among them, are absent from the tool list; with writes on, they are
  present; a server restarted with the other setting sends a list-changed
  notification, to a 2026-07-28 client and to a 2025-11-25 one, and changes
  the list, without restarting `dtc mcp`. A job file write tool called on a
  server with writes off returns the API's `writes_off`, and
  `delete_executions` returns `writes_off` without reaching its endpoint.
- `get_queue_entry` with `wait_seconds` returns when the entry's state
  changes or a run ends, not when the next run starts or a step changes,
  so a chain takes about one call a run; returns at once for a finished entry,
  never waits longer than asked, takes up to 7200, keeps a client's idle
  timer alive with progress, and closes its watch when the call is
  cancelled.
- Through MCP, cancelling, parking, unparking, or resuming a person's entry,
  or releasing a person's hold, answers `not_permitted` and changes
  nothing; the same through `dtc queue` works.
- `GET /v1/queue/{id}/watch` streams the snapshots `WatchQueueEntry` sends,
  as SSE, with a keep-alive, and ends on shutdown; `mcp_server/` imports no
  `grpc`.
- `GET /v1/executions/{id}?brief=1` has no command and checks of stage and
  verdict only, `GET /v1/executions/{id}/runs/{run}` gives one run in full,
  and `GET /v1/jobs/{job}?brief=1` has no text for a valid job.
- No tool accepts a path, a `draw-things-cli` flag, a credential, or an
  unknown argument; no argument, dots and slashes included, reaches an
  endpoint other than its tool's; and an argument refused makes no request
  and is named in the error.
- With the API unreachable, the token file missing, or the token wrong,
  tools return `server_unreachable` or `unauthorized` naming `dtc serve`,
  and the MCP server keeps running.
- A non-loopback `--server-url` is refused without `--allow-remote-server`.
- `dtc mcp`, started as a process with no API running and driven over stdio
  by the SDK's client, connects, lists its tools, and answers a call with
  `server_unreachable`; its stdout holds protocol messages only.
- `.mcp.json` registers `uv run dtc mcp`, with no other argument.
- The token never appears in a tool result, log line, or error.
- `make check` passes; the user guide documents `dtc mcp`.
