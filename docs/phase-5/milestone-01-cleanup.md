# Milestone 01: Cleanup and canonical H3 terms

**Phase:** [Phase 5](README.md)
**Status:** planned
**Depends on:** Phase 4 [Milestone 08](../phase-4/milestone-08-framecontrol-rename.md)'s Framecontrol identities and state migration, [Milestone 09](../phase-4/milestone-09-job-definition.md)'s version-2 per-run job definitions, and Milestones [10](../phase-4/milestone-10-stage-timing.md), [14](../phase-4/milestone-14-timing-knobs.md), and [15](../phase-4/milestone-15-timing-gate-and-search.md)'s timing records and calibration files.

## Goal

Make active Framecontrol terminology consistent before later Phase 5 work: use the established project and protocol identities, give H3 pipelines canonical names, and preserve usable state across the rename.

## Scope

In scope:

- `frame-control` for repository and directory naming; `Framecontrol` in product prose; and `framecontrol` for the package, `fc` command, HTTP API, gRPC namespace, caller header, and MCP server name.
- Canonical H3 version-2 pipeline names `i2va` and `ref2va`, replacing `i2av` and `r2av` throughout active job definitions, code, serialized plans, timing groups, interfaces, and documentation.
- `t2va` as reserved H3 vocabulary. It is not an accepted pipeline until [Milestone 02](milestone-02-h3-t2va.md).
- Valid version-2 replacements for every checked-in legacy version-1 H3 job definition, preserving each job's generation intent.
- A forward-only migration of persisted canonical pipeline fields: state rows, version-2 queue snapshots and their duplicated columns, timing rows and workloads, and active timing calibration/pin data. Queued legacy version-1 snapshots remain readable through their explicitly versioned historical decoder, so their exact snapshot evidence and resume behavior are not changed merely to rename a version-2 vocabulary.

Out of scope:

- H3 text-only generation or any other new generation behavior; Milestone 02 introduces `t2va`.
- Compatibility aliases for `i2av` or `r2av` in new job definitions.
- Rewriting a legacy version-1 queue snapshot, manifest, log, imported history payload, or timing-file backup just to remove a historical spelling. The reader and a restore path, not a blind rewrite of preserved evidence, provide compatibility where it is needed.
- Changes to the `framecontrol` protocol identity, package, command, HTTP routes, caller header, gRPC namespace, or MCP server name: they are already canonical.
- Rewriting completed plans, release notes, research, archived documents, LICENSE attribution, or earlier changelog entries.

## Contract

- New version-2 H3 jobs accept only `i2va` and `ref2va`. Validation rejects `i2av`, `r2av`, and `t2va`; the `t2va` error identifies it as reserved for Milestone 02 rather than treating it as an unknown spelling.
- `i2va` retains current first-frame-conditioned behavior. `ref2va` retains current reference-conditioned behavior, requires `reference_pair`, begins an independent segment, and does not receive color correction.
- The database migration maps persisted version-2 `i2av` to `i2va`, `r2av` to `ref2va`, and legacy execution-level `h3av` to `mixed`. It updates every duplicated version-2 queue field and its frozen effective plan in one transaction, preserving the queue reader's equality checks and resume behavior. It does not reinterpret a version-1 snapshot as version 2.
- Before any timing migration, recover or refuse an unfinished timing-file transaction while holding the existing timing-file lock. Rename H3 timing keys, ring entries, active calibration entries, pins, and retained summaries in one recoverable transaction. Preserve old backup bytes as historical recovery evidence; a rollback that selects one is translated and validated before it becomes active, so rollback cannot reintroduce obsolete pipeline keys. New records serialize only canonical terms.

## Planned changes

1. Add shared canonical-name constants and update H3 parsing, compilation, command preparation, finishing, queue/resume handling, events, history, timing, forecasts, CLI/TUI text, HTTP/gRPC serializers, MCP capability output, and tests. A version-2 parse rejects `i2av`, `r2av`, and reserved `t2va`; the historical version-1 and queue-snapshot readers remain deliberately version-gated rather than becoming aliases for a new definition.
2. Upgrade the checked-in legacy H3 examples to version 2, rename H3 example paths and job names containing obsolete pipeline terms, and update links, example discovery, and validation coverage. Update the active Phase 5 plans now, before Milestone 02 work begins, so their planned contracts consistently say `t2va`, `i2va`, and `ref2va`.
3. Add a new database schema migration. Transform typed SQL columns plus the duplicated version-2 queue `job_text`, snapshot, effective plan, settings, reference descriptors, and any structured JSON workload fields as applicable; reject malformed or internally inconsistent payloads before committing. Do not use global text replacement. The migration is idempotent, runs before a worker can claim work, and rolls back the complete database transaction on any refusal.
4. Migrate timing artifacts through `KnobFiles`' existing lock, recovery journal, atomic replacement, validation, and backup rules. Include active backend calibration, ring data, approved/validated summaries, pin paths, and journal-aware recovery; prove interruption at each transaction boundary leaves either the wholly old or wholly canonical usable set. Keep historical backup bytes recoverable and canonicalize them only on restore.
5. Audit active user-facing product wording. Confirm that the control API and MCP server retain their already-canonical `framecontrol` protocol identity and contain no `draw-things-control` spelling; do not create a no-op rename or an alias.
6. Update the Phase 5 overview, user documentation, architecture, API/MCP examples, and changelog when the behavior lands. Completed plans, release notes, research, archives, LICENSE attribution, and earlier changelog entries remain historical.

## Acceptance criteria

- `i2va` and `ref2va` jobs validate, queue, resume, execute, record, forecast, and display with unchanged generation semantics. `i2av`, `r2av`, and premature `t2va` definitions fail before a child starts.
- Every checked-in job definition validates as version 2 where applicable, uses canonical H3 pipeline names, and every renamed example is reachable from the current documentation and tests.
- A pre-migration state store containing version-2 queued H3 work, execution/run history, timing rows, and calibration/pin data opens successfully with canonical persisted fields. Resumed version-2 entries preserve their snapshot comparison and reference behavior; an old version-1 snapshot remains resumable only through its declared historical reader.
- A malformed or mismatched persisted payload refuses safely, with no partially migrated database or timing artifact set. Reopening after a successful migration is a no-op.
- New version-2 queue snapshots, history rows, capabilities, forecasts, API/MCP results, and active timing artifacts contain no `i2av` or `r2av` value. Historical version-1 evidence and immutable backup bytes are the stated exception, never emitted as a new canonical record.
- Active user-facing project references contain no `draw-things-control`; protocol/package/command identities remain `framecontrol`/`fc`; completed historical records remain unchanged.
- Targeted migration, parser, example, queue/resume, timing, API/MCP, and documentation tests pass, followed by `make check` when implementation lands.
