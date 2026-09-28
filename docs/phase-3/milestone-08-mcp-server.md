# Milestone 08: MCP Server

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 02: HTTP API](milestone-02-http-api.md), [Milestone 07: Job file management](milestone-07-job-file-management.md)

## Goal

Give AI agents typed tools and resources for the API, so an agent can do the
whole draft, create, queue, watch, cancel, and resume workflow of a long
chain without knowing HTTP.

## Scope

In scope:

- `dtc mcp`, an MCP server over stdio
- Tools that map one to one to API endpoints
- Write tools offered only while the API server has writes on
- Clear behavior when the API server is not running

Out of scope:

- Any generation, queue, or file logic of its own. It only calls the API.
- Direct access to the GPU, the database, or job files
- Transports other than stdio, or network exposure

## Planned changes

### The `mcp` command

- `dtc mcp [--server-url http://127.0.0.1:8765] [--token-file PATH] [--allow-remote-server]` runs an
  MCP server on stdio, built with the official `mcp` Python SDK. The token
  file defaults to the project's (`ProjectPaths.server_token`), wherever the
  MCP client starts the command from. A server URL that is not loopback is
  refused unless `--allow-remote-server` is given, the client's side of
  `serve --allow-remote-bind`, so a mistyped URL cannot send the token
  elsewhere.
- `mcp_server/` imports nothing else from the package
  (`tests/test_architecture.py`): `cli/app.py` passes it the URL and the
  token file's path, and imports it only inside the command. That makes
  `dtc mcp` a third allowed import between front ends, which the test, the
  development rules, and `AGENTS.md` name.
- Stdout carries the protocol and nothing else: every log line goes to
  stderr, unlike the other commands' logging.
- It talks to the API with `httpx`, sending the bearer token and the caller
  header the audit log reads (`mcp`). It also holds a generated gRPC client
  ([Milestone 02](milestone-02-http-api.md#monitoring-grpc)), sending the
  same token as `authorization` metadata, for `get_queue_entry`'s
  `wait_seconds` only; every other tool stays plain HTTP. It reads the token
  when it starts, and again after a failure from either client, so a changed
  token needs no restart. It never prints the token.
- Because it is a client, several MCP sessions can share one server, and the
  server remains the only process that owns the GPU (owner decision).

### Tools

Read and run (always offered):

| Tool | API |
|------|-----|
| `get_capabilities` | `GET /capabilities` |
| `list_jobs` | `GET /jobs` |
| `get_job` | `GET /jobs/{job}` |
| `preview_job` | `GET /jobs/{job}/preview` |
| `validate_job_text` | `POST /validate` |
| `list_inputs` | `GET /inputs` |
| `submit_job` | `POST /queue` |
| `get_queue` | `GET /queue` |
| `get_queue_entry` | `GET /queue/{id}` |
| `cancel_queue_entry` | `POST /queue/{id}/cancel` |
| `resume_queue_entry` | `POST /queue/{id}/resume` |
| `list_executions` | `GET /executions` |
| `get_execution` | `GET /executions/{id}` |
| `list_outputs` | `GET /executions/{id}/outputs` |

Write (offered only while `GET /capabilities` reports writes on):

| Tool | API |
|------|-----|
| `create_job` | `PUT /jobs/{name}` |
| `replace_job` | `PUT /jobs/{name}?overwrite=1` |
| `delete_job` | `DELETE /jobs/{name}` |

- The tool list is read from `GET /capabilities` whenever the client asks
  for it, so a server restarted with or without `--allow-write` changes it.
  With the API down, only the read and run tools are offered. A write tool
  called after writes were turned off returns the API's error.
- Tool arguments have the API's names and bounds: a job reference or name, a
  queue or execution ID, YAML text, a page cursor. No argument is a path, a
  `draw-things-cli` flag, or a credential; paths inside job text are
  confined by [Milestone 07](milestone-07-job-file-management.md#validation-before-writing).
  An unknown argument is rejected.
- Tool descriptions carry what an agent needs to succeed the first time:
  that inputs must already be in the input directory and match the job's
  size (`list_inputs` shows both); that `run_timeout_seconds` is required
  and the limits apply (`get_capabilities`); that `validate_job_text`
  checks a draft without writing; that `delete_job` moves the file to the
  trash; that a queued or running job's file cannot be changed; that
  cancelling a running entry loses the run in progress; that a stopped
  chain continues with `resume_queue_entry`, which reruns the run that was
  cut short (`get_queue_entry` says from which run); and that an output
  marked incomplete is a leftover, not a result.
- Results are compact JSON with the API's `code` and `field` on errors.
  Long lists are paged.
- No tool waits for a generation. `get_queue_entry` takes an optional
  `wait_seconds` (at most 30): with it, the tool reads one message from
  `WatchQueueEntry` (or times out) instead of calling `GET /queue/{id}`; the
  agent still makes one tool call and gets one JSON result, never a stream.
  The result includes the current run, its elapsed time, the last run's
  time, and `cooldown_until`, so an agent can choose when to look again.

### Resources

Read-only and small: each job file's text (`job://{job}`), so an agent can
read a job before editing it. They come from the API, never from disk.

### When the API is down

If the server is not reachable, or the token is missing or wrong, each tool
returns an error that says so and names the command that starts the server
(`dtc serve`), not a stack trace, and the MCP server keeps running.

### Documentation

The user guide gains an MCP section: starting it, a sample client entry, the
tools, and what the write flag changes.

## Acceptance criteria

- Through the SDK's in-memory client session, with `httpx` sending requests
  straight to the FastAPI app, a fake runner, and the gRPC client against an
  in-process `Monitor` service, each tool calls its endpoint and returns its
  result, and errors keep `code` and `field`.
- An agent's whole flow works that way: list inputs, validate a draft,
  create it, submit it, watch it with `get_queue_entry` (both with and
  without `wait_seconds`), cancel it, resume it, and list its outputs.
- With writes off on the server, the write tools are absent from the tool
  list; with writes on, they are present; a server restarted with the other
  setting changes the list without restarting `dtc mcp`.
- No tool accepts a path, a `draw-things-cli` flag, a credential, or an
  unknown argument.
- With the API unreachable or the token wrong, tools return a readable error
  naming `dtc serve`, and the MCP server keeps running.
- A non-loopback `--server-url` is refused without `--allow-remote-server`.
- `dtc mcp`, started as a process and driven over stdio by the SDK's client,
  completes the handshake, lists its tools, and answers a call with the API
  down; its stdout holds protocol messages only.
- The token never appears in a tool result, log line, or error.
- `make check` passes; the user guide documents `dtc mcp`.
