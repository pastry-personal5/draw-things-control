# Phase 4 changelog

Owner decisions, design decisions, and notable changes for
[Phase 4](README.md). Newest first.

## 2026-10-12

- **Change** [M02]: The backend now prepares its own finishing context and safe settings summary; the shared chain no longer computes Draw Things color anchors. Version-3 queue snapshots verify resolved settings while version 1 and 2 remain readable, and the test backend exercises snapshot recovery. H.264 color correction can fall back to `libx264` when VideoToolbox cannot encode.
- **Change** [M02]: Jobs now name a backend (default `draw-things-cli`), validate backend settings through an adapter, and run backend-prepared commands through the shared chain. Queue snapshots, manifests, execution records, API responses, and resume keep that identity; older records read as Draw Things. A test-only backend checks the second implementation path, while H3 remains unavailable until Milestone 03.
- **Owner decision** [M02, M03]: Jobs use `backend` and `backend_settings`; Milestone 02 ships the Draw Things adapter and proves the interface with a test-only fake backend. H3 arrives in Milestone 03 and rejects `color` initially, until its color checks and correction have a backend-specific design.
- **Design decision** [M02]: The backend owns validation, command preparation, and output finishing; the shared executor owns the chain and supervision. A versioned queue snapshot and explicit backend fields in queue and execution rows preserve identity through recovery and resume; older records default to Draw Things.
- **Owner decision** [M02, M03, M04]: Phase 4 adds a multiple-backend refactor before h3.c support. H3 is the `i2av` backend, uses `--first-frame` for chain handoff and the repo-local `contrib/backends/h3.c/h3` executable, and is available through all existing front ends. Graph-based metadata remains a later milestone.
- **Design decision** [M02, M03]: Keep existing jobs on `draw-things-cli` by default, make backend identity durable for queue and resume, and scope the first H3 integration to first-frame-conditioned MP4 output with generated audio. H3 reference modes use different conditioning and are outside this milestone.
- **Change** [M01]: Milestone 01 is done. Its contextual shortcut help and focus-sensitive status-line hint are covered for the Jobs list, Queue list, Execution History list, and Execution detail; `make check` passes.

## 2026-10-05

- **Owner decision** [M01]: Canonical TUI UX terminology is recorded in [TUI UX terms](../ux-terms.md) before shortcut and status-display planning. It distinguishes the Status pane from the status line and names every main-screen and transient component.
- **Owner decision** [M01]: The terminology is revised around Apple Human Interface Guidelines: it now favors task-oriented, unambiguous names; separates focus from selection; and sets consistent language and scope rules for keyboard shortcuts and feedback. Existing visible titles may change to match the canonical names.
- **Change** [M01]: The TUI now uses the canonical names in its visible titles, Textual classes and IDs, command help, tests, and user guide: Work status, Generation output, Activity log, Jobs list, Queue list, Execution History list, Execution detail, and command field.
- **Change** [M01]: A focused list or Execution detail immediately adds `Press ? for shortcut help` to the status line. `?` opens a centered shortcut help overlay for that focused component; `?` or Escape closes it and restores focus.

## 2026-10-04

- **Owner decision**: Phase 4 is titled “(1) Producing and Consuming Graph-based Metadata (2) Squashed Product Improvement”. No milestones are planned yet.
