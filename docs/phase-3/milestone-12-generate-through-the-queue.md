# Milestone 12: Generate through the queue

**Phase:** [Phase 3: API server and MCP server for AI](README.md)
**Status:** done (2026-10-04)
**Depends on:** [Milestone 03: Queue for people](milestone-03-queue-for-people.md) and [Milestone 11: Safety hardening](milestone-11-safety-hardening.md)

## Goal

Make `dtc serve`'s worker the only component that starts `draw-things-cli`. `dtc generate` becomes a client of that worker: it submits one bounded generation, returns its queue ID by default, and follows it when asked with `--wait`. It no longer runs a child, takes the run lock, or works while the server is down.

The result is a one-run queue entry and an ordinary execution-history record, without creating a job file. Job files remain the way to express chains, resumes, cooldowns, color preservation, and every agent action.

## Scope

In scope:

- A persistent, typed one-off generation snapshot in the shared FIFO queue
- A server route for a person-facing CLI to preview and submit that snapshot
- `dtc generate --wait`, using the existing queue watcher and cancellation behavior
- One-run execution records, queue events, cancellation, history, and output reporting for generated entries
- The safe local subset of the current command's options, with an explicit output inside `output_directory`
- Migration of existing queue entries without changing their snapshots or resume behavior

Out of scope:

- An MCP generation tool. Agents continue to validate, create, and submit job files.
- Remote, cloud, model-directory, terminal-preview, credential, and arbitrary configuration options.
- Prompt, image, audio, or configuration upload APIs.
- Resuming or parking a one-off generation. A one-run request has no next run, so its normal terminal states are `succeeded`, `failed`, `cancelled`, and `interrupted`.
- Creating a job file from a one-off request, or converting a job into one.
- Starting or embedding `dtc serve` when the command cannot reach it.

## Owner decisions

1. **Return by default; `--wait` is opt-in.** `dtc generate` prints the queued entry (`Q0007 queued: cube.png`) and exits once the server accepts it. `--wait` follows the entry and exits with its final queue-state code, the same contract as `dtc queue add --wait`.
2. **Support a safe local subset.** It accepts model and prompt settings, generation dimensions and seed, a named base configuration, local input media, the supported video settings, and an output. Remote and cloud execution, all credentials, `--models-dir`, arbitrary `--config-json`, terminal preview, downloading, and arbitrary local paths are removed from this command.
3. **Agents use job files.** No MCP tool is added. This avoids a second agent-facing structured generation schema and leaves each agent-visible generation subject to the existing job validation, limit, audit, and caller rules.
4. **Every queued one-off has an explicit output.** `--output` is required, is a relative path beneath the configured `output_directory`, and `--terminal-image` is removed. The server resolves and confines it before any file operation. Its parent directory must already exist, preserving the direct command's behavior and preventing an implicit directory-writing API.

## Behavior

### The command

`dtc generate` keeps the flags below and gains the standard server-client options `--server-url`, `--token-file`, and `--allow-remote-server`.

| Category | Kept flags | Queue rule |
|---|---|---|
| Model and prompts | `--model`, `--prompt`, `--prompt-file`, `--negative-prompt`, `--negative-prompt-file` | A prompt file or stdin is read by the CLI before submission and sent as text. The server stores the text snapshot; it never receives a client-side path. |
| Base configuration | `--config-file` / `--config` | A bare YAML name in `data/params/`; the server validates it, captures its text, and resolves the model before queuing. |
| Generation settings | `--steps`, `--cfg`, `--width`, `--height`, `--frames`, `--strength`, `--seed` | The existing typed `DrawThingsGenerateArguments` checks remain the source of truth. |
| Input media | repeated `--image`, `--audio`, `--avc`, `--segment-frames`, `--cond-frames` | Each path is relative to `input_directory`, resolves inside it, and is captured as a resolved server path. Reference images retain their order. |
| Output | `--output`, `--video-format` | Output is required and relative to `output_directory`; `.mov` keeps the existing default `prores4444`. |
| Queue control | `--wait`, `--dry-run` | `--wait` watches the submitted entry. `--dry-run` calls the preview endpoint, prints the redacted command, and writes nothing. |

`--timeout` remains but is required and becomes the one-off's `run_timeout_seconds`; it is checked against `max_job_seconds`. The command removes `--shutdown-grace`, `--executable`, `--models-dir`, `--config-json`, `--audio-encoder-file`, `--terminal-image`, `--terminal-image-protocol`, `--download-missing`, `--disable-preview`, `--offline`, every `--remote*` flag, and every `--cloud*` flag. Server startup owns the executable and shutdown grace. Existing scripts that use a removed option must either use a supported one-off invocation or express the work as a job file.

An unreachable server, an invalid token, or a server without the new endpoint uses the existing API-client error wording and exits `state_unavailable`; the command never falls back to a direct child. Ctrl-C during `--wait` cancels the entry through the existing endpoint. Without `--wait`, callers use `dtc queue show Q0007`, the TUI, or `dtc queue add`'s existing facilities to follow it.

### API and audit

| Method and path | Purpose |
|---|---|
| `POST /v1/generations/preview` | Authenticated validation and a redacted `draw-things-cli generate` command; writes and audits nothing. |
| `POST /v1/generations` | Authenticated validation, snapshot insertion through `QueueWorker.enqueue`, and a normal queue-entry response. |

The submit route exists with writes off: queue submission already does not need `--allow-write`. It writes exactly one audit row with action `generate`, caller `cli`, the canonical relative output as target once it resolved, and the final typed outcome. Refusals before route parsing and oversized request bodies are included in the same audit treatment as `POST /v1/queue`; raw prompt text, file paths from the client, and credentials never become audit targets.

