# Milestone 04: Rename to Framechain

**Status:** done

## Goal

Name the product Framechain across its installed command, Python package, interfaces, and current documentation.

## Scope and planned changes

- Install `fc` from the `framechain` package at version `3.0.0`; use `python -m framechain` as the module entry point.
- Use Framechain in TUI, CLI, API, and MCP identities. The MCP server is `framechain`, its caller header is `X-Framechain-Caller`, and the gRPC package is `framechain.monitor`.
- Back up `state/dtc.db` into `state/framechain.db` before opening state. Verify the backup, remove the legacy database and sidecars only after verification, and refuse migration when the run lock is held or both names exist.
- Update current documents and examples while preserving archived plans, release notes, earlier changelog entries, Draw Things backend identifiers, job formats, routes, and J/Q/E IDs.

## Acceptance criteria

- `make check` passes, including regenerated gRPC stubs.
- The new command and module entry points, interface identities, and current documentation examples are covered.
- Populated WAL history and queue data survive migration. An active run lock and both database names produce recovery messages. A fresh browse leaves state absent.
