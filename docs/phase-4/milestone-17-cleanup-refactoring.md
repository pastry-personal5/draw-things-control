# Milestone 17: Comprehensive cleanup and refactoring

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Milestone 15](milestone-15-timing-gate-and-search.md), completed 2026-10-20. The cleanup starts from the final timing gate and search design rather than an earlier in-progress shape.

## Goal

Make the whole codebase easier to understand, test, and change without changing observable behavior. Test maintenance is the first priority: keep tests that protect distinct contracts, remove tests that repeat the same contract through the same production path, and make the remaining suite express behavior instead of implementation structure.

The milestone applies clean-code and clean-architecture principles pragmatically. Responsibilities become cohesive, orchestration is separated from pure decisions and I/O adapters, dependencies remain explicit, and interfaces are defined at the boundary that consumes them. It does not add layers, abstractions, or indirection without a demonstrated maintenance benefit.

## Completion (2026-10-20)

The comprehensive review is complete. Test support now uses one honest typed queue-worker fake; the existing TUI table base owns shared focus and leave behavior; parser state is grouped by owner and lifetime; version-2 compilation exposes its phases; H3 finishing has a backend-owned staged workflow; and per-execution observer, log, and timing state no longer lives directly on the process-lifetime executor. Queue coordination, state persistence, timing, application services, and the remaining front-end boundaries were reviewed and retained where extraction would split an invariant or add indirection. No behavior-change candidate was found for Milestone 18, and final `make check` passed.

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

Milestone 15's completion revision passed `make check`. Slice 0 establishes a fresh baseline from the revision on which M17 implementation actually begins, so changes after M15 are included rather than assumed safe from the earlier result.

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
- reading or refactoring the vendored Draw Things executable, virtual environments, caches, or generated gRPC stubs; review their checked-in source definitions and generation boundary instead;
- modifying the protected user configuration and parameter files.

## Refactoring rules

Each change is small and independently reviewable:

1. Name the production responsibility or test contract being improved.
2. Identify the observable behavior and the smallest existing tests that protect it. Add a characterization test only when the behavior is otherwise unprotected.
3. Move or simplify one seam. Do not combine the move with an intentional behavior correction.
4. Delete dead code and redundant tests only after their callers and contract coverage have been checked.
5. Treat the pre-refactor contract evidence as fixed. Do not update an expected value, golden file, or characterization result merely to make the refactor pass; first show that the expectation covered implementation structure rather than an observable contract.
6. Compare deterministic outputs before and after the slice when it touches serialization, commands, events, manifests, timing, or media facts.
7. Run the narrow affected tests, then `make check` before the next slice.
8. Update `architecture.md` in the same slice when module ownership changes.
9. Record a discovered behavior-change candidate in Milestone 18 with evidence and continue only if the refactor can preserve the current behavior. Candidate triage may happen during M17, but an accepted behavior change is not implemented until M17 is done.

A helper is extracted only when it names one responsibility, reduces repeated policy, narrows a dependency, or makes a contract directly testable. Similar-looking code remains separate when its ownership, failure boundary, or dependency direction differs.

Stop a proposed extraction when it moves code without reducing the caller's knowledge, creates a one-method wrapper with no boundary or invariant, makes execution order harder to see, or requires more test seams than the responsibility warrants. Leave the current code and record the review as complete instead.

## Planned changes

The review is comprehensive; the edits are selective. Each slice below is completed and validated before the next starts. Test organization and deletion happen inside the slice whose production contract they protect, not as a suite-wide deletion pass that weakens the safety net before refactoring.

The slices provide this package coverage. Slice 0 records the exact tests and supported entry points for each row; the final sweep records a keep or change conclusion for every package even when an earlier slice made no edit.

| Area | Primary slices | Review focus |
|------|----------------|--------------|
| `core` | 0, 2, 11 | Process and resource boundaries, path and configuration policy, shared primitives, dead private support |
| `jobs` | 0, 3–6, 9, 11 | Parsing, compilation, backend finishing, execution state, media and timing decisions |
| `state` | 0, 8, 11 | Schema-specific decoding, transactions, migrations, compatibility readers, retention |
| `services` | 0, 1, 7, 9–11 | Process ownership, queue coordination, timing orchestration, shared application rules |
| `cli` | 0, 1, 2, 9–11 | Composition, command registration, translation, timing presentation |
| `tui` | 0–2, 10, 11 | Composition, pane behavior, service boundaries, framework-discovered handlers |
| `server` | 0, 1, 10, 11 | Context capabilities, routes, SSE and gRPC translation, framework registration |
| `mcp_server` | 0, 1, 10, 11 | HTTP-only isolation, tool/resource registration, client and transport translation |

