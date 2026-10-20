# Milestone 17: Comprehensive cleanup and refactoring

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 15](milestone-15-timing-gate-and-search.md); M15 must be complete before implementation begins. The cleanup starts from the final timing gate and search design rather than refactoring that subsystem while its behavior is still being built.

## Goal

Make the whole codebase easier to understand, test, and change without changing observable behavior. Test maintenance is the first priority: keep tests that protect distinct contracts, remove tests that repeat the same contract through the same production path, and make the remaining suite express behavior instead of implementation structure.

The milestone applies clean-code and clean-architecture principles pragmatically. Responsibilities become cohesive, orchestration is separated from pure decisions and I/O adapters, dependencies remain explicit, and interfaces are defined at the boundary that consumes them. It does not add layers, abstractions, or indirection without a demonstrated maintenance benefit.

## What clean means here

- A module has one cohesive reason to change, stated in domain language. File length by itself is not a reason to split it.
- A function either makes a decision or performs an effect where practical. An orchestration function may order both, but delegates each substantive decision or effect to a named collaborator.
- Mutable state lives for the shortest lifetime that owns it. Per-job and per-run state does not remain on a process-lifetime object without a concurrency reason.
- Dependencies are explicit at construction or call time. A narrow protocol is introduced only when it represents a real consumer capability, not merely to satisfy a test double or reduce an import count.
- Names describe jobs, runs, queue entries, artifacts, timing evidence, and user actions rather than implementation mechanics. Serialized names and visible terms remain unchanged.
- Errors are translated once at the boundary that can add useful context. Cleanup is local to the code that acquired the resource, and a broad exception boundary remains only where continued service is intentional.
- Duplication is removed when it duplicates knowledge or policy. Similar code stays separate when it belongs to different front ends, failure boundaries, or domain rules.
- Comments explain invariants, races, compatibility, and reasons. Comments that merely narrate simplified code are removed with that code.
- Tests describe contracts and state transitions. A test does not survive solely because it existed before, and a test is not deleted solely because another test has a similar name or setup.

## Behavioral boundary

This milestone is behavior-preserving. For the same inputs and controlled dependencies, it keeps:

- CLI commands, arguments, help, text, exit codes, and error details;
- TUI text, shortcuts, focus behavior, notifications, and command outcomes;
- HTTP, gRPC, SSE, and MCP request and response shapes, authentication, authorization, errors, and event order;
- job validation, compiled plans, queue snapshots, resume behavior, manifests, logs, database schemas and migrations, audit records, timing evidence, forecasts, and knob-file semantics;
- backend command arguments, process supervision, cancellation and parking behavior, media checks, output names, promoted artifacts, handoffs, and cleanup;
- concurrency, locking, atomicity, recovery, retention, and failure boundaries.

Undocumented internal Python classes, function signatures, and module locations may change. Proven-dead code may be removed. A possible product, compatibility, safety-policy, or error-handling improvement discovered during the work is recorded in [Milestone 18](milestone-18-deferred-behavior-changes.md) and is not made here. A suspected security or data-loss defect stops the affected refactoring slice for owner review rather than being silently preserved or changed.

## Current evidence

The package already has a useful enforced dependency direction and thin front ends. This milestone preserves that foundation instead of replacing it.

The initial review found these maintenance concentrations:

