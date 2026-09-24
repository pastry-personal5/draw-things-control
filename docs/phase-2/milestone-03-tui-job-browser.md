# Milestone 03: TUI Shell and Job Browser

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Milestone 01](milestone-01-job-events-cancel.md), [Milestone 02](milestone-02-state-store-run-lock.md)

## Goal

Start the terminal UI and let a person see every job, whether it is valid,
and exactly what running it would do, without leaving the terminal.

## Scope

In scope:

- The `main.py tui` command and the Textual application shell in `tui/`
- A job list read from the job directory
- A read-only detail view: the resolved job and its dry-run plan
- Global keys and a help screen

Out of scope:

- Starting a job (Milestone 04) and history (Milestone 05)
- Creating, editing, or deleting jobs
- Anything that changes a file

## Planned changes

### Command and package

- `main.py tui [--global-config PATH] [--data-dir PATH]` starts the app.
  The default data directory is `data/`.
- Code lives in `tui/`: `app.py` (the `App`), `screens.py`, `widgets.py`,
  and `styles.tcss`. The screens use existing core code
  (`read_job`, `JobService.preview`) and add no job logic of their own.
- `textual` is added to `pyproject.toml` and `uv.lock` (always installed).
  Its documentation is checked through Context7 first.

### Job list

- Lists `*.yaml` and `*.yml` files directly under the data directory.
  Dot-directories (`.trash`, `.backups`, added in Phase 3) and dotfiles are
  skipped.
- Each row shows the job name, mode, run count, and validation status.
  Validation uses the same code as `validate-job`. An invalid file is shown
  with the first error, naming the field, not hidden.
- The list is read when the screen opens and when the user presses the
  refresh key. There is no file watcher.
- Files are read-only in the TUI. Reading never writes, including the
  global configuration.

### Detail view

For the selected job:

- The summary `validate-job` prints (mode, runs, seed, cooldown and its
  source, output extension).
- The prompt pairs, with which batches use each.
- The dry-run plan: each planned run and its redacted command, as
  `run-job --dry-run` prints it.
- For an invalid job, the error and nothing else.

### Keys

| Key | Action |
|-----|--------|
| Up/Down, `j`/`k` | Move in the list |
| Enter | Open the detail view |
| `r` | Refresh the list |
| `?` | Help |
| `q` | Quit |

The run and history keys are added by Milestones 04 and 05.

## Acceptance criteria

- `main.py tui` opens, lists the jobs in `data/`, and quits cleanly with
  `q` and with Ctrl-C.
- A valid job shows its summary, prompt pairs, and dry-run plan; the plan
  matches `run-job --dry-run` for the same file.
- An invalid job appears in the list with an error naming the offending
  field and does not stop the other jobs from loading.
- Dot-directories in the data directory are not listed.
- No file under `data/`, `config/`, or `dt-config/` is modified by using the
  TUI.
- Headless Textual tests (`Pilot`) cover the list, the detail view, an
  invalid job, an empty data directory, and quit.
- `make check` passes.