The request model is deliberately separate from the job YAML model. Its parser must reject unknown keys, NUL text, missing or nonpositive timeout, values that `DrawThingsGenerateArguments` rejects, output traversal and symlinks, and input media outside the configured input directory before opening it. Prompt text and all captured configuration text count toward the existing request-body cap; each individual text field is bounded by `max_job_file_bytes`. Named configurations use the existing symbolic-link and bounded-read rules. The service rejects existing secret-bearing fields rather than accepting and redacting them after the fact.

### Queue snapshot, worker, and history

Schema 10 replaces the job-specific queue payload with a versioned queue-entry snapshot while retaining the existing common queue state, queue number, timestamps, execution link, caller, and resume columns. A job entry serializes the exact fields it stores today; a generation entry serializes the resolved model, prompt text, captured named-configuration text, resolved input paths, relative output and resolved output path, video settings, timeout, and a single-run display name. The migration copies every schema-9 job row into the job snapshot byte-for-byte in meaning, marks it `kind: job`, and leaves its state, order, linked execution, and resume chain intact. New rows always have an explicit kind and snapshot. Repository readers decode the tagged snapshot once and never attempt to parse one as the other.

`QueueRow`, `NewQueueEntry`, serializers, queue events, gRPC snapshots, CLI queue listing, and the TUI identify the entry kind. Existing job responses remain compatible. A generation response has `kind: "generate"`, no `job_path`, `total_runs: 1`, `succeeded` 0 or 1, and a compact `generation` summary with its model and relative output. Human UI labels use `generate: <output>` instead of a fabricated job file name.

The worker dispatches by kind after claiming the oldest entry. Job entries continue through `JobRunSession` unchanged. A generation entry runs a new single-run session that shares the existing runner factory, executable resolution, run lock, child-PID recording, cancel token, queue status, and event publisher. It reserves and links an execution before invoking the child, writes one `runs` row with the redacted command and final output, and checks that a zero exit status produced the requested output. It never runs job planning, media color correction, run-to-run cooldown, last-frame extraction, or resume logic.

Execution history gains a source-kind field and nullable job-file fields for one-offs. A generation record has `source_kind: "generate"`, display name `generate: <output>`, mode inferred from the output extension, total runs 1, the captured request JSON, and the same status, command redaction, output, duration, exit-code, and deletion behavior as a job execution. List, detail, run-detail, and output routes expose the source kind so the CLI and TUI do not describe a one-off as a job. Existing filters by job remain job-only; name filtering can find `generate:` labels.

The worker applies no between-entry cooldown after a generation entry. Its queue state changes, gRPC entry snapshots, SSE events, cancellation, shutdown recovery, hold behavior, and queue ordering use the existing common mechanisms. Park and resume reject a generation entry with `invalid_state`.

## Planned changes

1. **Model and validate a one-off request.** Add a service-layer generation snapshot and parser that turn the approved HTTP body into safe `DrawThingsGenerateArguments`, applying the path, YAML, text-size, NUL, timeout, executable, and output rules above. Factor shared argument validation out of the direct CLI service only where it is needed by both paths; do not let the server accept an arbitrary argv vector.
2. **Generalize persistent queue entries.** Add schema 10, tagged snapshot dataclasses, repository migration and queries, and compatibility tests for schema-9 job and resume rows. Make queue listing, serialization, events, gRPC, CLI text, and TUI rendering branch on the entry kind rather than on placeholder file names.
3. **Run and record one-offs.** Add the one-run session and recorder path, dispatch it from `QueueWorker`, link execution IDs before start, record safe command and output details, and preserve current cancellation, shutdown, and failure semantics. Keep the job worker path unchanged except for tagged dispatch.
4. **Expose the server API.** Add preview and submit routes, body schemas, authentication, audit action discovery, error mapping, output target canonicalization, and OpenAPI coverage. The MCP tool catalogue stays unchanged and tests prove it has no one-off tool.
5. **Convert the CLI.** Make `generate` normalize prompt files/stdin into bounded text, submit or preview through the authenticated API client, print the queue ID, and reuse `wait_for_entry` for `--wait`. Remove its runner, run-lock, direct executable, and unsupported-option plumbing.
6. **Document and verify the behavior.** Replace the direct-generation guide examples and phase/architecture exception with server-first examples, list the accepted and removed flags, explain output confinement and server-down behavior, and record the decisions in the phase changelog.

## Acceptance criteria

- `dtc generate --output cube.png --timeout 60 ...` requires a reachable, authenticated server, returns a `Q` ID without starting a local child, and the server worker performs the only `draw-things-cli` invocation.
- `--wait` reports the same state and exit codes as `dtc queue add --wait`; Ctrl-C cancels the one-off entry. Without it, the command exits after a successful submission.
- `--dry-run` returns a redacted command after the server validates the exact request and neither queues nor audits a generation.
- A one-off gets FIFO ordering with jobs, respects a queue hold, cancels and recovers across server restart, and never has a job cooldown, park, or resume path.
- Every allowed input path and the required output is confined before access; traversal, symlinks out of bounds, missing media, a missing output parent, an oversized prompt/configuration, NUL text, a missing timeout, and every excluded option are typed refusals. No accepted request or stored command contains a credential.
- Queue, gRPC/SSE watches, history, outputs, CLI listing, and TUI distinguish a generation from a job and show its output, status, and execution. Existing job and resumed-job behavior survives schema migration unchanged.
- The API audit records accepted and refused generation submissions with a canonical output target or null, never caller-provided prompt or path text.
- MCP lists exactly its existing job-based tools and cannot reach the new generation endpoints.
- Unit, route, migration, CLI, TUI, gRPC, audit, restart-recovery, and documentation tests cover the boundaries above; `make check` passes.