- `jobs/backends/h3.py` combines parsing, reference inspection, runtime preflight, arguments, timing facts, staged finishing, color delivery, promotion, recording, and reporting in one adapter. `H3Backend.finish` is 207 lines and is the largest production function.
- `jobs/executor.py` coordinates chain state, input boundaries, run lifecycle, timing, cooldown, parking, cancellation, events, and records in one stateful class even though planning, launching, and Draw Things finishing already have separate collaborators.
- `jobs/parsing.py` and `jobs/compile_v2.py` carry large parser/compiler objects with broad construction context and many validation helpers. Their distinction—reading one backend view versus compiling a version-2 plan—must remain, but the context and validation seams can become clearer.
- `services/queue_worker.py` owns the worker thread and also runs two kinds of entries, translates job events, manages forecast lifecycle, coordinates holds, parks and cancellation, and performs between-job waits.
- The completed timing subsystem spans pure model code and file/update orchestration. Its final Milestone 15 shape needs a responsibility and duplication pass only after that milestone is complete.
- Several server tests substitute workers that satisfy only a route's narrow needs, while `ServerContext` requires the concrete `QueueWorker`; this creates avoidable type suppressions and couples consumers to more behavior than they use.
- Repeated TUI pane focus and leave actions are candidates for the existing pane base. Some version, stream, and client code looks similar across front ends but is intentionally separate because the enforced architecture forbids those front ends from importing one another, and the MCP server is HTTP-isolated from the application package.
- The suite has no exact duplicate test-function bodies. Redundancy therefore has to be established from the protected contract and production path, not from textual similarity or test names. The large executor and queue suites contain deliberately different layers of coverage, so apparently similar state-transition tests need boundary-by-boundary review before deletion.

The last clean baseline before the ongoing Milestone 15 work passed `make check`. M17 implementation does not begin until M15 is done and the then-current worktree passes it again.

## Scope

In scope:

- a package-by-package responsibility and dependency review of `core`, `jobs`, `state`, `services`, `cli`, `tui`, `server`, and `mcp_server`;
- production refactors justified by excessive responsibility, a leaky boundary, duplicated decisions, hidden dependencies, hard-to-test orchestration, or dead code;
- test inventory, consolidation, deletion, fixture and fake cleanup, and organization around contracts;
- removal of unused internal code, imports, compatibility shims, branches, and test support after repository entry points, persisted formats, and documented interfaces prove them unreachable;
- clearer names and types where they do not change visible text or serialized names;
- current architecture and development documentation after modules or responsibilities move.

Out of scope:

- any intentional behavior, output, schema, format, command, UI, policy, performance, or dependency change;
- a numerical target for lines, functions, classes, modules, tests, coverage, or complexity;
- speculative interfaces for possible future backends or front ends;
- replacing small explicit code with a generic framework, base class, repository, event bus, or service locator;
- combining code across front ends when doing so would weaken their isolation or move framework dependencies into lower layers;
- modifying the protected user configuration and parameter files.

## Refactoring rules

Each change is small and independently reviewable:

1. Name the production responsibility or test contract being improved.
2. Identify the observable behavior and the smallest existing tests that protect it. Add a characterization test only when the behavior is otherwise unprotected.
3. Move or simplify one seam. Do not combine the move with an intentional behavior correction.
4. Delete dead code and redundant tests only after their callers and contract coverage have been checked.
5. Compare deterministic outputs before and after the slice when it touches serialization, commands, events, manifests, timing, or media facts.
6. Run the narrow affected tests, then `make check` before the next slice.
7. Update `architecture.md` in the same slice when module ownership changes.
8. Record a discovered behavior-change candidate in Milestone 18 with evidence and continue only if the refactor can preserve the current behavior.

A helper is extracted only when it names one responsibility, reduces repeated policy, narrows a dependency, or makes a contract directly testable. Similar-looking code remains separate when its ownership, failure boundary, or dependency direction differs.

Stop a proposed extraction when it moves code without reducing the caller's knowledge, creates a one-method wrapper with no boundary or invariant, makes execution order harder to see, or requires more test seams than the responsibility warrants. Leave the current code and record the review as complete instead.

## Planned changes

The review is comprehensive; the edits are selective. Each slice below is completed and validated before the next starts. Test organization and deletion happen inside the slice whose production contract they protect, not as a suite-wide deletion pass that weakens the safety net before refactoring.

### Slice 0: Stable baseline and contract map

