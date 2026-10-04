# Phase 3 open issues

Phase 3 remains **in progress**. This document records the work and decisions
that remain after Milestones 01 through 13 were built.

## Fix the TUI test gate

**Status:** fixed in the test fixtures; full gate still in progress

The full `make check` run on 2026-10-04 reported 24 TUI failures. They were
valid tests with stale fixtures: executions dated 2026-09-20 crossed the
default 14-day retention window, so opening the run-mode state store pruned
the rows the tests expected. The fixtures now derive their date from the
current date and stay inside retention. All 208 TUI tests pass in isolation.

The full gate now reaches all tests, but this machine still has three
environment-dependent media failures: Apple Vision cannot load its person
segmentation model, VideoToolbox cannot create an H.264 compression session,
and one ProRes probe does not report the expected matrix metadata. These need
to be rerun with the required Vision and hardware media support before the
full `make check` gate can be called green.

## Color preservation baseline

**Status:** deferred

The planned baseline measurement for the original E0016 chain cannot be
recreated because its clips are no longer available. Milestone 09 therefore
keeps the per-run drift measurements and correction behavior, while the
original baseline remains deferred to a future phase or a new controlled
chain.

## Resolved decisions

### Keep both monitoring transports

**Resolved:** 2026-10-04

gRPC remains the monitoring transport for the TUI and `dtc queue add --wait`.
The API's SSE watch remains the monitoring transport used by MCP. There will
be no migration to SSE and no gRPC retirement in Phase 3.
