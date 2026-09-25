# Milestone 04: Live Run View

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Milestone 03: TUI shell and job browser](milestone-03-tui-job-browser.md)

## Goal

Run a job from the TUI and watch it: which run is active, how long it has
taken, the cooldown countdown, and the output of `draw-things-cli`, with a
stop key that works at any moment. However the TUI ends, no
`draw-things-cli` it started is left running.

## Scope

In scope:

- Starting the selected job from the TUI, with confirmation
- A live view driven by job events, which survives leaving and reopening it
- Stopping a running job or cooldown
- A busy run lock, a job that fails before it starts, quitting during a run,
  signals sent to `dtc tui`, and the app ending unexpectedly during a run
- `--shutdown-grace` on `dtc tui`

Out of scope:

- Running more than one job, or queuing another job (Phase 3)
- Editing the job before running it
- Previewing the generated image or video
- Execution history screens (Milestone 05); the run is recorded, not shown

## What the TUI may write

M03's TUI wrote nothing. From this milestone, running a job writes what
`run-job` writes, and nothing else: the job's outputs and last frames, its
manifest and log when `write_job_records` is true, `state/dtc.db`, and
`state/run.lock`. Browsing still writes nothing, and nothing ever writes a
job file, `dt-config/`, or the global configuration. The user guide's
"only reads files" line changes to say this.

## Planned changes

### The service the TUI runs jobs on

`JobService` installs signal handlers when it is built with
`handle_signals=True` (the default, and what `create_job_service()` in
`cli/app.py` builds). `signal.signal` raises `ValueError` off the main
thread, so the TUI's jobs, which run on a worker thread, need
`handle_signals=False`:

- `create_job_service(*, handle_signals: bool = True)`; `dtc tui` passes
  `False`. `run-job` is unchanged.
- One `JobService` per app, as in M03, not one per job: `run()` resets its
  state on each call and refuses a second concurrent call, and `preview()`
  (the detail view) touches no run state, so both can share it.
- `dtc tui` gains `--shutdown-grace SECONDS` (default 10, as for `run-job`;
  negative is rejected with exit code 2, as `run-job` does), passed to the
  `App` and on to `JobService.run`.

### Starting a job

- `x` on the job list (selected row) or in the detail view starts the flow.
  The job file is read again on a worker, with
  `read_job(..., decode_input=False)` as `run-job` does, so an edit since
  the list was read is used and a slow volume does not freeze the UI. An
  invalid job shows its error in a notification, and nothing starts.
- A confirmation dialog shows the job name, mode, run count, the cooldown
  line (`cooldown_details`), the seed (or `random (drawn when the job
  starts)`), the output directory, and the executable. `y` or Enter
  starts, `n` or Escape cancels. The `JobDefinition` read for the dialog is
  the one that runs; the file is not read again after confirming.
- While a job is running, `x` shows "A job is already running" and opens
  nothing, and the list's status column shows `running` for that job.
- On confirm, the live view opens ("Starting...") and a thread worker does
  what `run-job` does, in its order:
  1. take `RunLock("tui")`; `RunLockBusy` or `RunLockError` ends the start
     with its message (for example "Another run is in progress (run-job,
     PID 4123). Try again when it finishes."), and nothing runs. A CLI
     refused by the TUI's lock names it as `tui`;
  2. open a `Store` on the worker thread (it keeps one connection per
     thread, so the worker opens, uses, and closes its own) and call
     `sweep_interrupted()`; a store error ends the start with its message;
  3. `JobService.run(job, executable=..., shutdown_grace=...,
     write_records=settings.write_job_records,
     observer=combine_observers(ExecutionRecorder(store), post_event),
     on_child_start=lock.record_child)`;
  4. close the store and release the lock, whatever happened, and post
     `JobWorkerEnded` last.
- The worker catches every exception and posts it in `JobWorkerEnded`; it
  never raises into Textual, since a failed worker closes the app.
  `ValueError` before `JobStarted` (a missing tool, an input that cannot be
  resized) records nothing, since the recorder starts on `JobStarted`; the
  view shows "Did not start: <message>". An exception after `JobStarted`
  arrives after `JobService`'s own `JobFinished(status="failed")`, and the
  view shows the message under the failed status.
- A job counts as running from confirmation until `JobWorkerEnded`, not
  until `JobFinished`: the lock is released after `JobFinished`, so an
  immediate `x` in between would otherwise meet this job's own lock.

### Events to the screen

Every event, including `RunOutput`, is emitted on the worker thread: the
runner reads the child's streams on reader threads but drains them, and
calls `on_message`, on the thread that called `run()`. The observer must not
touch widgets:

- `post_event` wraps each `JobEvent` in a Textual message and sends it with
  `App.post_message`, which is thread-safe, does not block, and returns
  `False` without raising once the app is closing. Not `call_from_thread`:
  it blocks the worker until the UI has handled the event, and raises once
  the app is gone. Order is kept, and `JobWorkerEnded` is posted the same
  way, so it always arrives after the job's last event.
