# Milestone 03: TUI Shell and Job Browser

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
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
  `typer.Exit`. `read_job` raises on an invalid global configuration too, so
  the `load_settings`/`read_job` exit-code translation in `cli/app.py` becomes
  one place. `report_ignored_config` logs through Loguru today; it moves to
  return its warning lines so the TUI can show them in the detail view
  instead of logging into a sink that does not exist. `cli/app.py` keeps only the `typer.echo` and exit-code
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
- There are two `RunnerFactory` protocols: `core/generation_service.py`
  (used by `GenerationService`, so by `generate`) and `jobs/job_service.py`
  (which extends it with `request_shutdown`). Both gain the `on_start`
  argument; `GenerationService.execute` and `JobService` forward it. The
  runner's own `on_start` takes only the PID, so the factory in `cli/app.py`
  binds the executable name (`lock.record_child(pid, name)`) and
  `on_child_start` on `JobService.run()` is `Callable[[int, str], None]`.
- A test shows a runner built for a job reports its child's PID to the lock
  without any module state, and that a `SIGKILL`ed holder still blocks a
  second starter (the M02 guarantee).
- The changelog gets an entry superseding the M02 "known limit" note.

### Command and package

- `dtc tui [--global-config PATH] [--data-dir PATH] [--executable PATH]`
  starts the app. Defaults: `PROJECT_ROOT/data`, `config/global-config.yaml`,
  and `draw-things-cli`. The data directory is anchored to the project root,
  like the global configuration, so the TUI works from any working
  directory. `--executable` has the same meaning and default as in
  `run-job`; the detail view's plan uses it, and Milestone 04 adds
  `--shutdown-grace` beside it.
- The `App` takes its collaborators (settings, data directory, executable,
  and a `JobService`) as constructor arguments; `cli/app.py` builds the real
  ones. Tests pass a `JobService` with a fake `find_executable` and fake tool
  checks, and use temporary data and configuration directories, so no test
  reads the real `data/`, `config/`, or `state/`.
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
  use the placeholder seed `0`, with a note above the plan that the real
  seed replaces it, so the plan is the same every time the view opens and a
  copied command is not mistaken for the one a run will use.
  This needs `JobService.preview` to accept an optional `seed` override; it
  keeps drawing a random seed when none is given, so `run-job --dry-run` is
  unchanged.
- If `draw-things-cli` or `ffmpeg` is missing, `preview` raises `ValueError`;
  the plan pane shows that message and the summary and pairs still render.
- For an invalid job, the error and nothing else.
- The plan is computed on a worker and cached per file until the next
  refresh; the refresh key clears the whole cache. It is not keyed by
  modification time, because the plan also depends on the `dt-config` file,
  the global configuration, and the tools, which a file-time key would miss.

### Keys

| Key | Action |
|-----|--------|
| Up/Down, `j`/`k` | Move in the list |
| Enter | Open the detail view |
| Escape | Back from the detail view |
| `r` | Refresh the list |
| `?` | Help |
| `q`, Ctrl-C | Quit |

The run and history keys are added by Milestones 04 and 05. `r` means
refresh here; Milestone 04 needs a different key for "run" (`x`) so the two
do not collide, and updates this table.

## Acceptance criteria

- `validate-job` and `run-job --dry-run` output is unchanged by the move to
  `jobs/job_report.py` (pinned by a test written before the move).
- No module-level lock state remains in `cli/app.py`; a runner reports its
  child to the lock it was given.
- `dtc tui` opens, lists the jobs in the data directory (by default
  `PROJECT_ROOT/data`, from any working directory), and quits cleanly with
  `q` and with Ctrl-C. Ctrl-C is bound to quit explicitly; Textual's default
  handling of it is checked through Context7 before building.
- `--executable` is used for the plan: with a local binary passed, the
  detail view shows the plan, not the missing-tool message.
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

## Outcome

Built as planned, with these details settled while building:

- `jobs/job_report.py` holds `read_settings`, `read_job` (which also accepts
  an already loaded `GlobalConfig`, so the TUI reads the global configuration
  once), `job_files`, the cooldown text helpers moved from
  `job_definition.py`, `ignored_config_lines` and `report_ignored_config`
  (which logs them, for the CLI and the job log), `job_summary` (whose
  `random_seed_text` lets the TUI word a random seed), `pair_runs`, and
  `plan_lines`. `job_files` matches `.yaml` and `.yml` in any letter case. `cli/app.py` translates `ValueError` to exit 2 in one place,
  `invalid_input_exits()`. `tests/cli/test_job_output.py` pins the output.
- The one callback type is `ChildStartCallback = Callable[[int, str], None]`
  in `core/generation_service.py`. Both `RunnerFactory` protocols take it as
  `on_start`; `create_runner` binds it to the runner's PID-only `on_start`
  with the executable's name. `tests/cli/test_run_lock_reporting.py` runs a
  job in a holder process with a stand-in executable, `SIGKILL`s the holder,
  and shows the next starter is refused.
- `JobService.preview(..., seed=)` uses `seed` only when the job configures
  none; the source stays `random`.
- `dtc tui` loads the global configuration before the app starts; an invalid
  one is logged and exits 2 without opening the app. It then removes the
  Loguru sinks `main()` installed, since the app owns the terminal, and
  reinstalls them when it exits. If the app ends on an error, the command
  exits with its return code.
- Job text reaches widgets as `rich.text.Text`, never as a markup string: a
  YAML error quotes the file's line, and prompts often hold brackets.
  Errors while planning (`ValueError` or `OSError`) show in the plan pane,
  and results that arrive after the detail view has closed are cached but
  not shown.
- Textual's own Ctrl-C is `help_quit` (it only explains how to quit), so the
  app binds it to `quit` with `priority=True`.
- `cli/app.py` imports `tui/` only inside the `tui` command, to start it;
  `tui/` imports nothing from `cli/`. `AGENTS.md`, the development rules,
  and the architecture state this exception.
