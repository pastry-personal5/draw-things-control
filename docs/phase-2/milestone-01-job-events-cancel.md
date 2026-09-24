# Milestone 01: Job Events and Cancellation

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** planned
**Depends on:** [Phase 1](../phase-1/README.md)

## Goal

Make `JobService` usable from something other than the command line. Today it
reports progress only as log text and files, and it can be stopped only by a
process signal on the main thread. A TUI (this phase) and a server (Phase 3)
need structured progress and a stop that works from any thread.

## Scope

In scope:

- A typed event stream from `JobService`
- A `cancel()` method with the same outcome as a signal
- A way to run a job without installing process signal handlers
- Moving the CLI's log lines into one event observer, with no change in
  output

Out of scope:

- Any state store, lock, or UI (Milestones 02 to 05)
- Changing what a job does, its manifest, or its log file format
- Cancelling a job that was not started through `JobService`

## Planned changes

### Events

A new module, `jobs/job_events.py`, holds frozen dataclasses:

| Event | When | Main fields |
|-------|------|-------------|
| `JobStarted` | Before run 1 | job name, mode, total runs, seed and its source, cooldown and its source |
| `RunStarted` | A run's command is launched | run number, total, pair name, output path, redacted command |
| `RunOutput` | The child prints a line | run number, stream (`stdout` or `stderr`), text |
| `RunFinished` | A run ends | run number, status, exit code, seconds |
| `CooldownStarted` | A wait begins | after run number, seconds, local "until" time |
| `CooldownEnded` | A wait ends | seconds waited, whether it was cut short |
| `JobFinished` | The job ends | status (`succeeded`, `failed`, `interrupted`), exit code, the signal if any |

`JobService.run()` takes an `observer: Callable[[JobEvent], None]` argument
(optional; the default ignores events). The service calls it on the thread
that runs the job, so an observer for a UI must hand events to its own
thread. An exception raised by an observer is logged and swallowed: a broken
display must never stop a generation.

`RunStarted.command` is redacted with `GenerationService.redact_command`
before it is put in an event.

### Child output

Today the child's lines reach the log only through Loguru
(`DrawThingsProcessRunner._drain_output`, tagged `child_stream`). Events must
not be built by parsing log text, so the runner gains an optional
`on_output(stream, text)` callback, called from the same place that logs each
line. `JobService` passes a callback that emits `RunOutput`. The Loguru
output is unchanged, so the CLI and the job log file behave as before.
`GenerationService` and the runner factory carry the callback through
(`RunnerFactory` gains an optional argument).

### Logging and the terminal

Loguru's sinks are process-global. `cli/app.py`'s `configure_logging()` adds
stdout and stderr sinks, which would write over a full-screen UI. Front ends
that own the terminal (the Phase 2 TUI) must not install them. The job log
file sink (`add_job_log`) is a file and stays as is.

### Cancellation

- `JobService.cancel(received_signal=signal.SIGTERM)` is safe to call from
  any thread. It does what the job's signal handler does now: set the
  interrupt flag, forward the stop to the current runner with
  `request_shutdown`, and end a cooldown wait at once.
- The cooldown wait must be woken without a signal. The wake-up pipe from
  phase 1 (Milestone 04) is kept for the signal path; `cancel()` writes a
  byte to the same pipe, so the wait wakes in both cases. When no wait is in
  progress, `cancel()` only sets the flag.
- The outcome is the same as a signal: no later run starts, the job is
  `interrupted`, and `JobFinished` names the signal used.

### Running without signal handlers

`JobService.run()` gains `install_signals: bool = True`. When it is false,
the service does not call `signal.signal` or `signal.set_wakeup_fd`. That is
required off the main thread, where both raise `ValueError`. The TUI runs
jobs in a worker thread with `install_signals=False`.

### The CLI as an observer

A `LogObserver` (in `jobs/job_events.py` or a small `jobs/job_log_observer.py`)
produces the same log lines that `JobService` writes today. `cli/app.py`
passes it to `run()`. Job log files and manifests are unchanged.

## Acceptance criteria

- `run-job` output, log file, manifest, and exit codes are byte-for-byte
  what they were before, for success, failure, timeout, Ctrl-C, and a signal
  during a cooldown.
- A test with a fake runner receives the expected event sequence for a
  three-run job with a cooldown, for a failed run, and for a cancelled job.
- `cancel()` called from another thread ends a running fake run and a
  cooldown wait, with no real signal sent.
- `run(..., install_signals=False)` works from a non-main thread.
- `RunOutput` events carry the child's lines in order, from the runner's
  callback, and a run with no callback behaves as before.
- An observer that raises does not change the job outcome.
- No event contains a credential value.
- `make check` passes.
