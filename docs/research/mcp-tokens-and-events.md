# MCP: token use and events

How an agent driving `dtc` over MCP spends tokens, how a server can tell an MCP client that something happened, and where SSE could serve the API server, for [Phase 3, Milestone 10](../phase-3/milestone-10-mcp-server.md). Researched 2026-10-02. What the owner chose is in the [changelog](../phase-3/phase-3-changelog.md) of that day.

## Summary

- **Turns cost more than bytes.** Each tool call is a model turn that reads the whole conversation again (mostly from the prompt cache). Trimming an answer saves 1 to 35% of a few hundred tokens; not polling saves whole turns. Watching a 30-run chain with a 600-second cap takes about 225 calls, and with a wait that ends at the next change about 30.
- **Claude Code defers MCP tool schemas.** Only the tool names and the server's `instructions` stay in context every turn; a tool's schema loads when the agent uses it. The number of tools matters little; the length of `instructions` matters on every turn.
- **Only a held call or a channel pushes.** A tool call that waits is standard MCP and, in Claude Code, moves to the background after 2 minutes, so the agent is told when it ends. Claude Code's channels push into a session, but are a research preview behind a flag. No other MCP notification reaches the model in Claude Code.
- **SSE needs no new dependency.** FastAPI has it built in (`fastapi.sse`), and the `mcp` package brings `sse-starlette` too.

## Sources

- Context7, 2026-10-02: the MCP Python SDK v2 documentation (`mcp` 2.2.0 on PyPI), the MCP TypeScript SDK v2 documentation, Claude Code's documentation (`code.claude.com`), and FastAPI's.
- Sizes measured on a copy of `state/dtc.db` and on `data/jobs/duo-blend-i8x.yaml`, with `server/serializers.py`.

## Token use

### What stays in context

- Claude Code's tool search is on by default for MCP: "only tool names and server instructions enter context until Claude uses a specific tool". `ENABLE_TOOL_SEARCH` changes it; `auto` loads schemas upfront while they fit in 10% of the context. A client without tool search loads every schema on every turn.
- So `instructions` should be a few lines, and each description as short as the facts it must carry. A schema, once loaded, stays in the conversation.
- A tool list that changes mid-session (writes turned on or off) changes the tool definitions; in a client that loads them upfront, that also changes the cached prompt prefix.

### What a result costs

