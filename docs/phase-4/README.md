# Phase 4: (1) Producing and Consuming Graph-based Metadata (2) Squashed Product Improvement

**Status:** in-progress

## Goal

Improve the product experience, make chained generation support multiple backends, add H3 image-to-audio-video generation, and produce and consume graph-based metadata.

## Scope

- TUI terminology, shortcuts, and status feedback in Milestone 01.
- A backend boundary for `draw-things-cli`, H3, and future generators in Milestone 02.
- H3 image-to-audio-video (`i2av`) jobs through the existing front ends in Milestone 03.
- Framechain product, package, command, and state database naming in Milestone 04.
- H3 performance controls and mixed `i2av`/reference-to-audio-video (`r2av`, H3's Ref2VA model family) pipelines in Milestone 05.
- H3 generated-video color characterization, truthful tags where verified, warning-based fallback, and opt-in `i2av` color drift correction in Milestone 06.
- Faster, deterministic local and CI validation in Milestone 07.
- Framecontrol product, app, package, interface, and state database naming in Milestone 08.
- Unified per-run job definitions, scoped knob defaults, run-specific timing, and mixed backend chains in Milestone 09.
- Measured per-stage timing for both backends and Framecontrol's own finishing, kept with each execution's history, in Milestone 10.
- A stage-aware, measured completion-time forecast for the running job, computed once by the server and shown by every front end, in Milestone 11.
- The forecast's heuristic knobs (tunables and per-backend, per-run-knob, per-size calibration) kept as external files under `data/timing-data-analysis/`, factory defaults where a value is missing, provisional early learning, and later updates validated on unseen executions, in Milestone 12.
- Producing and consuming graph-based metadata in Milestone 20.

## Non-goals

- Changing generation behavior as part of Milestone 01's TUI work.
- Adding a third generator as part of the backend refactor or H3 integration.
- Adding H3 reference-image, video-reference, or last-frame modes in the initial `i2av` integration; Ref2VA support follows in Milestone 05.
- Predicting the start and end of queued entries in Milestone 11; its forecast covers the running job.

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 01 | [TUI interaction and contextual shortcut help](milestone-01-tui-interaction.md) | done |
| 02 | [Refactor for multiple backends](milestone-02-multiple-backends.md) | done |
| 03 | [H3 image-to-audio-video support](milestone-03-h3-i2av.md) | in-progress |
| 04 | [Rename to Framechain](milestone-04-framechain-rename.md) | done |
| 05 | [H3 controls and mixed reference pipelines](milestone-05-h3-r2av.md) | in-progress |
| 06 | [H3 video color tags and correction](milestone-06-h3-color.md) | done |
| 07 | [Check performance](milestone-07-check-performance.md) | done |
| 08 | [Rename to Framecontrol](milestone-08-framecontrol-rename.md) | done |
| 09 | [Unified job definitions and per-run knobs](milestone-09-job-definition.md) | done |
| 10 | [Stage timing measurement](milestone-10-stage-timing.md) | done |
| 11 | [Job completion time estimation](milestone-11-time-estimation.md) | in-progress |
| 12 | [Timing knobs as external files](milestone-12-timing-knobs.md) | planned |
| 20 | [Graph-based metadata](milestone-20-graph-metadata.md) | planned |

## Depends on

- [Phase 3](../archive/phase-3/README.md)'s TUI, queue, state store, and server event feed.
- Milestone 03 depends on Milestone 02's backend boundary and the local [h3.c](https://github.com/antirez/h3.c) executable and model snapshot. See the [H3 research notes](../research/h3-c.md).
- Milestone 05 depends on Milestone 03's H3 runtime, staged MP4 finishing, and queue snapshot contract.
- Milestone 06 depends on Milestone 03's H3 `i2av` output and handoff contract, and Milestone 05's mixed `i2av`/`r2av` pipeline; M06 applies color tags to both pipelines.
- Milestone 09 depends on the backend contracts, H3 handoff behavior, reference-pipeline validation, and backend-specific color finishing from Milestones 02, 03, 05, and 06.
- Milestone 10 depends on the backend boundary, H3 runtime and finishing, and per-run records from Milestones 02, 03, and 09.
- Milestone 11 depends on Milestone 10's stage timeline and per-run timing rows.
- Milestone 12 depends on Milestone 11's landed forecast, fit, and replay harness, which it refactors onto a tunables and calibration seam, and on Milestone 10's timing rows.
- Milestone 20 follows Milestones 05, 06, and 09 so its provenance graph can record frozen references, retained video copies, and each run's backend and pipeline.

## Changelog

Decisions and notable changes are recorded in
[phase-4-changelog.md](phase-4-changelog.md).

## Exit criteria

- Milestone 01's canonical interaction terminology, consistent visible TUI names, and contextual shortcut help are complete and documented.
- Draw Things behavior continues through the backend boundary; already queued and resumable version-1 work remains runnable, while new authoring uses the version-2 job format.
- `i2av` jobs use H3 from every existing front end, with native audio preserved in their outputs and run history.
- H3 jobs expose reproducible rendering controls and safely mix first-frame `i2av` runs with ordered-reference `r2av` Ref2VA runs.
- H3 generated videos carry truthful color tags when verified; otherwise a warning identifies the original output and any conflicting tags it retains. `i2av` jobs can opt into bounded, recorded color correction without changing the default output pixels.
- Framecontrol is the product and app name, with `fc` as its installed command and existing state safely migrated.
- Version-2 jobs resolve named profiles and scoped defaults into a frozen per-run plan; H3 duration and Draw Things temporal control can vary by run without duplicated H3 frame values.
- Every run of both backends records a measured stage timeline that is kept and pruned with its execution, as defined in Milestone 10.
- The running job's completion time and progress come from one stage-aware forecast with a stated range and basis, computed once by the server, shown the same way by the TUI's work status, `fc queue add --wait` and `fc queue show`, the HTTP API, gRPC, and MCP, and measured against the previous estimator by replay, as defined in Milestone 11.
- The forecast's tunables and calibration live in files under `data/timing-data-analysis/`, factory defaults apply where a value is missing, early calibration is labeled provisional, later changes pass a held-out replay gate with backups, an owner's hand edit is kept, and the forecast depends on each run's backend, knobs, width, and height, as defined in Milestone 12.
- Graph-based metadata is produced, consumed, and documented as defined in Milestone 20.
