# Phase 4 changelog

Owner decisions, design decisions, and notable changes for
[Phase 4](README.md). Newest first.

## 2026-10-12

- **Change** [M01]: Milestone 01 is done. Its contextual shortcut help and focus-sensitive status-line hint are covered for the Jobs list, Queue list, Execution History list, and Execution detail; `make check` passes.

## 2026-10-05

- **Owner decision** [M01]: Canonical TUI UX terminology is recorded in [TUI UX terms](../ux-terms.md) before shortcut and status-display planning. It distinguishes the Status pane from the status line and names every main-screen and transient component.
- **Owner decision** [M01]: The terminology is revised around Apple Human Interface Guidelines: it now favors task-oriented, unambiguous names; separates focus from selection; and sets consistent language and scope rules for keyboard shortcuts and feedback. Existing visible titles may change to match the canonical names.
- **Change** [M01]: The TUI now uses the canonical names in its visible titles, Textual classes and IDs, command help, tests, and user guide: Work status, Generation output, Activity log, Jobs list, Queue list, Execution History list, Execution detail, and command field.
- **Change** [M01]: A focused list or Execution detail immediately adds `Press ? for shortcut help` to the status line. `?` opens a centered shortcut help overlay for that focused component; `?` or Escape closes it and restores focus.

## 2026-10-04

- **Owner decision**: Phase 4 is titled “(1) Producing and Consuming Graph-based Metadata (2) Squashed Product Improvement”. No milestones are planned yet.