### Slice 0: Stable baseline and contract map

- Confirm M15 is done, identify the exact baseline revision, inventory unrelated worktree changes without discarding them, and record one passing `make check` from an isolated M17 baseline.
- Inventory the supported entry points and persisted readers: console commands, Typer registration, Textual bindings and handlers, FastAPI routes, gRPC methods, MCP tools, queue snapshot versions, database migrations, manifest/history import, backend registry hooks, and string-dispatched callbacks.
- For each later slice, list the owning tests and classify them as pure decision, adapter, integration, transport, process, concurrency, recovery, or characterization coverage in the implementation record. This is a working record for the milestone, not a new permanent documentation system.
- Add a focused characterization assertion only where existing tests do not already pin a deterministic contract: compiled plan, redacted command, event dictionaries, manifest, API serialization, timing replay, knob transaction, or media facts. Avoid opaque snapshots that make intentional fields indistinguishable from incidental representation.

Exit evidence: current `make check` passes, every planned slice names its contract tests, and no production code has changed.

### Slice 1: Test support and consumer capabilities

- Start with the repeated server worker fakes and their `ServerContext` type suppressions. Inventory the capabilities health, queue routes, entry snapshots, execution deletion, SSE, and gRPC actually consume.
- Introduce a production protocol only for a stable application capability used by real consumers. If the only problem is incomplete test doubles, prefer one complete typed fake in `tests/fixtures.py`; do not reshape production solely to satisfy Pyright.
- Review `Toolkit`, `ServerContext`, CLI context, and TUI construction for hidden ownership or test-only mutation. Use constructor injection or a small composition factory only where it removes a real alternate construction path.
- Remove duplicated fake setup, unused fake methods, and the associated type suppressions once every consumer uses a complete and honest seam.

Exit evidence: affected server tests, including MCP tests that host the real server application, use honest typed test support without an unexplained `QueueWorker` suppression. A production protocol exists only if a real consumer owns that narrow capability; process-lifetime ownership remains explicit and API behavior is unchanged.

### Slice 2: Small local simplifications

- Review repeated focus and leave handlers in the TUI, repeated local serialization guards, and trivial pass-through methods. Consolidate them only into an existing natural base or helper whose name describes the behavior; do not introduce a mixin or inheritance hierarchy for a one-line saving.
- Remove unused imports, unreachable private helpers, narration comments, and stale aliases proven unnecessary by Slice 0's entry-point inventory.
- Merge table-driven test cases with `subTest` only when they execute the same production path and differ solely in input and expected output.

Exit evidence: each change is local to one package, visible TUI behavior and framework discovery are covered unchanged, and the total concept count falls rather than moving code to a new file.

### Slice 3: Job parsing context

- Classify `JobParser`'s constructor inputs by owner and lifetime: source and filesystem policy, frozen snapshot/reference data, and the internal version-2 run view. Replace the broad argument list with cohesive immutable context values only where that removes repeated forwarding through `load_job_text` and `nested`; keep `load_job` and `load_job_text` as the public entry points.
- Keep parsing and error translation in `JobParser`; do not create a class for each field. Extract a validator only when both legacy and version-2 paths currently encode the same rule.
- Preserve exact error type, path, field, and message, including filename enforcement, path confinement, missing resume inputs, reference snapshots, legacy frames, and frozen base configurations.
- Review `test_parsing.py`, `test_compile_v2.py`, examples, queue snapshot, and resume tests together. Consolidate only cases that reach the same parser mode and validation branch; legacy, file-read, text-snapshot, and run-view tests are distinct boundaries.

Exit evidence: parser call sites no longer forward unrelated option groups independently, no context merely renames the full old argument list, no policy is duplicated between entry points, and all error and compiled-job characterization remains identical.

### Slice 4: Version-2 compilation phases

- Make `RunCompiler`'s existing phases explicit: catalog validation, defaults and selection, resolved run construction, backend view parsing, cross-run edge validation, and root-plan assembly.
- Prefer typed intermediate values and pure functions within `compile_v2.py` first. Split a module only if a group has its own inputs, outputs, invariant, and tests; do not convert each private method into a class.
- Keep backend adapters as the sole authority for backend settings and keep `JobParser` as the sole error-context translator. The compiler must not grow a second H3 or Draw Things validator.
- Remove temporary compatibility or remapping helpers only after accepted snapshot versions and exact error fields prove them unreachable.

Exit evidence: `compile()` reads as those phases in order, a run is still parsed once by its adapter, and every existing effective-plan and edge-validation result is equal.

### Slice 5: H3 finishing workflow

