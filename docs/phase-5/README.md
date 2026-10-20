# Phase 5: Graph-based metadata

**Status:** planned

## Goal

Produce and consume graph-based metadata that records trustworthy provenance for generated artifacts.

## Scope

- Export, read, and trace provenance graphs in Milestone 08.

## Non-goals

- Replacing SQLite, manifests, or queue snapshots with a graph database.

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 08 | [Graph-based metadata](milestone-08-graph-metadata.md) | planned |

## Depends on

- [Phase 4](../phase-4/README.md) Milestones 02, 03, 05, 06, and 09, which provide the durable backend, media, reference, artifact-role, and per-run pipeline facts that Milestone 08 projects.

## Changelog

Decisions and notable changes are recorded in [phase-5-changelog.md](phase-5-changelog.md).

## Exit criteria

- Graph-based metadata is produced, consumed, and documented as defined in Milestone 08.
