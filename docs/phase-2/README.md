# Phase 2: Terminal UI for Humans

**Status:** in-progress

## Goal

Let a person browse, run, monitor, and review jobs from a terminal UI,
without watching a scrolling log. Along the way, build the shared core that
Phase 3 also needs: structured progress events, cancellation that does not
depend on signals, a SQLite state store, and a run lock that keeps two
processes from driving the GPU at once.

## Scope

- Structured job events and a `cancel()` method on `JobService`, so a
  front end can observe and stop a job without parsing logs or raising
  signals. The CLI's behavior and log output do not change.
- A SQLite state store (`sqlite3` from the standard library) holding job
  executions and per-run records: the source of truth for execution history
- An OS-level run lock (`fcntl.flock`) taken by every command that starts a
  generation: `run-job`, `generate`, and the TUI
- A Textual TUI, started with `dtc tui`, to browse jobs, run and stop a
  job, watch it live, and review past runs
- One-time import of existing manifests into the state store
- Pruning of history older than 14 days (`history_retention_days` overrides
  it); database rows only, never output files

## Non-goals

- Editing job files in the TUI. Jobs are still written in an editor.
- Editing `dt-config/*.json` or the global configuration from anywhere.
- Parallel generation. One run at a time, machine-wide.
- A queue of jobs. Phase 2 runs one job at a time; queuing is
  [Phase 3](../phase-3/README.md).
- Recording the one-off `generate` command in the state store. It takes the
  run lock, but it is not a job and stays out of history.
- Image or video preview inside the terminal.
- Non-macOS support for "reveal in Finder".

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 01 | [Job events and cancellation](milestone-01-job-events-cancel.md) | done |
| 02 | [State store and run lock](milestone-02-state-store-run-lock.md) | planned |
| 03 | [TUI shell and job browser](milestone-03-tui-job-browser.md) | planned |
| 04 | [Live run view](milestone-04-tui-live-run.md) | planned |
| 05 | [Execution history](milestone-05-tui-run-history.md) | planned |

Milestones are built in order: 02 records what 01 emits, and 03 to 05 sit on
both.

## New dependencies

- `textual` (the TUI). It is added in Milestone 03 and installed with a plain
  `uv sync`, not as an optional extra (owner decision, see the changelog).
- No new dependency for the state store or the lock: both use the standard
  library.

Library documentation is fetched through Context7 when a milestone is built,
as AGENTS.md requires.

## Code layout

All code lives in the `src/draw_things_control/` package (see
[architecture](../architecture.md)). The TUI lives in `tui/`. Shared new
modules: `jobs/job_events.py` (Milestone 01), and `state/store.py`,
`state/recorder.py`, and `core/run_lock.py` (Milestone 02).

## Changelog

Decisions and notable changes are recorded in
[phase-2-changelog.md](phase-2-changelog.md).

## Exit criteria

- Every phase-1 workflow (validate, dry run, run, stop) can be done from the
  TUI.
- Stopping from the TUI, or with Ctrl-C in the CLI, ends a run and a
  cooldown at once, with the same outcome as in phase 1.
- Starting a run while another process holds the run lock fails immediately
  with a clear message and a distinct exit code; it never queues silently and
  never runs two `draw-things-cli` processes at once.
- Execution history in the TUI comes from the state store, including
  executions started from the CLI and executions imported from phase 1
  manifests.
- No credential value (`--api-key`, `--remote-shared-secret`) reaches the
  state store or an event.
- TUI screens are covered by headless tests; no test starts the real
  `draw-things-cli`.
- `make check` passes.