- Keep `H3Backend` as the `JobBackend` facade. Move the 207-line `finish` workflow into a backend-owned finishing module with explicit request, staged-artifact state, and result values; keep it near the H3 adapter rather than in backend-neutral media code. Do not introduce a finisher class if functions and one ownership value express the lifecycle more clearly.
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

Exit evidence: the claim loop and its two entry workflows are readable without crossing lock ownership; extraction occurs only if it improves that boundary. Each mutable flag has one owner under one documented lock, and race, recovery, forecast, and cooldown behavior is unchanged.

### Slice 8: State persistence and compatibility

- Review state repositories for repeated row decoding, transaction control, timestamp/id handling, and error translation. Extract only rules with identical schema meaning; do not add a generic repository over distinct tables.
- Trace every accepted schema and queue-snapshot version through its reader before removing a decoder, default, remapping branch, or migration. Repository-internal cleanup must not narrow the persisted compatibility boundary.
- Keep transaction begin/commit/rollback and destructive retention ownership visible. Do not move a multi-table invariant behind a generic CRUD helper.

Exit evidence: every retained compatibility branch names the persisted version or migration that needs it, every removed branch is unreachable from accepted data, transaction boundaries remain visible, and fixed database and queue fixtures read identically.

### Slice 9: Timing pipeline

- Review the completed M15 implementation as one flow from pure fit, gate, and search decisions through evidence selection, knob transactions, recovery, scheduling, and CLI presentation.
- Keep `jobs/timing/` pure. Separate calculation from file transaction and background scheduling only where the final code crosses those responsibilities or duplicates one decision in `services`.
- Preserve byte-equivalent deterministic replay, selected candidate, approval provenance, knob documents, journals, backups, recovery, cancellation and CPU budgets, and CLI output for fixed inputs.

Exit evidence: every timing component has one calculation, persistence, or scheduling responsibility; recovery and approval transactions remain visible; focused deterministic replays and CPU-budget checks pass unchanged.

### Slice 10: Application-service boundaries

- Review queue submission/resume, history, catalogs, input listing, job-file operations, generation sessions, and deletion for repeated path, snapshot, representation, authorization, and error-translation policy. Keep one authority for each safety and compatibility rule.
- Check each front end's direct decisions against the service layer. Move a rule only when it is application behavior shared by consumers, not presentation or transport policy that properly differs.
- Review process-lifetime construction in `Toolkit`, `QueueHost`, `ServerContext`, CLI services, and the TUI composition root after the earlier slices; remove alternate ownership paths that exist only for tests.

Exit evidence: each shared application rule has one owner, front-end-specific translation remains local, and lifetime and cleanup ownership can be followed from each composition root without a service locator or framework dependency.

### Slice 11: Front-end and dead-code sweep

- Confirm CLI, TUI, server, and MCP remain translation layers. Move a business decision into services only when more than one front end implements it or the framework currently prevents direct contract testing.
- Retain intentional duplicate clients, version helpers, and transport adapters where sharing would join front ends or make MCP import the application package.
- Search registrations, dynamic framework lookup, string callbacks, protocol methods, serialization keys, migration readers, and tests before removing a command, handler, serializer, widget, client method, compatibility branch, or module.
- Remove dead code together with its obsolete tests and fixtures. Update `architecture.md` after every ownership move, then perform the final package-by-package review and `make check`.

Exit evidence: every package has a recorded keep/change conclusion, every remaining abstraction has a current caller and named responsibility, every M18 candidate has an owner disposition, and the final deterministic comparisons and `make check` pass.

## Acceptance criteria

- Milestone 15 was completed and verified before implementation began.
- Every package has been reviewed; unchanged areas are left alone because their current responsibility and dependency shape is adequate, not because the audit stopped early.
- All documented and serialized behavior listed in the behavioral boundary remains unchanged. Deterministic job plans, snapshots, manifests, event dictionaries, forecasts, knob files, API bodies, and backend command vectors used by existing golden or characterization tests compare equal.
- Every confirmed responsibility concentration has fewer unrelated reasons to change: H3 finishing, job parsing/compilation, chain execution, and queue execution either expose a clearer named workflow whose decisions and effects can be tested at their owning boundary, or have a recorded keep conclusion showing why extraction would add indirection without reducing knowledge.
- A consumer that genuinely owns a narrow worker or application capability depends on a protocol defined at that consumer boundary. Where `ServerContext` intentionally owns the complete process-wide worker, tests instead use one complete typed fake; no production abstraction exists solely to satisfy a test double.
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

