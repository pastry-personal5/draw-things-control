# Milestone 03: TUI Shell and Job Browser

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Milestone 01](milestone-01-job-events-cancel.md), [Milestone 02](milestone-02-state-store-run-lock.md)

## Goal

Start the terminal UI and let a person see every job, whether it is valid,
and exactly what running it would do, without leaving the terminal.

## Scope

In scope:

- Moving the job-reading and job-text helpers out of `cli/app.py` so both front ends share them
- Removing the module-level `_active_lock` in `cli/app.py`
- The `dtc tui` command and the Textual application shell in `tui/`
- A job list read from the data directory
- A read-only detail view: the resolved job and its dry-run plan
- Global keys and a help screen

Out of scope:

- Starting a job (Milestone 04) and history (Milestone 05)
- Creating, editing, or deleting jobs
- Anything that changes a file

## Planned changes

### Shared job code (first task)

`tui` must not import `cli`, and `cli/app.py` currently owns the code the
detail view needs. Before any screen is written:

- New `jobs/job_report.py` holds `read_job` (the loader that returns
  `(JobDefinition, GlobalConfig)`), the `validate-job` summary text, the
  cooldown text helpers, and the `run-job --dry-run` plan formatting. They
  return values or raise `ValueError`; they never print and never raise
  `typer.Exit`. `cli/app.py` keeps only the `typer.echo` and exit-code
  translation (`ValueError` -> exit 2), so `validate-job` and
  `run-job --dry-run` produce byte-identical output. A test pins that output
  before the move.
- The TUI calls `load_job`/`load_global_config` through `read_job`; there is
  no job logic in `tui/`.

### Run lock reporter without a global

The M02 changelog left `create_runner` reading the module-level
`_active_lock` and said to revisit it when the TUI became a second caller.
Do it here, so M04 starts clean:

- `JobService.run()` takes an optional `on_child_start: Callable[[int, str], None]`
  and passes it to each runner it creates; `RunnerFactory` gains that
  argument (`on_start`), alongside `on_message`.
- `run-job` passes `lock.record_child`; `_active_lock` and its
  assignments in `held_run_lock` are deleted. `generate` does the same
  through `GenerationService`.
- A test shows a runner built for a job reports its child's PID to the lock
  without any module state, and that a `SIGKILL`ed holder still blocks a
  second starter (the M02 guarantee).
- The changelog gets an entry superseding the M02 "known limit" note.

### Command and package

- `dtc tui [--global-config PATH] [--data-dir PATH]` starts the app. Defaults:
  `data/` and `config/global-config.yaml`, as for the other commands.
- Code lives in `src/draw_things_control/tui/`: `app.py` (the `App`),
  `screens.py`, `widgets.py`, and `styles.tcss`. `tui/` imports `state`,
  `jobs`, and `core` only.
- `textual` is added to `pyproject.toml` and `uv.lock` (always installed).
  Its documentation is checked through Context7 first, including `Pilot`
  testing and how a screen gets a `Worker`.
- Stdout and stderr Loguru sinks are not installed while the app owns the
  terminal (see the M01 changelog); the TUI does not call
  `configure_logging`.

### Job list

- Lists `*.yaml` and `*.yml` files directly under the data directory.
  Sub-directories are not scanned. Dot-directories (`.trash`, `.backups`,
  added in Phase 3) and dotfiles are skipped. A missing data directory shows
  an empty-state message naming the path, not a traceback.
- Each row shows the job name, mode, run count, and validation status,
  sorted by file name. Validation uses `read_job` with `decode_input=False`,
  so opening the list does not decode every input image; the input is
  decoded when the detail view opens the job. An invalid file is shown with
  its first error, naming the field, not hidden, and does not stop the others
  from loading.
- The global configuration is loaded once when the app starts; if it is
  invalid, the app shows the error and quits with exit code 2 rather than
  listing every job as invalid.
- Loading runs on a worker so a slow volume does not freeze the UI.
- The list is read when the screen opens and when the user presses the
  refresh key; the selection is kept by file name across a refresh. There is
  no file watcher.
- Files are read-only in the TUI. Reading never writes, including the
  global configuration and `state/`; the TUI does not open the store in this
  milestone.

### Detail view

For the selected job:

- The summary `validate-job` prints (mode, runs, seed, cooldown and its
  source, input, output directory, config file, model).
- The prompt pairs, with which runs use each.
- The dry-run plan: each planned run and its redacted command, as
  `run-job --dry-run` prints it, except the seed: a job with no configured
  seed shows `seed: random (drawn when the job starts)` and the commands
  use a placeholder seed, so the plan is the same every time the view opens.
  This needs `JobService.preview` to accept an optional `seed` override; it
  keeps drawing a random seed when none is given, so `run-job --dry-run` is
  unchanged.
- If `draw-things-cli` or `ffmpeg` is missing, `preview` raises `ValueError`;
  the plan pane shows that message and the summary and pairs still render.
- For an invalid job, the error and nothing else.
- The plan is computed on a worker and cached per file until the next
  refresh (keyed by path and modification time).

### Keys

| Key | Action |
|-----|--------|
| Up/Down, `j`/`k` | Move in the list |
| Enter | Open the detail view |
| Escape | Back from the detail view |
| `r` | Refresh the list |
| `?` | Help |
| `q` | Quit |

The run and history keys are added by Milestones 04 and 05. `r` means
refresh here; Milestone 04 needs a different key for "run" (`x`) so the two
do not collide, and updates this table.

## Acceptance criteria

- `validate-job` and `run-job --dry-run` output is unchanged by the move to
  `jobs/job_report.py` (pinned by a test written before the move).
- No module-level lock state remains in `cli/app.py`; a runner reports its
  child to the lock it was given.
- `dtc tui` opens, lists the jobs in `data/`, and quits cleanly with `q` and
  with Ctrl-C.
- A valid job shows its summary, prompt pairs, and dry-run plan; the plan
  matches `run-job --dry-run` for the same file, apart from the seed rule
  above.
- An invalid job appears in the list with an error naming the offending
  field and does not stop the other jobs from loading.
- With `draw-things-cli` or `ffmpeg` missing, the detail view still shows the
  summary and the message.
- Dot-directories in the data directory are not listed.
- No file under `data/`, `config/`, `dt-config/`, or `state/` is created,
  modified, or deleted by using the TUI.
- Headless Textual tests (`Pilot`) cover the list, the detail view, an
  invalid job, an empty and a missing data directory, refresh keeping the
  selection, a missing tool, and quit.
- `make check` passes.
