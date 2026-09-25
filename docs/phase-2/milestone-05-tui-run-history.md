# Milestone 05: Command Layout and Execution History

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Milestone 02: State store and run lock](milestone-02-state-store-run-lock.md), [Milestone 04: Live run view](milestone-04-tui-live-run.md)

## Goal

Put everything on one screen, driven by `/` commands typed on a command
line, and show what ran before: every execution, its result, its runs, and
where the outputs are, read from the state store.

## Scope

In scope:

- One screen with five widgets, replacing the job list, detail, live, and
  help screens of Milestones 03 and 04
- A command line with `/` commands and Tab completion
- Removing the single-key shortcuts `q`, `x`, `l`, `s`, `r`, and `?` (and
  the list's `j`/`k`); every action they had is a command
- Ctrl-C pressed twice to quit
- The execution history pane: newest first, paged, filtered by status and
  by job name
- The detail of one execution and its runs, and revealing an output file in
  Finder

Out of scope:

- Deleting history or output files
- Re-running a past job from its snapshot (the job file is what runs)
- Resuming an interrupted job (Phase 3, Milestone 01)
- Non-macOS "reveal" support
- Saving command history across sessions

## Layout

```
┌ draw-things-cli ──────────────────────┐┌ History ───────────────┐
│ Run 1/2  00:12 elapsed  progress 4/8  ││ ID Job     Status  ... │
│ (child output, last 2000 lines)       ││ 12 walk    running     │
│                                       ││ 11 walk    succeeded   │
└───────────────────────────────────────┘│                        │
┌ Messages ─────────────────────────────┐│                        │
│ 14:03:01 > /run walk                  ││                        │
│ 14:03:02 Job started: sunset-walk ... ││                        │
│ (command output and job log)          ││                        │
└───────────────────────────────────────┘└────────────────────────┘
────────────────────────────────────────────────────────────────────
> █
────────────────────────────────────────────────────────────────────
 walk.yaml running run 1/2  |  /path/to/data  |  /help: commands
```

From the top down, the left column holds the draw-things-cli pane and
Messages, and the history pane fills the right column. Under both, across
the full width, come a rule, the command line, a rule, and the status line.

- **draw-things-cli** (top left): 15 lines including its border, so 12
  lines of output under the run line. On a terminal too short to give
  Messages at least 6 lines, it shrinks, but to no fewer than 7 lines.
  - The **run line** (first line) shows one of:
    - the active run's number, elapsed time, step progress, and output name
    - the cooldown countdown
    - the job's phase (`starting`, `stopping`, `finished`, `did not
      start`)
    - "No job has run in this session"
  - Below it: the child's last 2000 output lines, as the M04 output pane
    showed them (stderr in red; progress-bar lines update the run line
    instead). A new job's output replaces the last job's.
- **Messages** (left, the rest of the height), capped at 5000 lines. It
  holds:
  - the echo of each command (`> /run walk`)
  - its output: job lists, job details and dry-run plans, execution
    details, and errors
  - a readable log of the job: started, each run started (with its
    redacted command) and finished, cooldowns, stop requests, and the
    result with the manifest and log paths

  Each entry starts with the local time. Text is shown as written, never
  as markup.
- **History** (right, a third of the width and at least 36 columns, the
  full height above the upper rule): executions,
  newest first. The active filter is in its border title; "No execution
  history yet", "No executions match the filter", or a store error is in
  its border subtitle.
- **Command line**: one input line between two thin horizontal rules (`─`)
  that span the full width of the screen, in white (see [Style](#style)).
  The line shows `> ` followed by a steady (not
  blinking) white block cursor, with no placeholder text. It has focus when
  the app starts and after every dialog. When the history table has focus,
  the cursor is hidden.
- **Status line** (one line, under the lower rule):
  - the running or last job's file, phase, and run, or `idle`
  - the data directory
  - the hint `/help: commands, Ctrl-C twice: quit`

  For 2 seconds after a first Ctrl-C, it shows "Press Ctrl-C again to quit"
  instead.

The drawing is a sketch; the minimum terminal size is 80x24.

## Style

- The TUI uses a dark theme by default: the app sets Textual's
  `textual-dark` theme itself, so the terminal's own colors do not change it.
- Textual's command palette (Ctrl-P) is turned off. It would add a second,
  different way to run commands, and a way to switch themes that the plan
  does not cover.
- The draw-things-cli pane, Messages, and the status line are on a black
  background (owner decision); the command line and the history keep the
  theme's background.
- Pane borders are rounded, in the theme's panel color, with titles in the
  accent color; the focused history pane's border turns to the accent color.
- The block cursor is white. The two rules around the command line use
  the theme's foreground color, which is white in the dark theme (owner
  decision).
- Status colors are the same everywhere (run line, Messages, History):
  `running` bold cyan, `succeeded` green, `failed` and `timed_out` red,
  `interrupted` yellow. stderr lines and errors are red.

## Keys

The single-key shortcuts of Milestones 03 and 04 are removed: `q` (quit),
`x` (run), `l` (live view), `s` (stop), `r` (refresh), `?` (help), and
`j`/`k` in the job list. Typing those letters on the command line types
them. These keys remain:

| Key | Where | Action |
|-----|-------|--------|
| Enter | Command line | Run the command |
| Tab | Command line | Accept the completion; with none, move to the history |
| Up/Down | Command line | Recall the earlier and later commands typed in this session |
| Escape | Command line | Clear the line |
| Shift-Tab | Anywhere | Move focus back |
| Up/Down, PageUp/PageDown, Home/End | History | Move in the list |
| Enter | History | Show the execution's detail, as `/execution` |
| Escape | History | Back to the command line |
| Ctrl-C | Anywhere but a dialog | Clear the command line, or quit on a second press (below) |
| `y`, `n`/Escape | Dialog | Answer the confirmation; Enter also answers yes to `/stop` and `/quit`, but not to `/run` |

The logs are not focusable; they scroll with the mouse wheel.

### Ctrl-C

- If the command line holds text, Ctrl-C clears it and does nothing else.
- Otherwise the first press arms the quit and shows "Press Ctrl-C again to
  quit" in the status line. A second press within 2 seconds quits when no
  job runs. While a job runs, it opens the "Stop the job and quit?" dialog,
  as `/quit` does. After 2 seconds the first press is forgotten.
- While a dialog is open, Ctrl-C does nothing; the dialog is answered with
  its own keys.
- Once a quit is waiting for the job to stop, Ctrl-C writes "Waiting for the
  job to stop" and does nothing else.
- Textual reads the terminal in raw mode, so Ctrl-C arrives as a key, not a
  signal. `SIGINT`, `SIGTERM`, and `SIGHUP` sent from outside still stop the
  job and quit with 128+N, as in Milestone 04.

### Command recall

- Up and Down on the command line step through the commands entered since
  the app started, whether they ran or failed. Down past the newest returns
  to an empty line.
- A command that repeats the one before it is kept once.
- Recall lives in memory only; nothing is written to disk.

## Commands

Every command begins with `/`, and its name says what it acts on.

| Command | Action |
|---------|--------|
| `/help` | List the commands and keys |
| `/jobs` | List the job files and whether each is valid (reads the directory again) |
| `/job JOB` | The summary, prompt pairs, and dry-run plan of a job (reads the file again) |
| `/run JOB` | Read the job again, confirm, and run it |
| `/stop` | Stop the running job, after confirmation |
| `/history` | Read the history pane again |
| `/execution ID` | The detail of one execution |
| `/filter status STATUS` | Show only `succeeded`, `failed`, `interrupted`, or `running` executions |
| `/filter name TEXT` | Show only executions whose job name or job file name contains `TEXT` (any letter case) |
| `/filter off` | Remove both filters |
| `/reveal ID [RUN]` | Reveal a run's output in Finder (default: the last run with an output) |
| `/clear` | Clear the messages |
| `/quit` | Quit; while a job runs, asks to stop it first |

- The two filters combine; setting one keeps the other.
- `JOB` is a job file name in the data directory (`walk.yaml`), or its name
  without the suffix when only one file has it. Tab completes a name that
  needs quoting in the style already typed: inside an opened quote, it
  closes the quote; otherwise it escapes spaces and other special
  characters with backslashes (`my\ job.yaml`).
- Arguments are split as a shell splits them, so a name with spaces is
  quoted. Command names match in any letter case.
- There are no aliases (`exit`, `?`, and unprefixed words are gone).
- Errors go to Messages, in red, and run nothing:
  - a line without `/`: "Commands begin with /; type /help"
  - an unknown command: "Unknown command '/launch'; type /help"
  - wrong arguments: the command's usage
  - unclosed quotes: the parse error
- An empty line does nothing.
- Tab completes:
  - command names after `/`
  - job file names after `/job` and `/run`
  - `status`, `name`, and `off` after `/filter`
  - the statuses after `/filter status`

## History pane

- Each row: execution ID, job name, status, start time (local, `MM-DD
  HH:MM`), and runs succeeded of total. Mode, seconds, and exit code are in
  the detail.
- Loaded in pages of 200 from the state store; the next page loads when the
  cursor reaches the last row, so a long history opens at once.
- Read when the app starts, on `/history`, when a filter changes, and when
  this TUI's job starts, finishes a run, and ends. A re-read keeps as many
  rows as were loaded and keeps the cursor on the same execution, so
  refreshing never jumps back to the first page.
- While another process holds the run lock (for example, `run-job` in
  another terminal, or later the Phase 3 server), it checks the store every 5
  seconds, and once more after the lock is released. A newer execution than
  the pane shows means reading the pane again; otherwise only its `running`
  rows are read and updated in place. This works whatever the filter shows,
  and whether the other job started before or after the TUI. There are no
  live events across processes; the store is the only channel.
- While this TUI runs a job, the pane is read again when the job starts and
  when it ends; each finished run updates only that execution's row, which
  the recorder's `execution_id` names.
- Reads go through one `HistoryReader` that keeps a single `Store` for the
  screen's life. No read raises: a failed Textual worker would close the
  app, so every error, not only `sqlite3.Error`, becomes the pane's
  subtitle or a message. Only the newest read's result is shown, and
  changing a filter clears the rows at once, so a page read for one filter
  never lands under another.
- When the run lock is free, `running` rows are shown as `interrupted`, as
  Milestone 02 planned for read-only screens; the database is not changed.
- `ID` and `RUN` are ASCII digits from 1 to 2^63-1, the largest SQLite
  integer; anything else is a usage error.
- The filter and error texts in the pane's title and subtitle are shown as
  written, never read as markup.
- The name filter needs a new store option: a case-insensitive substring
  match on `job_name` or on the file name at the end of `job_file`
  (`LIKE` with `%` and `_` escaped). The succeeded-run counts come from a
  new store method, `succeeded_runs`, with one query per page.
- If `state/dtc.db` does not exist yet, the pane says there is no history,
  and the file is not created. The pane opens the store without pruning
  (pruning writes), so browsing still writes nothing.

## Execution detail

`/execution ID` (or Enter on a row) writes to Messages:

- The execution as it ran, from the stored row, never from the current job
  file:
  - job name and file, mode, status, model, seed and its source, and
    cooldown
  - start and finish time, exit code and signal
  - manifest and log paths

  Executions imported by `import-history` have no stored job snapshot and
  are marked `imported`. One closed by the crash sweep says so.
- Each run: pair, positive and negative prompt, input, output, last frame,
  seconds, exit code, status, cooldown after it, and the command as stored
  (with credentials already redacted).
- Outputs and last frames are shown as full paths. The store keeps only
  file names; they are joined to the output directory recorded in the
  execution's settings, or, for an imported execution, to its manifest's
  folder. A file that no longer exists is marked `(missing)`, not treated
  as an error.

## Reveal

`/reveal ID [RUN]` runs `open -R <path>` (macOS Finder) on the run's
output. The path is passed as an argument, never through a shell. These
write a message and do nothing:

- a missing file
- an unknown execution or run
- a run without an output
- an execution without a recorded output directory

If `open` itself cannot run or exits with an error, Messages says so. The
app never stops because of it: a failed background worker would close the
Textual app. Tests replace `open` with a fake.

## Planned changes

- `tui/screens.py`: `MainScreen` (the five widgets and the command
  dispatch) and `ConfirmScreen`. The job list, detail, live, and help
  screens are removed.
- `tui/commands.py`: the command table, parsing, usage, help text, and
  completion.
- `tui/history.py`: history reads that never create or prune the database,
  filters, output paths, and reveal.
- `tui/widgets.py`:
  - widgets: `CommandInput` (steady block cursor, Tab completion, recall,
    Escape and Ctrl-C clearing), `HistoryTable`, and `MessageLog`
  - the text each widget shows
- `state/recorder.py`: `ExecutionRecorder.execution_id`.
- `tui/app.py`:
  - only the Ctrl-C binding, which handles the two-press quit
  - notices go to Messages instead of toasts
  - job and signal handling unchanged from Milestone 04
- `tui/styles.tcss`: the layout, the rules, and the cursor. `tui/app.py`
  sets the `textual-dark` theme and turns off the command palette.
- `state/store.py`: `succeeded_runs`, `executions_by_id`, and the
  substring name filter.
- Tests:
  - `tests/tui/test_commands.py` for parsing and completion
  - `tests/tui/test_tui.py` for the layout and browsing
  - `tests/tui/test_live_run.py` for running and signals
  - `tests/tui/test_history.py` for the history pane
  - shared helpers in `tests/tui/tui_case.py`
  - `tests/tui/sigterm_app.py` types `/run walk`
- Docs:
  - `docs/user-guide.md`: the TUI section and its key table rewritten
    around the commands
  - `docs/architecture.md`: the `tui/` modules
  - `docs/phase-2/README.md`: this milestone's title
  - `phase-2-changelog.md`: the decisions

## Acceptance criteria

- At 80x24 and larger, the widgets are laid out as above:
  - the draw-things-cli pane is 15 lines when there is room, and never
    fewer than 7
  - Messages keeps at least 6 lines
  - the command line is one line between two full-width rules
  - the status line is one line
- `q`, `x`, `l`, `s`, `r`, and `?` do nothing but type on the command line.
- Every command begins with `/`, and a line without it runs nothing. Every
  M03 and M04 workflow works through commands: list, show, run, confirm,
  stop, and quit.
- Up/Down recall this session's commands, and Escape clears the command
  line. Ctrl-C clears a non-empty command line. Otherwise it quits only on a
  second press within 2 seconds, and asks first while a job runs. The
  signal and shutdown behavior of M04 is unchanged.
- History shows executions from `run-job`, from the TUI, and from an
  import, newest first. The status and name filters narrow it, combine,
  and are removed by `/filter off`. A refresh keeps the loaded rows and the
  selection.
- The detail shows the stored snapshot even after the job file is edited or
  deleted. A missing output is marked, not an error. `/reveal` on an
  existing output calls `open -R` with the path, and a failing `open` is
  reported without closing the app.
- Messages keeps at most 5000 lines.
- No credential value appears in any widget.
- Headless tests cover:
  - the layout at 80x24 and at a larger size
  - each command, its errors, completion, recall, and Escape
  - Ctrl-C in each case above
  - both filters, and paging with a refresh
  - the detail, an imported execution, a missing output, and an empty
    history
  - a reveal that fails
  - the running and signal cases of M04
- The app opens in the dark theme, and Ctrl-P opens nothing.
- `make check` passes.