Implementation began from revision `8d482ce46b505bdcf6a48bef18cbd248a6877665` on 2026-10-20 with a clean worktree. Milestone 15 is present in its completed revision `c2b7ab6`. The baseline `make check` passed unchanged; its functional suite covered 2,147 tests, 2 skips, and 921 subtests. A first sandboxed run also established that the 59 socket-binding failures were environment restrictions, not product failures; the authorized run of the identical command passed.

The supported-entry inventory used for dead-code decisions is:

- the `fc` console entry point and Typer's root commands, `queue`, `history`, `timing`, and `timing knobs` command groups in `cli/`;
- Textual `BINDINGS`, `action_*`, `on_*`, file-watch callbacks, and the command registry in `tui/`;
- every `/v1` route registered by `server/app.py`, the authenticated OpenAPI route, and the `WatchEvents` and `WatchQueueEntry` gRPC methods;
- the MCP server's schema-backed tool registry and its stdio and HTTP transports in `mcp_server/`;
- SQLite migrations through `state/schema.py`, queue snapshot readers for legacy row snapshots and submitted snapshot versions 1, 3, 4, 6, and 8, history-manifest import, timing import and knob-schema readers;
- `BackendRegistry` construction and optional backend hooks reached by planning, parsing, launching, finishing, and timing; and framework/string-dispatched callbacks found by the CLI, Textual, FastAPI, gRPC, and MCP runtimes.

The contract map for the remaining slices is fixed as follows. A listed suite may protect more than one boundary; deletion requires a same-path, same-contract retained case inside that slice.

| Slice | Primary contract tests | Coverage classification |
|-------|------------------------|-------------------------|
| 1 | `tests/server/test_*`, `tests/mcp_server/test_*`, `tests/cli/test_queue_wait_grpc.py` | adapter, transport, process, concurrency |
| 2 | `tests/tui/test_*`, package-local `core`, CLI, and server tests for any simplified helper | adapter, integration, framework characterization |
| 3 | `tests/jobs/test_parsing.py`, `test_compile_v2.py`, `test_job_characterization.py`, queue snapshot/resume tests | pure decision, adapter, integration, recovery, characterization |
| 4 | `tests/jobs/test_compile_v2.py`, parser and backend argument tests, queue snapshot tests | pure decision, adapter, characterization |
| 5 | `tests/jobs/test_h3_finish.py`, H3 inspection/color and media-check tests, `test_mixed_chain.py`, `test_executor_timing.py` | pure decision, adapter, integration, characterization |
| 6 | `tests/jobs/test_executor.py`, `test_executor_timing.py`, `test_job_characterization.py`, `test_mixed_chain.py`, `test_color_executor.py` | integration, concurrency, recovery, characterization |
| 7 | `tests/services/test_queue_worker.py`, queue park/cancel/forecast/recovery tests, server queue/SSE/gRPC tests | integration, transport, concurrency, recovery |
| 8 | `tests/state/test_*`, queue snapshot/resume tests, history and timing import tests | adapter, integration, recovery, characterization |
| 9 | `tests/jobs/timing/test_*`, timing service, knob, replay, CLI timing, and CPU-budget tests | pure decision, adapter, integration, recovery, characterization |
| 10 | service tests plus corresponding CLI, TUI, server, and MCP consumer tests | adapter, integration, transport, process |
| 11 | architecture tests, all front-end suites, deterministic characterization suites, and full `make check` | adapter, integration, transport, process, concurrency, recovery, characterization |

Fill this table as M17 is implemented. One slice may use several small changes; it is complete only when its exit evidence holds.

