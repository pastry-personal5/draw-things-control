# Testing the API with curl

`fc mcp` speaks MCP over stdio, not HTTP, so there is no MCP port to curl
directly. What curl *can* reach is `fc serve`'s HTTP API, which `fc mcp`
drives: every MCP tool is one call to one endpoint, listed in
[the user guide's AI agents over MCP section](user-guide.md#ai-agents-over-mcp).
Curling the API exercises the same requests the MCP tools make, endpoint for
endpoint; see
[Server: HTTP API and gRPC monitoring](user-guide.md#server-http-api-and-grpc-monitoring)
for the full endpoint table, limits, and error shapes.

## Start the server

```bash
uv run fc serve
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
`fc serve` runs with `--allow-write`).

## Read-only calls

```bash
curl -s http://127.0.0.1:8765/v1/health | jq

curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/capabilities | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/jobs | jq
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8765/v1/jobs/J0001?brief=1" | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/inputs | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/queue | jq
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/executions | jq

# What each stage of a run took: the child's stages and Framecontrol's own, with each stage's share of the run
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/executions/E0012/runs/3/stages | jq '.stages[] | {stage, start, duration, share, done, total, unit}'

# The same stages inside the run, as `timing`
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8765/v1/executions/E0012/runs/3?stages=1" | jq .timing.complete

# With each stage's per-unit marks, for a plot or a fit
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8765/v1/executions/E0012/runs/3/stages?marks=1" | jq '.stages[] | select(.stage == "denoise") | .marks'
```

`/stages` answers `recorded: false` with no stages for a run that was never timed
(an execution from before stage timing, until `fc timing import` rebuilds it).
`seconds` is the run's time as its stages add up; a stage with a negative
`start` was work done for the run before it started and has no `share`. A stage's
`marks` are `[offset, units done]` pairs for its steps or frames (up to 1000 a
stage). They are left out unless the request has `?marks=1`, on `/stages` and on
`?stages=1` alike, so an answer an agent reads stays small; MCP's
`get_execution_run` never asks for them. A first mark estimated from a job log is
flagged by `uncertain`, which is always present.

## Validate and submit a job

Job text must be `version: 2`; a version-1 file is refused with a pointer to the
format, at field `version`. `GET /v1/capabilities` lists what a job may use under
`job_format`: the backends and their pipelines, the knob names, and the Draw
Things models whose frame rate is known (none today, so `knobs.seconds` is for
H3 runs; use `knobs.frames` for Draw Things).

```bash
# What a job may say
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/capabilities | jq .job_format

# Validate YAML text without writing anything
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @- http://127.0.0.1:8765/v1/validate <<'EOF'
{"yaml": "version: 2\nname: my-job\n..."}
EOF

# Queue an existing job by reference
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"job": "J0001"}' http://127.0.0.1:8765/v1/queue | jq

curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/queue/Q0001 | jq
```

A running entry carries `forecast`: the active `stage` with `stage_done` and `stage_total`, a `run` and a `job` span
(`started_at`, `end_at`, the 80% range `low` to `high`, all epoch seconds, and the `floor` a progress bar never falls
below, with `progress` and `seconds_left` as of the read), and the `basis` it rests on (`tier`: `signature`,
`backend_fit`, `pipeline_fit`, `nearest`, `factory`, or `live_only`). A span is `null` when the server has no number for
it; `forecast` is `null` for an entry that is not running. MCP's `get_queue_entry` returns the same object, and a new
forecast does not end its wait.

## Watch an entry over SSE

```bash
curl -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/queue/Q0001/watch
```

A browser's `EventSource` cannot send the token, so this endpoint serves
clients that send headers, `curl` and `fc mcp` included; see
[MCP token use and events](research/mcp-tokens-and-events.md).

## Write endpoints (`fc serve --allow-write` only)

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"yaml": "version: 2\nname: test-job\n..."}' http://127.0.0.1:8765/v1/jobs/test-job | jq

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
- A running `fc serve` is almost always a real local queue, not a sandbox.
  Read-only calls (`health`, `capabilities`, `GET /jobs`, `GET /queue`, and
  similar) are safe to try freely; treat write, cancel, park, and hold calls
  with the same care you would give the MCP tools themselves.
