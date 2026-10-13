# Phase 4 changelog

Owner decisions, design decisions, and notable changes for
[Phase 4](README.md). Newest first.

## 2026-10-13

- **Change** [M04]: Legacy database migration now escapes project paths as SQLite file URIs, preserving history when a directory name contains URI punctuation such as `?` or `#`.
- **Owner decision** [M04]: Rename the product and app to Framechain, install only `fc` and `framechain`, and keep version `3.0.0`. MCP, caller-header, and gRPC identities change without aliases.
- **Change** [M04]: State now opens `state/framechain.db`. An existing `state/dtc.db`, including committed WAL data, is copied through SQLite backup, verified, and removed only after a safe migration; conflicting files or a held run lock require recovery before startup.
- **Owner decision** [M10]: Move the graph-based metadata milestone from M04 to M10; the milestone document and Phase 4 plan now use M10.
- **Change**: The API creates job files and replacement backups with private permissions. The server refuses linked, non-regular, or malformed token files and reads an existing token from one checked file descriptor.

## 2026-10-12

- **Change** [M03]: H3 is an installed `i2av` backend with a closed job schema, project-owned executable and model paths, first-frame normalization, controlled media-tool environment, staged MP4 promotion, verified final-frame handoffs, and H.264/AAC media validation. Its safe requested/effective timing settings are kept with queue snapshots and run records; local model snapshots are an installation dependency and are rechecked when work starts or resumes.

- **Design decision** [M03]: H3 outputs are generated into a unique sibling staging MP4, then strictly probed, decoded, and atomically promoted before they can be recorded or used as a handoff. The H3 child receives a controlled media-tool environment: inherited `H3_FFMPEG` and `H3_FFPROBE` overrides are rejected, and the resolved FFmpeg/FFprobe pair is placed first on its `PATH`. This prevents partial output and tool-selection drift across H3, preflight, and finishing.
- **Owner decision** [M04]: The graph is a DTC-owned JSON format, available through a read API and an explicit JSON export. The file is produced on request rather than after every execution.
- **Design decision** [M04]: Project a versioned provenance graph from a consistent read of execution history, then use the same schema for the API, CLI export, and offline trace. This avoids a second mutable graph store and event/file synchronization. Imported resume IDs must be remapped or left unresolved, because blindly treating an old manifest ID as a local execution ID could create false provenance.
- **Owner decision** [M03]: The final real H3 first-frame/MP4 acceptance run is performed by a human on the Apple Silicon host; automated tests use fake process and media tools because the Codex execution sandbox does not expose Metal.
- **Owner decision** [M03]: H3's accepted requested duration is 1–15 seconds, inclusive, before the backend applies the CLI's temporal alignment. This supersedes the 4–15-second duration decision below.
- **Owner decision** [M03]: H3's first input and every chain handoff normalize to the requested size by centered crop, rather than padding or stretching.
- **Owner decision** [M03]: H3's accepted requested duration is 4–15 seconds, inclusive, before the backend applies the CLI's temporal alignment.
- **Design decision** [M03]: Jobs require both temporal values and reject disagreement at 24 fps, but H3 commands pass only `--seconds`, because the checked-in CLI rejects `--seconds` and `--frames` together. Version 1 exposes no H3 performance controls.
- **Owner decision** [M03]: An H3 job supplies both `frames` and `seconds`; their requested values must agree at H3's fixed 24 fps before H3 applies its own supported-frame rounding. Requested, effective, and measured temporal values remain distinct in records.
- **Design decision** [M03]: H3 installation paths are project-owned and never job settings; its queue snapshot deliberately captures validated job state but not mutable model weights. H3 uses a closed `backend_settings` schema, normalizes every first-frame handoff under its own media policy, and a run succeeds only after FFprobe verifies and records both required video and audio streams.
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