- When a result has `structuredContent`, Claude Code passes the JSON to the model and drops text blocks, "assumed to duplicate the structured data" (documented for the Agent SDK's tools). Sending both forms costs Claude Code's agent one copy.
- Past `MAX_MCP_OUTPUT_TOKENS` (25,000 by default; a warning at 10,000), Claude Code saves a result to a file in the session's `tool-results` directory and gives the agent its path, to read in parts, a turn each. A tool can raise its own threshold, up to 500,000 characters, with `_meta["anthropic/maxResultSizeChars"]` in its `tools/list` entry.
- Measured, in bytes:

| Result | As the API sends it | Compact JSON | Compact, nulls dropped |
|--------|--------------------:|-------------:|-----------------------:|
| `get_queue_entry` | 740 | 687 | 480 |
| `get_job` (`duo-blend-i8x`) | 4,708 | 4,642 | 4,630 |
| `list_executions`, 20 rows | 7,252 | 6,770 | 6,437 |
| `get_execution` (E0018, 6 runs) | 76,307 | | |

- `get_job`'s 4.6 KB is the YAML text (2.5 KB) and the resolved job, whose prompt pairs (1.4 KB) repeat the text. Without the text it is 2.1 KB; without the prompt pairs, 3.2 KB.
- E0018's runs are 12.6 KB each: the command 2.5 KB, and the checks' `facts` about 8 KB. Without the command, and with each check as its stage and verdict, a run is 0.6 KB.

### Turns per job

At about 75 minutes a run, cooldown included (E0018: 6 runs in 7.5 hours):

| How the agent watches | 6 runs | 30 runs | Catch |
|-----------------------|-------:|--------:|-------|
| `wait_seconds` capped at 600 | ~45 | ~225 | |
| Wait until the next change an agent acts on, capped above the longest gap (a 3600-second run and an 1800-second cooldown) | ~6 | ~30 | |
| Wait until the job finishes | 1 | 1 | 30 runs take about 37.5 hours, past the 27.8-hour default `MCP_TOOL_TIMEOUT`; the wait is lost if the session exits |
| Channel push at each run's end | ~6 | ~30 | A research preview, behind a flag |

## Telling the client something happened

### A call that waits

- Claude Code puts no per-request timer on a stdio server. A tool call is bounded by `MCP_TOOL_TIMEOUT` (100,000,000 ms, about 28 hours, by default, or the server's `timeout` in `.mcp.json`) and by an idle limit: 30 minutes with no answer and no progress notification for a stdio server, 5 minutes for an HTTP one (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`).
- A main-conversation call still running after 2 minutes moves to a background task (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`, v2.1.212 or later): the agent gets a task ID, keeps working, and the result arrives as a task notification. The task shows the latest progress the server reported, and does not survive the session's end. Calls from subagents, and calls in a `-p` run unless `CLAUDE_AUTO_BACKGROUND_TASKS=1`, are not moved.
- The MCP TypeScript SDK's default request timeout is 60 seconds (`DEFAULT_REQUEST_TIMEOUT_MSEC`), and a client may ask it to restart the timer on progress (`resetTimeoutOnProgress`). A client built on those defaults ends a longer wait.
- The low-level Python server reports progress with `ctx.session.report_progress(progress, total, message)`, which does nothing when the client gave no progress token.

### Claude Code's channels

- A channel is a stdio MCP server that declares `experimental: {"claude/channel": {}}` and sends `notifications/claude/channel` with `content` and `meta`; the event reaches the session as `<channel source="..." key="value">content</channel>`, and the server's `instructions` tell Claude what to do with it.
- During the research preview, `--channels` takes plugins from an allowlist only; a custom server needs `claude --dangerously-load-development-channels server:<name>`. Neither flag is listed in `claude --help`.
- A server that negotiates the 2026-07-28 protocol cannot deliver channel messages, so Claude Code registers a channel only on the earlier handshake (the default for stdio unless `MCP_PROTOCOL_NEGOTIATION=auto`).
- Events arrive only while the session is open, and each one starts a turn. Its content lands in the conversation, so it must hold only what the server generates, never text a job or a child process wrote.
- The Python SDK can do it: the low-level server takes `experimental_capabilities`, and its connection's `notify(method, params)` sends any method.

### Other ways, and why not

- **Resource subscriptions.** `notifications/resources/updated`, on `resources/subscribe` up to 2025-11-25 and on a `subscriptions/listen` stream from 2026-07-28. Nothing in Claude Code's documentation passes one to the model.
- **Tasks.** The SDK's v1 experimental task-augmented tool calls are removed in v2, whose migration guide says to report progress from an inline call instead.
- **Logging notifications.** Deprecated as of 2026-07-28 (SEP-2577).
- **Claude Code's Monitor tool.** It runs a command in the background and turns each stdout line into an event, or reads a WebSocket. Each watch ends within 30 minutes, after which Claude gets one notice and may start it again, so a 75-minute run costs about 3 turns. It needs a shell, and runs under the Bash permission rules.

## SSE for the API server

- **FastAPI.** `fastapi.sse.EventSourceResponse` and `ServerSentEvent` (`data`, `event`, `id`, `retry`, and `comment` for keep-alives); a route reads `Last-Event-ID` as a header. `server/event_backlog.py` already keeps events as JSON, with IDs that reset across server runs, so an SSE stream of events would be an adapter over it.
- **Browsers.** `EventSource` cannot send an `Authorization` header, and Milestone 11 refuses a token in the query string, so an SSE endpoint serves clients that send headers (`httpx`, `curl`).
- **Shutdown.** Uvicorn waits for open connections when it stops, so a stream must end itself when the server shuts down, as the 2026-09-25 SSE plan did.
- **Testing.** `httpx.ASGITransport` may collect a whole response before returning it, which would never return for an endless stream; check it, and run the app under uvicorn on a loopback port if so.
- **MCP over HTTP.** The low-level server's `streamable_http_app()` is an ASGI app (SSE responses, an `event_store` for `Last-Event-ID` resumption, DNS-rebinding protection for loopback) that mounts in FastAPI, with its session manager run from the host app's lifespan. Claude Code would reach it with `"type": "http"` and a `headersHelper` that reads the token. Against it: channels need stdio; Claude Code gives an HTTP server a per-request timer of at least 60 seconds unless the server's `timeout` is raised, and a 5-minute idle limit; and `dtc serve` would import the MCP server, against the rule that `mcp_server/` reaches the rest only over HTTP.
- The 2026-09-25 decision chose gRPC over SSE for one typed, generated contract every client shares; SSE then "needed no new dependency" too.
