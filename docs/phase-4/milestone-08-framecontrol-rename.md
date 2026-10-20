# Milestone 08: Rename to Framecontrol

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Milestone 04](milestone-04-framechain-rename.md)'s Framechain identities and state migration.

## Goal

Name the product and app Framecontrol throughout the active repository, while keeping `fc` as the sole installed command and version `3.0.0`.

## Scope

In scope:

- Rename the Python package and distribution to `framecontrol`, with `python -m framecontrol` as the module entry point. Update imports, product-named classes and internal markers, packaging and lockfile metadata, generated-stub paths, build configuration, and launchers.
- Use Framecontrol in CLI, TUI, HTTP, and MCP presentation. The MCP server identity becomes `framecontrol`, the caller header becomes `X-Framecontrol-Caller`, and the gRPC package becomes `framecontrol.monitor`. Regenerate the gRPC stubs. Do not accept old public identities as aliases.
- Open `state/framecontrol.db`. On first open, migrate an existing `state/framechain.db` or older `state/dtc.db` with a verified SQLite backup that includes committed WAL data. Remove the source database and sidecars only after verification. Refuse migration while the run lock is held or when more than one database name exists; never choose or overwrite one automatically. A fresh history browse must not create state.
- Update active documentation, examples, and agent instructions. Change the planned graph milestone's schema identity to `framecontrol-provenance-graph` before that format is released.

Out of scope:

- Renaming the local checkout directory or an external repository.
- Compatibility aliases for the old package, MCP identity, caller header, or gRPC package. The database migration is the deliberate exception that preserves local history and queue data.
- Renaming Draw Things backend identifiers, changing job formats or API routes, or changing J/Q/E identifiers and their meanings.
- Rewriting completed plans, research notes, release notes, or past changelog entries. Record this decision in a new changelog entry instead.

## Planned changes

1. Move `src/framechain/` to `src/framecontrol/` and update absolute imports, module entry points, product-named symbols, internal log markers, tests, `pyproject.toml`, `uv.lock`, Makefile paths, generated-stub exclusions, `.gitignore`, and checked-in launchers and MCP configuration. Keep only the `fc` console script and version `3.0.0`.
2. Replace active interface names and text in the CLI, TUI, HTTP API, MCP server, and gRPC proto; regenerate all three client/server stub copies. Replace old caller-header handling in requests, audit paths, and tests without a compatibility alias. Leave routes and payload semantics unchanged.
3. Extend the state filename migration to recognize `framechain.db` and `dtc.db` as legacy sources for `framecontrol.db`. Preserve the existing run-lock, exclusive destination creation, backup integrity and schema checks, and safe cleanup rules. Refuse ambiguous combinations of old and new files with recovery guidance.
4. Update the README, current user guide and architecture, development rules, examples, Phase 4 overview, and the planned graph milestone's identity. Preserve historical documents and add a new Phase 4 changelog entry that supersedes the Milestone 04 name decision.
5. Update identity and migration tests, then run `make check` through completion.

## Acceptance criteria

- The installed distribution is `framecontrol` at version `3.0.0`; `fc` and `python -m framecontrol` work, and no old package or command alias is installed.
- CLI, TUI, HTTP, MCP, and gRPC report Framecontrol identities. New clients use `X-Framecontrol-Caller`; the old header is not accepted as that caller identity. The MCP server is `framecontrol`, and the gRPC package is `framecontrol.monitor`.
- Existing history and queued work in either legacy database name survive a verified migration, including committed WAL transactions. The source and sidecars are removed only after success. An active run lock or conflicting database files yield recovery errors; a fresh browse creates no state.
- Active documentation and examples use Framecontrol. The planned graph milestone specifies `framecontrol-provenance-graph`; historical records retain their original wording.
- `make check` passes, including regenerated gRPC stubs.