- The app, not a screen, handles the messages and applies them to a
  `LiveRun` model (new `tui/live_run.py`: plain data, updated on the main
  thread only; `apply` asserts it runs on the thread that created the
  model, which is how tests prove no worker touches the UI). The live screen
  renders from the model when it mounts and on each change, so leaving the
  view during a run and coming back loses nothing.
- `LiveRun` holds: the job name and file; its phase (`starting`, `running`,
  `cooling_down`, `stopping`, `finished`, `not_started`); the `JobStarted`
  values (mode, seed and its source, cooldown, total runs, manifest and log
  paths when written); each run's number, pair, status (`pending`,
  `running`, `succeeded`, `failed`, `timed_out`, `interrupted`), seconds,
  and output name; the active run's start (monotonic time when its
  `RunStarted` was applied), output name, redacted command, and latest
  progress; the cooldown's seconds and end time; the last 2000 output lines;
  whether a stop was requested; the `JobFinished` values; and the worker's
  error, if any.
- Progress lines are not added to the output. `draw-things-cli` redraws its
  progress bar as a new line per step, which `OutputProcessor` passes on as
  `RunOutput` with `progress` or `percent` set; they update the active
  run's progress instead, so a long run does not push every other line out
  of the 2000. The CLI's log and the job log file keep every line, as now.

The view (`LiveRunScreen` in `screens.py`) shows:

- A header line: job name, mode, seed (the real one, from `JobStarted`), and
  phase.
- A run table: number, pair, status, seconds.
- The active run: elapsed time (a 1-second `set_interval` timer, not
  events), output name, step progress (`3/8, 37%`) when the child reports
  it, and the redacted command from `RunStarted`.
- A cooldown panel while waiting: seconds left and the local end time,
  counted down by the same timer from `CooldownStarted`.
- An output pane (`RichLog` with `max_lines=2000`) with the child's lines;
  stderr is styled apart from stdout. Lines are written as `rich.text.Text`,
  never as markup (child output and prompts contain brackets; see the M03
  review).
- When the job ends: the final status, completed runs, exit code, the
  stopping signal, if any, and the manifest and log paths, if written.

### Stopping

