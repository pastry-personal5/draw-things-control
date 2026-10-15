# Milestone 02: Refactor for multiple backends

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Phase 3](../archive/phase-3/README.md)'s shared job executor, queue, state store, and front ends.

## Goal

Make a second generator an additive backend implementation. A change to its settings, command, or media format should stay in its adapter and tests, without adding generator-specific branches throughout job execution, queueing, history, and front ends. Preserve existing Draw Things behavior and make the boundary usable by [Milestone 03](milestone-03-h3-i2av.md).

## Scope

In scope:

- One backend per job and per chain, selected by the top-level `backend` key. An omitted key means `draw-things-cli`, including in old job text. The only production adapter in this milestone is Draw Things.
- A reserved top-level `backend_settings` mapping. It must be absent or empty for Draw Things; Milestone 03 defines and validates its H3 keys. `config_file` and `config_override` keep their current Draw Things meanings and are not a generic backend settings format.
- Backend-owned validation, capability rules, command preparation, required-tool checks, output validation, media finishing, and handoff preparation. Shared code owns chain order, prompt scheduling, seed continuity, naming, cooldown, cancellation, run-boundary resume, event delivery, and recording.
- Backend identity and safe, effective settings in queue snapshots, execution records, manifests, history, and previews. Existing files and records continue to read as Draw Things.
- A test-only fake backend that exercises the contract through planning and execution; it is not a user-selectable backend.

Out of scope:

- H3 execution, `i2av`, and H3's settings; these belong to Milestone 03. `backend: h3` must be rejected until that adapter exists.
- Changing the one-off `generate` command, its Draw Things options, or its public behavior.
- Dynamic plugin loading, per-run backend switching, automatic backend selection from `mode` or file extension, user-supplied executable paths in job files, non-process backends, and abstractions for unimplemented backend features.

## Current coupling to remove

- `jobs/parsing.py` requires a Draw Things `config_file`, loads its base YAML, and validates Draw Things overrides and output format for every job.
- `jobs/planning.py` constructs `DrawThingsGenerateArguments`; `jobs/launcher.py` invokes the Draw Things `GenerationService`; `jobs/run_finisher.py` applies Draw Things color tagging, half-level frame handling, drift checks, and correction to every video run.
- `core/process/runner.py` already supervises a command vector, but its factory is typed to Draw Things arguments. `services/toolkit.py` wires that factory and the Draw Things media tools into the job executor.
- Queue entries keep both legacy job/config columns and a versioned `snapshot`. Resume currently rebuilds an entry from those columns. Executions and manifests have `mode` and Draw Things settings, but no backend identity. A refactor must not let the two queue representations disagree.

## Job and backend contract

- Parse common fields once: name, input path, run count, prompt schedule, output directory, timeout, cooldown, and safe path limits. Resolve `backend` before interpreting mode, settings, color, and output format. The registered adapter validates its supported `(backend, mode)` combinations and backend-owned fields with field-specific `InputError`s. Keep the validated backend configuration typed; do not grow `JobDefinition` with optional fields for each future generator or pass unvalidated dictionaries to execution.
- Keep the backend registry explicit and small in the `jobs` layer; pass it to parsing, planning, and execution. Wire machine-dependent tools from `services/toolkit.py`. The shared `jobs` layer depends on a backend protocol and common run values; adapters may import lower-layer `core` utilities. State, services, and front ends do not select behavior with `if backend == ...` chains. A missing installed adapter is a validation error for new work and a clear refusal to execute or resume persisted work; history remains readable.
- The backend supplies its supported modes, required or forbidden job fields, default and allowed output extensions, output contract, and any bounded finishing cost used by API job limits. Input resizing and handoff rules are explicit capabilities, not inferred from `is_video` alone. An unsupported combination identifies `backend`, `mode`, or the offending field before queue submission.
- The backend prepares one run from common inputs: validated settings, prompt pair, seed, input/handoff path, reserved output path, and timeout. It returns an immutable command specification containing the executable name, argument vector, capture policy, and a safely redacted preview. The same specification feeds the process runner, `RunStarted`, saved commands, and dry-run output. Backend-specific redaction must happen before a command enters events, logs, HTTP responses, or storage.
- A backend finishes a successful child output and returns a verified artifact: output path, optional next-run handoff, actual media properties (including audio presence when required), and diagnostic checks. It declares failures for missing, invalid, or unextractable required output. The executor retains the public status and exit-code mapping for missing tools, child failure, timeout, interruption, and finishing failure. A handoff is recorded only after its artifact is complete, so resume starts at the last successful run boundary.
- The Draw Things adapter owns base-configuration loading, override translation, model and seed resolution, `DrawThingsGenerateArguments`, output-format rules, ProRes tagging, half-level frame extraction, color drift, and correction. Reuse the current `core` Draw Things modules because one-off `generate` still needs them; remove their direct use from the common job planner, launcher, and finisher. Keep the existing process-group supervisor, cancel token, run lock, queue worker, and event stream.
- This milestone rejects `backend_settings` for Draw Things unless it is an empty mapping. Milestone 03 adds H3's schema under that key, allows `config_file` to be absent only for H3, and rejects Draw Things-only `config_override`, `output.video_format`, negative prompts, and `color` for H3. H3 color handling needs a separate design; Draw Things' half-level and ProRes behavior must never run on H3 output.