- After M15 is marked done, confirm a clean worktree and record one passing `make check` baseline.
- Inventory the supported entry points and persisted readers: console commands, Typer registration, Textual bindings and handlers, FastAPI routes, gRPC methods, MCP tools, queue snapshot versions, database migrations, manifest/history import, backend registry hooks, and string-dispatched callbacks.
- For each later slice, list the owning tests and classify them as pure decision, adapter, integration, transport, process, concurrency, recovery, or characterization coverage. This is a working note in the milestone during implementation, not a new permanent documentation system.
- Capture deterministic before-values only where existing tests do not already pin them: compiled plan, redacted command, event dictionaries, manifest, API serialization, timing replay, knob transaction, or media facts.

Exit evidence: current `make check` passes, every planned slice names its contract tests, and no production code has changed.

### Slice 1: Test support and consumer capabilities

- Start with the repeated server worker fakes and their `ServerContext` type suppressions. Inventory the capabilities health, queue routes, entry snapshots, execution deletion, SSE, and gRPC actually consume.
- Introduce a production protocol only for a stable application capability used by real consumers. If the only problem is incomplete test doubles, prefer one complete typed fake in `tests/fixtures.py`; do not reshape production solely to satisfy Pyright.
- Review `Toolkit`, `ServerContext`, CLI context, and TUI construction for hidden ownership or test-only mutation. Use constructor injection or a small composition factory only where it removes a real alternate construction path.
- Remove duplicated fake setup, unused fake methods, and the associated type suppressions once every consumer uses a complete and honest seam.

Exit evidence: the affected server and MCP tests use typed test support without pretending that a partial object is a `QueueWorker`; process-lifetime ownership remains explicit; API behavior is unchanged.

### Slice 2: Small local simplifications

- Review repeated focus and leave handlers in the TUI, repeated local serialization guards, and trivial pass-through methods. Consolidate them only into an existing natural base or helper whose name describes the behavior; do not introduce a mixin or inheritance hierarchy for a one-line saving.
- Remove unused imports, unreachable private helpers, narration comments, and stale aliases proven unnecessary by Slice 0's entry-point inventory.
- Merge table-driven test cases with `subTest` only when they execute the same production path and differ solely in input and expected output.

Exit evidence: each change is local to one package, visible TUI behavior and framework discovery are covered unchanged, and the total concept count falls rather than moving code to a new file.

### Slice 3: Job parsing context

- Replace `JobParser`'s broad constructor with cohesive immutable context values for source and filesystem policy, frozen snapshot/reference data, and the internal version-2 run view. Keep `load_job` and `load_job_text` as the public entry points.
- Keep parsing and error translation in `JobParser`; do not create a class for each field. Extract a validator only when both legacy and version-2 paths currently encode the same rule.
- Preserve exact error type, path, field, and message, including filename enforcement, path confinement, missing resume inputs, reference snapshots, legacy frames, and frozen base configurations.
- Review `test_parsing.py`, `test_compile_v2.py`, examples, queue snapshot, and resume tests together. Consolidate only cases that reach the same parser mode and validation branch; legacy, file-read, text-snapshot, and run-view tests are distinct boundaries.

Exit evidence: parser call sites pass a small set of named contexts, no policy is duplicated between entry points, and all error and compiled-job characterization remains identical.

### Slice 4: Version-2 compilation phases

- Make `RunCompiler`'s existing phases explicit: catalog validation, defaults and selection, resolved run construction, backend view parsing, cross-run edge validation, and root-plan assembly.
- Prefer typed intermediate values and pure functions within `compile_v2.py` first. Split a module only if a group has its own inputs, outputs, invariant, and tests; do not convert each private method into a class.
- Keep backend adapters as the sole authority for backend settings and keep `JobParser` as the sole error-context translator. The compiler must not grow a second H3 or Draw Things validator.
- Remove temporary compatibility or remapping helpers only after accepted snapshot versions and exact error fields prove them unreachable.

Exit evidence: `compile()` reads as those phases in order, a run is still parsed once by its adapter, and every existing effective-plan and edge-validation result is equal.

### Slice 5: H3 finishing workflow

