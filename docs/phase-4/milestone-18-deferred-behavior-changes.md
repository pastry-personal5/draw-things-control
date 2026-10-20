# Milestone 18: Deferred behavior changes from refactoring

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** Findings from [Milestone 17](milestone-17-cleanup-refactoring.md), which preserves current behavior and records candidates here instead of changing them. Candidate triage may occur while M17 is in progress; implementation of an accepted candidate waits until M17 is done.

## Goal

Evaluate and, only after explicit owner approval, implement behavioral changes discovered while cleaning up and refactoring the codebase. This keeps Milestone 17 reviewable as a structural change and gives each product change its own rationale, compatibility assessment, tests, documentation, and changelog entry.

## Candidate changes

None yet. Milestone 17 records a candidate here before proceeding with any refactor that exposes a possible behavior change. Give each candidate a stable ID (`M18-C01`, `M18-C02`, and so on), its source M17 slice, and one status: `pending`, `accepted`, `rejected`, `moved`, or `implemented`. A pending candidate may remain while later M17 slices proceed if they do not depend on it, but it blocks M17 completion.

Each candidate must state:

- the current observed behavior and a reproducible example;
- why it may be incorrect, unsafe, inconsistent, or unnecessarily difficult for a user;
- affected CLI, TUI, HTTP, gRPC, MCP, job, snapshot, database, event, file, media, timing, or concurrency contracts;
- compatibility and migration consequences;
- the smallest proposed change and the alternative of preserving current behavior;
- tests and documentation that would change;
- the owner's decision and date: accept, reject, or move to another phase or milestone; accepted candidates remain `accepted` until implemented after M17.

## Scope

In scope only after candidates exist and the owner approves them: the accepted behavior corrections discovered in Milestone 17 and their tests, compatibility handling, documentation, and changelog entries.

Out of scope: structural cleanup already owned by Milestone 17; unrelated feature requests; automatically treating cleaner code, a simpler test, or a surprising behavior as authority to change a public contract.

## Planned changes

1. Triage every candidate after Milestone 17's relevant refactoring slice identifies it. This disposition work may happen during M17.
2. Group only candidates whose behavior, compatibility boundary, and validation are inseparable; otherwise implement them as independent small changes.
3. Write the accepted behavior and compatibility plan into this document before implementation.
4. After M17 is done, add or change tests to state the new contract, implement the smallest behavior change, and update user and architecture documentation where applicable.
5. Record each owner or design decision and each notable behavior change in the Phase 4 changelog.
6. Run focused validation and `make check` for each accepted change.

If Milestone 17 finishes with no accepted candidates, mark this milestone done with no code change and record that disposition; do not invent work to fill it.

## Acceptance criteria

- Every candidate discovered in Milestone 17 has evidence and an explicit owner disposition.
- Rejected candidates leave behavior unchanged. Moved candidates link to their destination and are not implemented here.
- Every accepted candidate has a specified compatibility boundary and focused acceptance criteria added to this document before implementation.
- Accepted changes include tests at the owning boundary, any required migration or backward-compatibility handling, current user and architecture documentation, and a new changelog entry.
- Changes remain small and independently validated, and `make check` passes.

## Decisions for the owner

- **Separate behavior from refactoring** (owner decision, 2026-10-20): behavioral changes found during M17 are deferred to M18 so the comprehensive cleanup remains behavior-preserving.