| Slice | Status | Review conclusion and contract evidence | Tests consolidated or removed | M18 candidate IDs |
|-------|--------|-----------------------------------------|-------------------------------|-------------------|
| 0. Baseline and map | done | Clean revision and passing baseline recorded above; supported entry points, persisted readers, dynamic registrations, and owning contract suites inventoried. Existing deterministic coverage is sufficient; no characterization assertion was missing. | None | None |
| 1. Test support and capabilities | done | `ServerContext` intentionally retains its complete process-wide `QueueWorker`; one typed complete fake now serves HTTP, SSE, gRPC, and MCP tests without a production-only protocol. The 303 focused tests (348 subtests) and full `make check` passed. | Four partial worker fakes consolidated; no contract test deleted. | None |
| 2. Local simplifications | done | The existing `SidewaysTable` now owns the identical focus-status refresh and command-line leave actions of all three table panes; Textual handler names and bindings are unchanged. All 247 TUI tests (117 subtests) and full `make check` passed. | None | None |
| 3. Parsing context | done | Parser source/filesystem policy, caller policy, frozen queue data, and a nested run view now have separate immutable ownership values. The parser, compiler, characterization, submission, and resume tests (130 tests, 101 subtests) and full `make check` passed with exact errors and plans unchanged. | None | None |
| 4. Compilation phases | done | `compile()` now names catalog validation, defaults and selection, root options, resolved-run construction, cross-run edge validation, and root assembly in order. `_Catalogs` and `_RootOptions` carry phase outputs; adapter parsing remains cached once per distinct view. Focused compiler/backend/queue tests and full `make check` passed. | None | None |
| 5. H3 finishing | done | `H3Backend.finish` delegates to `h3_finish.py`; an explicit request, staged-artifact owner, and finish state separate verification, color interpretation, delivery, correction, handoff, promotion, recording, reporting, and cleanup. All 94 focused finishing, color, inspection, mixed-chain, timing, and media tests (8 subtests) passed; final full validation is recorded in Slice 11. | Test patch points moved to the owning finishing module; no contract test deleted. | None |
| 6. Execution state | done | Observer, log writer, current timing, timing-read failure, pre-start spans, and child-start callback now belong to one per-run execution session passed with the chain; only `CancelToken` remains cross-thread process state. The 129 executor, timing, characterization, mixed-chain, and color tests (28 subtests) passed; final full validation is recorded in Slice 11. | None | None |
| 7. Queue coordination | done | Keep: the claim loop already delegates job and generation workflows to named methods, while their finish, hold, park, forecast, and wait transitions share `_state_lock` ordering. A new object would split lock-coupled ownership or merely wrap these methods. Queue, race, recovery, forecast, route, SSE, and gRPC coverage remains at its existing boundaries. | None; apparent executor/worker park and cancel overlaps protect different state owners. | None |
| 8. State persistence | done | Keep: `Database.transaction` is the single explicit begin/commit/rollback owner; row decoders belong to their schema-specific values and preserve missing-column compatibility; multi-table delete, recovery, retention, and import transactions remain visible. Snapshot readers for versions 1–8 and every schema migration were retained. | None; store, migration, queue, import, recorder, and timing-store cases protect distinct persisted boundaries. | None |
| 9. Timing pipeline | done | Keep: `jobs/timing/` remains pure calculation; `timing_knobs`, `timing_knob_files`, `timing_replay`, and `knob_updater` separately own interpretation, transactions/recovery, replay, and scheduling. No calculation or approval decision was duplicated across those boundaries. | None; pure model, persistence/recovery, service, CLI, deterministic replay, and CPU-budget cases remain distinct. | None |
| 10. Application services | done | Keep: queue submission/resume, catalogs, history, input listing, job files, generation, and deletion each have one service owner. Front-end imports of job/state values are representation, ID parsing, serialization, or composition—not duplicated application policy—and `Toolkit`, `QueueHost`, `ServerContext`, CLI, and TUI lifetimes remain explicit. | None | None |
| 11. Front ends and final sweep | done | CLI, TUI, server, and MCP registrations and string/framework callbacks were rechecked; MCP still imports no application front end and uses HTTP only. No unreferenced top-level private production function was found. Package conclusions are recorded below. Architecture tests and final `make check` passed, including deterministic characterizations and CPU-budget gates. | No test deleted: apparent overlaps retained where they cross adapter, process, transport, concurrency, or recovery boundaries. | None |

### Final package review

| Package | Conclusion |
|---------|------------|
| `core` | Keep. Paths, configuration, process supervision, signals, and shared primitives already have cohesive modules and explicit injected paths; the entry-point and private-symbol sweep found no safe removal. |
| `jobs` | Changed the four confirmed concentrations: parser context, compiler phases, H3 finishing, and execution-session ownership. Backend-neutral media, records, inputs, events, and pure timing modules remain cohesive and keep their separate failure and policy boundaries. |
| `state` | Keep. Table-specific decoding and transactions preserve migration and persisted compatibility more clearly than a generic repository would. |
| `services` | Test support was consolidated; queue coordination, timing orchestration, and application use cases retain their current explicit owners after review. |
| `cli` | Keep as command registration, composition, and text/error translation. Its direct store imports are the two explicit import/maintenance commands, not shared application policy. |
| `tui` | Shared table-pane focus and leave behavior moved to the existing base; other repeated-looking handlers carry pane-specific shortcuts, focus, or selection behavior and remain local. |
| `server` | Production remains a transport/composition boundary. `ServerContext` intentionally owns the full worker; tests now use one honest typed fake instead of partial doubles or a test-driven production protocol. |
| `mcp_server` | Keep. Its schema-backed tools, wait logic, and stdio/HTTP transports remain isolated clients of the HTTP API and do not import `services`, `server`, `jobs`, or `state`. |