- Keep `H3Backend` as the `JobBackend` facade. Move the 207-line `finish` workflow into one backend-owned finisher with explicit request, staged-artifact state, and result values; keep it near the H3 adapter rather than in backend-neutral media code.
- Separate structural verification, source-color interpretation, delivery selection, optional correction, handoff extraction, promotion, record facts, reporting, and cleanup into named operations. Keep orchestration order visible in one method.
- Keep staged-file ownership in one object or context so every exit path cleans exactly the artifacts it owns. Atomic promotion remains the final mutating step, with cancellation checked at the same points.
- Preserve every warning, stage name, artifact role, media fact, record field, packet check, and success-without-handoff rule. Keep Draw Things and H3 color policy separate.
- Review `test_h3_finish.py`, H3 inspection/color tests, mixed-chain tests, executor timing tests, and media-check tests by boundary. A finisher unit case may replace another finisher unit case, but it does not replace an executor timing or mixed-chain integration case.

Exit evidence: `H3Backend.finish` is a delegation point, the finisher's stages and ownership are readable without tracing unrelated parsing code, and deterministic records/events/media reports compare equal.

### Slice 6: Per-execution state and chain orchestration

- Move the observer, log writer, timing state, before-start spans, and other state belonging to one `JobExecutor.run` call into the existing per-execution chain context or one cohesive execution session. Keep the thread-safe `CancelToken` on the long-lived executor because `cancel`, `park`, and `unpark` reach it from other threads.
- Leave `JobExecutor` as the public facade. Make its main flow read as prepare, run ordered steps, finish records, and close timing; extract run-boundary preparation, cooldown/park outcome, and event/timing adaptation only where each owns a complete state transition.
- Preserve event order, timestamps, cleanup, resume boundaries, first-image and anchor handling, and the rule that only one run call may be active.
- Partition the large executor test module by contract if that improves navigation. Compare apparent overlaps with `test_job_characterization.py`, `test_executor_timing.py`, `test_mixed_chain.py`, `test_color_executor.py`, and `test_h3_finish.py`; retain cross-boundary tests and delete only same-path duplicates.

Exit evidence: process-lifetime versus execution-lifetime state is evident from ownership, the top-level run sequence is readable in order, and cancellation/concurrency tests pass without new synchronization seams.

### Slice 7: Queue execution and coordination

- Keep `QueueWorker` responsible for its thread, claim serialization, wake/stop, and coordination of cancellation, park, hold, forecast, and between-job cooldown.
- Extract job-entry execution and one-off-generation execution only if each can receive one claim context and return one explicit outcome without taking ownership of the worker's locks. Do not split lock-coupled state across objects.
- Keep `QueueClaimGate`, `QueueHold`, and `WorkerStatus` as the existing focused collaborators unless the review finds duplicated policy. Preserve the documented lock order and event-publication order.
- Review worker, cancellation, parking, forecast, recovery, route, SSE, and gRPC tests as distinct boundaries. Similar park/cancel cases in executor and worker suites stay when one protects executor semantics and the other queue state or races.

Exit evidence: entry execution no longer obscures the claim loop, each mutable flag has one owner under one documented lock, and race, recovery, forecast, and cooldown behavior is unchanged.

### Slice 8: State, timing, and application-service review

- Review state repositories for repeated row decoding, transaction control, timestamp/id handling, and error translation. Extract only rules with identical schema meaning; do not add a generic repository over distinct tables.
- Review queue submission/resume, history, catalogs, and job-file services for repeated path, snapshot, and representation policy. Keep one authority for each safety and compatibility rule.
- Review the completed M15 implementation as one flow from pure fit/gate/search decisions through evidence selection, knob transactions, recovery, scheduling, and CLI presentation. Separate pure calculation from file transaction and background scheduling only where the final code still crosses those responsibilities.
- Preserve byte-equivalent deterministic replay, selected candidate, approval data, knob documents, journals, backups, recovery, and CLI output for fixed inputs.

Exit evidence: any extracted state or timing component owns a complete invariant; transaction and recovery boundaries remain visible; no abstraction unifies merely similar tables or files.

### Slice 9: Front-end and dead-code sweep