- `s` in the live view asks for confirmation, then calls
  `JobService.cancel(signal.SIGINT)`, so the outcome is the one Ctrl-C gives
  in the CLI: status `interrupted`, exit code 130, no later run started. A
  cooldown ends at once (M01's wake pipe).
- The view shows "Stopping..." until `JobWorkerEnded`. `s` again shows
  "Already stopping" and sends nothing. `s` with no job running does
  nothing.

### Leaving and reopening the view

- Escape goes back to the job list; the job keeps running.
- `l` on the job list opens the live view of the running job, or of the last
  job started in this session (including one that did not start), until the
  next one starts. With neither, it shows "No job has run in this session".

### Quitting during a run

- `q` or Ctrl-C while a job runs opens a dialog: "Stop the job and quit" or
  "Stay". Stop and quit calls `cancel(signal.SIGINT)`, shows "Stopping...",
  and exits on `JobWorkerEnded` (at most about the shutdown grace, since the
  runner then kills the child's process group). While stopping, `q` and
  Ctrl-C only repeat "Waiting for the job to stop"; there is no forced quit
  that would leave the child running. A second quit dialog is never stacked
  on the first.
- With no job running, `q` and Ctrl-C quit at once, as in M03. `dtc tui`
  exits with 0 after either.

### Signals while a job runs

Textual installs no handler for `SIGHUP`, `SIGTERM`, or `SIGINT` (checked
against the installed 8.2.8 drivers). Unhandled, `SIGHUP` (the terminal
closed) or `SIGTERM` kills `dtc` and leaves `draw-things-cli`, which runs in
its own session, still running; the M02 orphan guard then blocks the next
start until it ends. So:

- The app registers `SIGHUP`, `SIGTERM`, and `SIGINT` with the asyncio loop
  (`loop.add_signal_handler`) on mount and removes them on unmount. The
  handler runs as an event-loop callback, not inside arbitrary code, so it
  can safely call `cancel(received_signal)` first, before anything touches
  the terminal, and then quit when `JobWorkerEnded` arrives. The job is
  recorded as stopped by that signal, exit code 128+N, as in the CLI.
- With no job running, a signal quits the app. Either way, `dtc tui` exits
  with 128+N after a signal. A signal that arrives while the job is already
  stopping changes nothing: the exit code is that of the signal that stopped
  it (130 after `s`).
- If the app has already gone while its job stops, the signals stay handled,
  and ignored, until the worker ends; then the previous handlers are
  restored.
- `SIGKILL` cannot be handled. The operating system releases the lock, the
  child keeps running, and the next start is refused until it ends (M02).
  The execution stays `running` in the store until the next start sweeps it
  to `interrupted`.

### The app ending during a run

Textual runs thread workers on asyncio's default executor, and Python waits
for those threads at exit. If the app ends while a job runs for any reason
other than the paths above (an unexpected exception, a terminal write that
fails after `SIGHUP`), `dtc` would stay alive in the background and the job
would run to its end with nothing on screen. As a backstop, the app's
`on_unmount` calls `job_service.cancel(signal.SIGINT)` when a job is
running, recording `SIGINT` as the stop first, so a worker that has not
reached `JobService.run` yet cancels on `JobStarted`. It has to be there: `App.run()` uses `asyncio.run`, which waits for
the executor's threads (up to 300 seconds) before `run()` returns, so a
cancel placed only after `run()` would come after the job had run on.
`tui_command` still calls `cancel(signal.SIGINT)` in a `finally` after
`run()`, for an app that fails before it mounts; it does nothing when no job
runs. The worker then ends the job within the shutdown grace, releases the
lock, and the process exits.

### Logging

The TUI keeps M03's rule: no stdout or stderr Loguru sinks while the app
runs. `JobService` still logs; with `write_job_records` true, the job log
file gets those lines as in the CLI, through its own sink. An observer that
raises is logged by `notify` and ignored, so a UI error cannot stop a job.

### Keys

| Key | Where | Action |
|-----|-------|--------|
| Up/Down, `j`/`k` | List | Move in the list |
| Enter | List | Open the detail view |
| `x` | List, detail | Run the job, after confirmation |
| `l` | List | Open the live view of the running or last job |
| `s` | Live view | Stop the job, after confirmation |
| Escape | Detail, live view | Back to the list (a running job continues) |
| `r` | List | Refresh the list |
| `?` | Anywhere | Help |
| `q`, Ctrl-C | Anywhere | Quit; while a job runs, asks to stop it first |

The help screen and the user guide's key table are updated to match.

### Files

- `tui/live_run.py`: the `LiveRun` model, `apply(event)`, and the message
  classes (`JobEventMessage`, `JobWorkerEnded`).
- `tui/screens.py`: `ConfirmScreen` (a `ModalScreen[bool]`, reused for run,
  stop, and quit), `LiveRunScreen`, the `x` and `l` bindings.
- `tui/widgets.py`: the run table, the progress and cooldown text.
- `tui/app.py`: the job worker, the message handlers, the signal handlers,
  quit during a run, `shutdown_grace`, and the unmount backstop. The job
  file read for `x` reaches the app as a message (`ReadForRun`,
  `ReadForRunFailed`), like the job's events.
- `cli/app.py`: `create_job_service(handle_signals=...)`,
  `--shutdown-grace` on `tui`, the cancel backstop.

Textual documentation is checked through Context7 first: `post_message`
from a thread, `RichLog` (`max_lines`, writing `Text`), `ModalScreen`
results, `set_interval`, and signal handlers on the loop Textual runs.

## Tests

- A fake runner factory whose runners emit output lines (including progress
  lines) through `on_message`, report a PID through `on_start`, and, for the
  stop tests, block until `request_shutdown` (as `BlockingRunner` in the
  job service tests does).
- A temporary state directory (`run_lock.STATE_DIRECTORY` patched, as the
  CLI tests do); a busy lock is held by a second `RunLock` on the same file,
  which `flock` treats as another holder.
- Signal handling: the handler is called directly for each signal, and one
  subprocess test starts a headless app with a fake runner, sends it
  `SIGTERM` during a run, and checks the exit code, the store row, and the
  lock file. No test sends a signal to the test process itself.
- The backstop: a test ends the app during a fake run without the quit
  dialog and checks the job was cancelled and the lock released.
- Stdout and stderr: the app runs with Loguru's sinks removed, as
  `dtc tui` leaves them, while both streams are captured.
- The fake runs are in `tests/tui/fake_runs.py`; the tests are in
  `tests/tui/test_live_run.py`, and the `SIGTERM` test runs
  `tests/tui/sigterm_app.py` in a subprocess.

## Acceptance criteria

- With a fake runner, a job started from the TUI shows each run's state
  change in order, streams output, shows step progress and a cooldown
  countdown, and ends with the final status, completed runs, and exit code.
- The execution is recorded in the state store as `run-job` records it
  (same statuses, runs, and seed); a job that fails before `JobStarted`
  records nothing and shows its message.
- `s` during a run and during a cooldown ends the job with exit code 130,
  status `interrupted` in the store, and no later run started; a second `s`
  sends no second cancel.
- With the lock held by another holder, confirming a run shows the busy
  message and starts nothing; the lock is never left held after any start,
  failed or not, and `x` right after a job ends starts the next one.
- `q` and Ctrl-C during a run offer to stop it; stopping and quitting ends
  the job, releases the lock, and only then exits.
- `SIGTERM` to `dtc tui` during a run stops the job (status `interrupted`,
  exit code 143 for the job and for `dtc tui`), with the lock released.
- If the app ends during a run without the quit dialog, the job is
  cancelled and the process exits within the shutdown grace.
- Escape during a run and `l` back show the same state and output; `l`
  after the run shows the final state.
- The output pane never holds more than 2000 lines, holds no progress-bar
  lines, and shows bracketed output as written.
- `LiveRun` is only updated on the main thread (its assertion holds through
  a full headless run).
- No credential value appears on screen.
- Nothing is written to stdout or stderr while the TUI runs.
- Browsing writes nothing; running a job writes only what `run-job` writes.
- No test starts the real `draw-things-cli`.
- `make check` passes.
