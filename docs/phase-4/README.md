# Phase 4: (1) Producing and Consuming Graph-based Metadata (2) Squashed Product Improvement

**Status:** in-progress

## Goal

Improve the product experience, make chained generation support multiple backends, add H3 image-to-audio-video generation, and produce and consume graph-based metadata.

## Scope

- TUI terminology, shortcuts, and status feedback in Milestone 01.
- A backend boundary for `draw-things-cli`, H3, and future generators in Milestone 02.
- H3 image-to-audio-video (`i2av`) jobs through the existing front ends in Milestone 03.
- Producing and consuming graph-based metadata in Milestone 10.

## Non-goals

- Changing generation behavior as part of Milestone 01's TUI work.
- Adding a third generator as part of the backend refactor or H3 integration.
- Adding H3 reference-image, video-reference, or last-frame modes in the initial `i2av` integration.

## Milestones

1. [Milestone 01: TUI interaction and contextual shortcut help](milestone-01-tui-interaction.md) — done
2. [Milestone 02: Refactor for multiple backends](milestone-02-multiple-backends.md) — done
3. [Milestone 03: H3 image-to-audio-video support](milestone-03-h3-i2av.md) — in progress
4. [Milestone 10: Graph-based metadata](milestone-10-graph-metadata.md) — planned

## Depends on

- Phase 3's TUI, queue, state store, and server event feed.
- Milestone 03 depends on Milestone 02's backend boundary and the local [h3.c](https://github.com/antirez/h3.c) executable and model snapshot. See the [H3 research notes](../research/h3-c.md).

## Changelog

Decisions and notable changes are recorded in
[phase-4-changelog.md](phase-4-changelog.md).

## Exit criteria

- Milestone 01's canonical interaction terminology, consistent visible TUI names, and contextual shortcut help are complete and documented.
- Existing Draw Things jobs continue to work through the backend boundary, including queued and resumed work.
- `i2av` jobs use H3 from every existing front end, with native audio preserved in their outputs and run history.
- Graph-based metadata is produced, consumed, and documented as defined in Milestone 10.