- Confirm CLI, TUI, server, and MCP remain translation layers. Move a business decision into services only when more than one front end implements it or the framework currently prevents direct contract testing.
- Retain intentional duplicate clients, version helpers, and transport adapters where sharing would join front ends or make MCP import the application package.
- Search registrations, dynamic framework lookup, string callbacks, protocol methods, serialization keys, migration readers, and tests before removing a command, handler, serializer, widget, client method, compatibility branch, or module.
- Remove dead code together with its obsolete tests and fixtures. Update `architecture.md` after every ownership move, then perform the final package-by-package review and `make check`.

Exit evidence: every package has a recorded keep/change conclusion, every remaining abstraction has a current caller and named responsibility, every M18 candidate has a disposition, and the final deterministic comparisons and `make check` pass.

## Acceptance criteria

- Milestone 15 is done before implementation begins.
- Every package has been reviewed; unchanged areas are left alone because their current responsibility and dependency shape is adequate, not because the audit stopped early.
- All documented and serialized behavior listed in the behavioral boundary remains unchanged. Deterministic job plans, snapshots, manifests, event dictionaries, forecasts, knob files, API bodies, and backend command vectors used by existing golden or characterization tests compare equal.
- Large coordinators have fewer unrelated reasons to change: H3 finishing, job parsing/compilation, chain execution, and queue execution each expose a named workflow whose pure decisions and I/O effects can be tested independently.
- Consumers that need only a worker view or another narrow capability depend on a protocol they own rather than the concrete process-wide implementation; test fakes satisfy those protocols without type suppressions.
- Every deleted production symbol is shown unreachable from supported entry points, framework registration, accepted persisted formats, and reflection or string lookup. Every deleted test is redundant by production path and protected contract, with an equal or stronger retained test identified during the change.
- Test cleanup is performed with its owning production slice. The remaining test layout names contracts clearly, shared fakes and builders have one home, and no deletion relies only on a lower-level or different-transport test.
- No dependency direction is weakened, no lower layer imports a front-end framework, and MCP remains isolated over HTTP.
- No new dependency, configuration key, schema migration, compatibility alias, or generic application framework is added.
- Each refactor lands as a small independently validated change. `make check` passes after every completed slice and at milestone completion.
- Behavior improvements discovered during the work are listed in Milestone 18 with evidence and disposition; none is smuggled into this milestone.

## Decisions for the owner

- **Behavior preservation and deferral** (owner decision, 2026-10-20): M17 changes internal design only. Behavioral changes discovered during it go to M18.
- **Sequence after M15** (owner decision, 2026-10-20): finish M15 before M17, so the timing subsystem is refactored only after its behavior is complete.
- **Comprehensive review, test maintenance first** (owner decision, 2026-10-20): review every package, prioritize removal and simplification of unnecessary tests, and change production structure where that makes contracts easier to maintain.
- **Internal freedom** (owner decision, 2026-10-20): internal types and modules may move, and proven-dead code may be removed, while observable and persisted contracts stay fixed.
- **Evidence instead of numerical gates** (owner decision, 2026-10-20): do not add arbitrary size, complexity, coverage, or test-count thresholds. Existing architecture rules, focused contract evidence, deterministic comparisons, and `make check` are the gates.
- **Small changes** (owner decision, 2026-10-20): implement the comprehensive plan as small independently verified refactors rather than one rewrite.

## Implementation record

Fill this table as M17 is implemented. One slice may use several small changes; it is complete only when its exit evidence holds.

| Slice | Status | Contract evidence | Tests consolidated or removed | M18 findings |
|-------|--------|-------------------|-------------------------------|--------------|
| 0. Baseline and map | planned | | | |
| 1. Test support and capabilities | planned | | | |
| 2. Local simplifications | planned | | | |
| 3. Parsing context | planned | | | |
| 4. Compilation phases | planned | | | |
| 5. H3 finishing | planned | | | |
| 6. Execution state | planned | | | |
| 7. Queue coordination | planned | | | |
| 8. State, timing, and services | planned | | | |
| 9. Front ends and final sweep | planned | | | |