## Persistence and compatibility

- Add an explicit backend field to execution and queue rows, with a forward-only migration that makes every existing row `draw-things-cli`, including one-off `generate` rows. Keep `source_kind` (`job` or `generate`) separate from backend. Add backend to `JobStarted`, manifest, execution settings, and the queue's canonical tagged job snapshot. Existing manifests and execution settings missing it read as Draw Things; an explicit unknown value is retained for display and refused only when someone tries to run or resume it.
- Version the tagged job snapshot for the new backend payload, leaving one-off generation snapshots on their existing path. Read old version-1 job snapshots and legacy queue rows as Draw Things. For new entries, validate the exact job text and Draw Things base-config text being stored, then persist the chosen backend and validated, non-secret effective settings with them. On claim, parse the stored snapshot and check its identity against the row's display/index field; never re-read a live job file or base configuration to decide what runs.
- A resume carries forward the original snapshot and backend-specific settings, including the base-config text for Draw Things. It may change only the queue metadata and resolved resume point. Do not reconstruct a newer snapshot from the legacy columns or current files. Preserve the original seed and handoff path, and refuse a resume when the recorded backend cannot be loaded or the required handoff is gone.
- Keep existing manifest and HTTP fields additive. Expose `backend` in job details/previews, queue rows, execution summaries/details, and the corresponding CLI, TUI, and MCP presentations where work is identified. Store effective backend settings as a backend-namespaced, serializable value with sensitive values excluded; retain Draw Things' existing `config_file` and `config_override` fields for compatibility. The adapter owns a readable settings summary; a front end must not parse saved Draw Things flags to identify a backend.
- Preserve historical import: a manifest without `backend` imports as Draw Things; a manifest with an explicit backend keeps it. Import does not require a currently runnable adapter or alter the source manifest.

## Implementation order

1. Characterize existing Draw Things jobs, previews, command vectors, output names and media checks, events, queue recovery, resume, manifests, and API responses with focused tests. Include old job text, old snapshots, and a pre-migration database fixture.
2. Add backend identity and the registry; split common parsing from Draw Things parsing. Keep missing `backend` and existing version-1 jobs valid. Reject unknown backends and unsupported fields before enqueue.
3. Introduce the command and finished-artifact contracts. Make the process factory accept a backend-neutral command specification, and move Draw Things preparation and finishing behind its adapter. Keep one-off `generate` on its existing public path.
4. Add the state migration, snapshot reader/writer, resume propagation, manifest/import handling, and additive event/API fields. Give one canonical representation authority at each step, with consistency checks for duplicated queue columns.
5. Update CLI, TUI, HTTP, and MCP display through shared services. Run the fake backend through a multi-run chain, interruption, and resume to prove the shared path has no Draw Things dependency. Update [architecture](../architecture.md) and the [user guide](../user-guide.md) when implementation lands.

## Acceptance criteria

- An existing version-1 job with no `backend`, a pre-migration queued entry, and a pre-migration manifest/execution all resolve to Draw Things. Existing commands, output names, status transitions, color behavior, cooldown, cancellation, queue recovery, and resume stay the same.
- `backend: draw-things-cli` behaves like omission. Unknown `backend`, unsupported `mode`, nonempty Draw Things `backend_settings`, and a backend-owned field used with the wrong backend fail before queue insertion with the offending field named. `backend: h3` remains unavailable in this milestone.
- A test-only second adapter plans and completes a multi-run chain, produces its own command and handoff, and survives cancel/timeout and run-boundary resume without importing Draw Things argument/config types in shared job execution.
- Dry runs, queue entries, manifests, execution history, HTTP responses, and the front ends identify the backend. Commands and effective settings recorded or exposed through them are redacted. A queued or resumed job uses the original backend and settings even after its job/config files change.
- A missing executable, failed child, missing or invalid output, cancellation, and timeout follow the existing public error/status rules. Backend-specific finishing cannot mark a run successful without a valid handoff when another run needs one.
- The module dependency rule remains intact, focused compatibility and contract tests pass, and `make check` passes when the implementation lands.
