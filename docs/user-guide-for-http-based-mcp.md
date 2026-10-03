# Testing the API with curl

`dtc mcp` speaks MCP over stdio, not HTTP, so there is no MCP port to curl
directly. What curl *can* reach is `dtc serve`'s HTTP API, which `dtc mcp`
drives: every MCP tool is one call to one endpoint, listed in
[the user guide's AI agents over MCP section](user-guide.md#ai-agents-over-mcp).
Curling the API exercises the same requests the MCP tools make, endpoint for
endpoint; see
[Server: HTTP API and gRPC monitoring](user-guide.md#server-http-api-and-grpc-monitoring)
for the full endpoint table, limits, and error shapes.

## Start the server

```bash
uv run dtc serve
# add --allow-write to also exercise PUT/DELETE job-file endpoints
```

Default bind is `127.0.0.1:8765`. First start creates `config/server-token`
(mode 0600, git-ignored).

## Get the token

```bash
TOKEN=$(cat config/server-token)
```

Every endpoint but `GET /v1/health` requires `Authorization: Bearer $TOKEN`.

## Discovering the endpoint list

The server serves its own OpenAPI schema at `GET /v1/openapi.json`, behind
the token (the interactive `/docs` and `/redoc` UIs are off):

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/openapi.json | jq

# Just the paths and methods
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/openapi.json \
  | jq -r '.paths | to_entries[] | .key as $p | (.value | keys[]) as $m | "\($m|ascii_upcase) \($p)"'
```

This is the live, authoritative list straight from the running server,
including whether the write endpoints are present (they only register when
`dtc serve` runs with `--allow-write`).

## Read-only calls

```bash
curl -s http://127.0.0.1:8765/v1/health | jq

curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/capabilities | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/jobs | jq
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8765/v1/jobs/J0001?brief=1" | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/inputs | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/queue | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/executions | jq
```

## Validate and submit a job

```bash
# Validate YAML text without writing anything
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @- http://127.0.0.1:8765/v1/validate <<'EOF'
{"yaml": "name: my-job\n..."}
EOF

# Queue an existing job by reference
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"job": "J0001"}' http://127.0.0.1:8765/v1/queue | jq

curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/queue/Q0001 | jq
```

## Watch an entry over SSE

```bash
curl -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/queue/Q0001/watch
```

A browser's `EventSource` cannot send the token, so this endpoint serves
clients that send headers, `curl` and `dtc mcp` included; see
[MCP token use and events](research/mcp-tokens-and-events.md).

## Write endpoints (`dtc serve --allow-write` only)

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"yaml": "name: test-job\n..."}' http://127.0.0.1:8765/v1/jobs/test-job | jq

curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/v1/jobs/test-job?expected_sha256=<sha from GET>"
```

Without `--allow-write`, `PUT` and `DELETE` answer `405 writes_off`.

## Notes

- IDs are always `J0001`/`Q0001`/`E0001` style, never a raw path or row
  number.
- `GET /jobs`, `/executions`, `/inputs`, and `/audit` are paged (`limit`,
  `cursor`).
- A curl request's caller defaults to `api`, distinct from `mcp`; it is bound
  by the same `max_queued_jobs`, `max_job_seconds`, and other
  `api_limits:` as any other caller (see
  [Server: HTTP API and gRPC monitoring](user-guide.md#server-http-api-and-grpc-monitoring)).
- A running `dtc serve` is almost always a real local queue, not a sandbox.
  Read-only calls (`health`, `capabilities`, `GET /jobs`, `GET /queue`, and
  similar) are safe to try freely; treat write, cancel, park, and hold calls
  with the same care you would give the MCP tools themselves.
