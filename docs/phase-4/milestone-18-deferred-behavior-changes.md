# Milestone 18: Deferred behavior changes from refactoring

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Milestone 17](milestone-17-cleanup-refactoring.md) (done), which preserved current behavior and would have recorded candidates here instead of changing them during refactoring.

## Completion (2026-10-20)

Closed with no code or behavior change. Milestone 17 completed all planned review slices and recorded `None` in every row of its M18 candidate column. Its final package review found no product, compatibility, safety-policy, or error-handling change that needed M18 triage, and its final `make check` passed. There was therefore no candidate to accept, reject, move, implement, migrate, document, or validate separately.

## Goal

Evaluate and, only after explicit owner approval, implement behavioral changes discovered while cleaning up and refactoring the codebase. This keeps Milestone 17 reviewable as a structural change and gives each product change its own rationale, compatibility assessment, tests, documentation, and changelog entry.

## Candidate outcome

None. Milestone 17's implementation record is the authoritative slice-by-slice evidence: every candidate entry is `None`, and its completion summary confirms that no behavior-change candidate was found. The milestone therefore followed the planned no-candidate path below and did not invent unrelated work.

Had a candidate been found, it would have received a stable ID (`M18-C01`, `M18-C02`, and so on), its source M17 slice, and one status: `pending`, `accepted`, `rejected`, `moved`, or `implemented`. A pending candidate could have remained while later independent M17 slices proceeded, but it would have blocked M17 completion.

Each candidate must state:

- the current observed behavior and a reproducible example;
- why it may be incorrect, unsafe, inconsistent, or unnecessarily difficult for a user;
- affected CLI, TUI, HTTP, gRPC, MCP, job, snapshot, database, event, file, media, timing, or concurrency contracts;
- compatibility and migration consequences;
- the smallest proposed change and the alternative of preserving current behavior;
- tests and documentation that would change;
- the owner's decision and date: accept, reject, or move to another phase or milestone; an accepted candidate would have remained `accepted` until implemented after M17.

## Scope

The conditional scope was accepted behavior corrections discovered in Milestone 17 and their tests, compatibility handling, documentation, and changelog entries. No candidate entered that scope.

Out of scope: structural cleanup already owned by Milestone 17; unrelated feature requests; automatically treating cleaner code, a simpler test, or a surprising behavior as authority to change a public contract.

## Planned changes

1. Triage every candidate after Milestone 17's relevant refactoring slice identifies it. This disposition work may happen during M17.
2. Group only candidates whose behavior, compatibility boundary, and validation are inseparable; otherwise implement them as independent small changes.
3. Write the accepted behavior and compatibility plan into this document before implementation.
4. After M17 is done, add or change tests to state the new contract, implement the smallest behavior change, and update user and architecture documentation where applicable.
5. Record each owner or design decision and each notable behavior change in the Phase 4 changelog.
6. Run focused validation and `make check` for each accepted change.

Milestone 17 finished with no candidates, so this milestone is done with no code change; no work was invented to fill it.

## Acceptance criteria

- Milestone 17 completed with no candidate pending or omitted: its implementation record and completion summary both record that none was found.
- No behavior, compatibility boundary, persisted format, test contract, or user or architecture documentation changed under M18.
- The no-candidate disposition is recorded in this document, the Phase 4 index, and the Phase 4 changelog.
- Milestone 17's final `make check` passed; M18 requires no separate product validation because it contains no product change.

## Decisions for the owner

- **Separate behavior from refactoring** (owner decision, 2026-10-20): behavioral changes found during M17 are deferred to M18 so the comprehensive cleanup remains behavior-preserving.
