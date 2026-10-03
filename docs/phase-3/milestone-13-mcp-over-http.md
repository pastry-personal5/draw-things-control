# Milestone 13: MCP over Streamable HTTP

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done (2026-10-03)
**Depends on:** [Milestone 10: MCP server](milestone-10-mcp-server.md)

## Goal

Let an MCP client that cannot start `dtc mcp` on this machine, such as OpenClaw
in a virtual machine, reach the same tools over the network.

## Why

[Milestone 10](milestone-10-mcp-server.md) built `dtc mcp` on stdio alone: the
client starts it and talks over its pipes. A client in a virtual machine cannot
do that, and pointed at `dtc serve` instead it fails: `dtc serve`'s port speaks
the REST API (and an SSE watch of one queue entry at
`/v1/queue/{id}/watch`), not MCP. `openclaw mcp add NAME --url
http://192.168.64.1:8765 --transport sse` answered `SSE error: Non-200 status
code (404)`, and saved nothing, since `openclaw mcp add` connects before it
saves. Every path but `/v1/...` on that port is 404, `/`, `/sse`, and `/mcp`
included.

## Scope

- `dtc mcp --transport streamable-http`: the same server, tools, and resources,
  at `http://HOST:PORT/mcp`, with `--host` (default `127.0.0.1`), `--port`
  (default 8767), and `--allow-remote-bind`
- The server's own bearer token on every request, so a listener on the network
  adds no way in that the API does not already have
- Documents: the user guide, the architecture, and a script that registers it with
  OpenClaw

Not in scope: TLS (as for `dtc serve`, an SSH tunnel is the safer way across a
network), OAuth, a token of its own for MCP, or the older SSE transport of the
protocol (OpenClaw and the SDK both speak Streamable HTTP).

## As built

- `mcp_server/http.py`. `http_app` is the SDK's `Server.streamable_http_app` at
  `/mcp` behind `BearerAuth`, a pure ASGI check that answers every HTTP request
  without `Authorization: Bearer <the token in the token file>` with 401
  `{"code": "unauthorized", ...}` and `WWW-Authenticate: Bearer`, before the MCP
  app sees it. The token is read again for each request (a token `dtc serve`
  regenerated counts at once) and compared with `hmac.compare_digest`. The answer
  never says why, so an unreadable token file, which names a path in the log,
  does not name it to a caller. `serve_http` binds the socket itself (a taken port
  or an address this machine lacks is a `BindError`, not uvicorn's exit) and runs
  uvicorn with `ws="none"`, so the app sees HTTP and the lifespan alone.
- A loopback bind keeps the SDK's own check of the `Host` and `Origin` headers.
  Beyond loopback that check is off, since the clients' addresses are not known;
  the bearer token is what keeps a caller out, and a page in a browser has no way to know it.
- `mcp_server/app.py`: `run_http`, and `BindError` re-exported for `cli/app.py`,
  which may import `mcp_server.app` alone (`tests/test_architecture.py`).
- `cli/app.py`: `dtc mcp` gains the four options. `--host`, `--port`, and
  `--allow-remote-bind` with `--transport stdio` are refused (exit 2), not
  ignored; a `--host` beyond loopback is refused without `--allow-remote-bind`,
  as `dtc serve --host` is; a port that is taken is exit 2, naming it.
- The token is the server's own, in `config/server-token`: the client sends the
  same token the API takes, and `dtc mcp` forwards with its own copy of the file.
  A second secret would be one more file to leak and to rotate.

## Acceptance criteria

- [x] `dtc mcp` with no option is unchanged: stdio, as before
- [x] Without the token, or with another, every request to the HTTP transport is
  401 and no session is made; with it, a client lists the tools and calls them
- [x] A regenerated token is the one that counts at once, with no restart
- [x] OpenClaw 2026.9.3's `openclaw mcp add NAME --url http://HOST:PORT/mcp
  --transport streamable-http --header Authorization=Bearer TOKEN` connects, and
  `openclaw mcp probe` lists the tools, write tools included while `dtc serve`
  runs with `--allow-write`
- [x] A host beyond loopback without `--allow-remote-bind`, a transport that is
  not one of the two, a listen option with stdio, and a taken port are each exit 2
  before anything is served
- [x] `make check` passes (tests in `tests/mcp_server/test_http.py`)
