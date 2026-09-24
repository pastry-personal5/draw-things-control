# Milestone 04: MCP Server

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 02: HTTP API](milestone-02-http-api.md), [Milestone 03: Job file management](milestone-03-job-file-management.md)

## Goal

Give AI agents typed tools and resources for the API, so an agent can do the
whole create, queue, watch, cancel workflow without knowing HTTP.

## Scope

In scope:

- `dtc mcp`, an MCP server over stdio
- Tools that map to API endpoints
- Write tools registered only when the server has writes on
- Clear behavior when the API server is not running

Out of scope:

- Any generation, queue, or file logic of its own. It only calls the API.
- Direct access to the GPU, the database, or job files
- Streaming transports other than stdio, or network exposure

## Planned changes

### The `mcp` command

- `dtc mcp [--server-url http://127.0.0.1:8765] [--token-file config/server-token]`
  runs an MCP server on stdio, built with the official `mcp` Python SDK.
  Its documentation is fetched through Context7 first.
- It talks to the API with `httpx` and the same bearer token from the token
  file. It reads the token when it starts and never prints it.
- Because it is a client, several MCP sessions can share one server, and the
  server remains the only process that owns the GPU.

### Tools

Read and run (always registered):

| Tool | API |
|------|-----|
| `list_jobs` | `GET /jobs` |
| `get_job` | `GET /jobs/{name}` |
| `list_inputs` | `GET /inputs` |
| `validate_job` | `POST /jobs/{name}/validate` |
| `preview_job` | `GET /jobs/{name}/preview` |
| `submit_job` | `POST /queue` |
| `get_queue` | `GET /queue` |
| `get_status` | `GET /queue/{id}` |
| `cancel_job` | `POST /queue/{id}/cancel` |
| `resume_job` | `POST /queue/{id}/resume` |
| `get_history` | `GET /history`, `GET /history/{id}` |
| `list_outputs` | `GET /outputs/{job_run_id}` |

Write (registered only when `GET /capabilities` reports writes on):

| Tool | API |
|------|-----|
| `create_job` | `PUT /jobs/{name}` |
| `replace_job` | `PUT /jobs/{name}?overwrite=1` |
| `delete_job` | `DELETE /jobs/{name}` |

- Tool arguments are the same names and bounds as the API: a job name, a
  queue id, YAML text. There is no argument that carries a `draw-things-cli`
  flag, a path, or a credential.
- Tool descriptions say what is refused and why: for example, that
  `delete_job` moves the file to the trash, that inputs must already be in
  the input directory (`list_inputs` shows them), and that a job the queue
  is using cannot be changed.
- Results are compact JSON with the same `code` and `field` on errors. Long
  lists are paged.
- `get_status` does not wait. Agents poll it; a tool never blocks for the
  length of a generation. (`get_status` includes the cooldown end and the
  current run so an agent can choose when to look again.)

### Resources

Read-only, small: job files as text (`job://{name}`) and the resolved job
summary, so an agent can read a job before editing it. They come from the API
and never from disk.

### When the API is down

If the server is not reachable, or the token is missing or wrong, each tool
returns an error that says so and names the command that starts the server
(`dtc serve`), not a stack trace.

## Acceptance criteria

- With a fake API (or `TestClient` transport), each tool calls the intended
  endpoint and returns its result, and errors keep `code` and `field`.
- With writes off on the server, the write tools are absent from the tool
  list; with writes on, they are present.
- No tool accepts a path, a `draw-things-cli` flag, or a credential.
- With the API unreachable or the token wrong, tools return a readable error
  and the MCP server keeps running.
- The token never appears in a tool result, log line, or error.
- An end-to-end test with the real MCP client library over stdio against a
  test API: create, validate, submit, status, cancel, and outputs.
- `make check` passes.
