# Milestone 04: Live Run View

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Milestone 03: TUI shell and job browser](milestone-03-tui-job-browser.md)

## Goal

Run a job from the TUI and watch it: which run is active, how long it has
taken, the cooldown countdown, and the output of `draw-things-cli`, with a
stop key that works at any moment.

## Scope

In scope:

- Starting the selected job from the TUI
- A live view driven by job events
- Stopping a running job or cooldown
- Handling a busy run lock and quitting during a run

Out of scope:

- Running more than one job, or queuing another job (Phase 3)
- Editing the job before running it
- Previewing the generated image or video

## Planned changes

### Starting a job

- `r` on a valid job asks for confirmation (job name, run count, total
  cooldown time), then starts it. Invalid jobs cannot be started.
- The job runs on a worker thread, using
  `JobService.run(..., observer=...)` (on a service built with
  `handle_signals=False`, one per job) from
  [Milestone 01](milestone-01-job-events-cancel.md). The TUI takes the run
  lock ([Milestone 02](milestone-02-state-store-run-lock.md)) before it
  starts the worker and releases it when the worker ends.
- Runs are recorded in the state store, exactly as `run-job` records them.
- If the lock is busy, the TUI shows the holder ("Another run is in
  progress (run-job, PID 4123)") and does not start.

### Events to the screen

The observer runs on the worker thread and must not touch widgets. It posts
each event to the app with `App.call_from_thread` (or a Textual message), and
the screen updates from that.

The view shows:

- A run list: each run's number, pair, and status (`pending`, `running`,
  `succeeded`, `failed`), with seconds for finished runs.
- The active run: elapsed time (updated by a timer, not by events),
  output path, and the redacted command.
- A cooldown panel while waiting: seconds left and the local time it ends,
  counted down by a timer from `CooldownStarted`.
- An output pane with the child's lines (`RunOutput`), kept to the last
  2000 lines so a long job does not grow without limit. Stdout and stderr
  are told apart by style.
- A footer with the job's overall state and exit code when it ends.

### Stopping

- `s` asks for confirmation, then calls `JobService.cancel()`. The view shows
  "Stopping..." until `JobFinished` arrives. The outcome is the same as
  Ctrl-C in the CLI (interrupted, later runs not started).
- Pressing `s` twice does not send a second cancel.

### Quitting during a run

`q` while a job runs asks whether to stop the job and quit, or stay. It never
leaves a generation running after the UI is gone. If the terminal closes or
the app is killed, the run lock is released by the operating system and the
child is stopped by the runner's own cleanup; the state store marks the job
`interrupted` the next time it opens (Milestone 02).

### Logging

The TUI does not install the CLI's stdout and stderr Loguru sinks (see
[Milestone 01, Logging and the terminal](milestone-01-job-events-cancel.md#logging-and-the-terminal)).
The screen is fed by events. The job log file, when `write_job_records` is
true, is written as in the CLI.

### Screens after a run

When the job ends, the view stays open on the final state until the user
goes back. The last job's result is one key away from the job list.

## Acceptance criteria

- With a fake runner, an execution from the TUI shows each run's state change
  in order, streams output, shows a cooldown countdown, and ends with the
  final status.
- `s` during a run and during a cooldown ends the job, with the job
  `interrupted` in the state store and no later run started.
- With the lock held by another process, `r` shows the busy message and
  starts nothing.
- `q` during a run offers to stop it; choosing to quit stops the job and
  releases the lock.
- The output pane never holds more than 2000 lines.
- No widget is touched from the worker thread (headless test with a fake
  runner runs the whole flow).
- No credential value appears on screen.
- Nothing is written to stdout or stderr by the app while the TUI runs (a
  test captures both during a full fake run).
- `make check` passes.
