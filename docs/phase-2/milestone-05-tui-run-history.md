# Milestone 05: Run History

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Milestone 02: State store and run lock](milestone-02-state-store-run-lock.md), [Milestone 03: TUI shell and job browser](milestone-03-tui-job-browser.md)

## Goal

Show what ran before: every job run, its result, its runs, and where the
outputs are, read from the state store.

## Scope

In scope:

- A history screen listing job runs, newest first
- A detail view for one job run and its runs
- Revealing an output file in Finder
- Filtering by job name and status

Out of scope:

- Deleting history or output files
- Re-running a past job from its snapshot (the job file is what runs)
- Resuming an interrupted job (Phase 3, Milestone 01)
- Non-macOS "reveal" support

## Planned changes

### History list

- `h` opens the history screen. Each row: job name, mode, status, started at
  (local time), total seconds, runs succeeded of total, and exit code.
- Newest first, loaded in pages from the state store, so a long history
  opens at once.
- Filters: by job name (text) and by status (`succeeded`, `failed`,
  `interrupted`, `running`). A `running` row means a job is running now, in
  this or another process; it is shown, not hidden.
- The list refreshes when opened and on a refresh key, and while a job
  runs from this TUI it is updated from that job's events.
- While the list shows a `running` row that this TUI did not start (for
  example, a job run by the Phase 3 server or by `run-job` in another
  terminal), it re-reads the store every 5 seconds, so its runs appear as
  they finish. There are no live events across processes; the store is the
  only channel.

### Detail view

For the selected job run:

- The job snapshot summary as it was when it ran (seed, cooldown, mode),
  from the stored snapshot, not from the current file.
- Each run: pair, positive and negative prompt, input, output, last frame,
  seconds, exit code, status, cooldown after it, and the redacted command.
- For a missing output file, the path is shown with a "missing" mark, not an
  error.

### Reveal

`o` on a run with an output that exists runs `open -R <path>` (macOS
Finder). The path is passed as an argument, never through a shell. If the
file is missing, the key shows a message and does nothing.

### Imported history

Runs imported by `import-history` (Milestone 02) appear like any other,
marked `imported`, because they have no stored job snapshot and may lack
fields the app records now.

## Acceptance criteria

- History shows runs from `run-job`, from the TUI, and from an import,
  newest first.
- A filter by status and by name narrows the list and clears again.
- The detail view shows the stored snapshot even after the job file is
  edited or deleted.
- A missing output is marked, not an error; `o` on an existing output
  reveals it (with `open` replaced by a fake in tests).
- The list opens quickly with several thousand rows (paged reads).
- No credential value appears in any history view.
- Headless tests cover the list, both filters, the detail view, an imported
  run, a missing output, and an empty history.
- `make check` passes.
