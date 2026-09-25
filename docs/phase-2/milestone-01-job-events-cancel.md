# Milestone 01: Job Events and Cancellation

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Phase 1](../archive/phase-1/README.md)

## Goal

Make `JobService` usable from something other than the command line. Today it
reports progress only as log text and files, and it can be stopped only by a
process signal on the main thread. A TUI (this phase) and a server (Phase 3)
need structured progress and a stop that works from any thread.

## Scope

In scope:

- A typed event stream from `JobService`, rich enough for Milestone 02's
  recorder and Milestone 04's live view to work from events alone
- A `cancel()` method with the same outcome as a signal
- Running a job off the main thread without process signal handlers
- Keeping the job's `source_text` so a recorder can store exactly what ran

Out of scope:

- Any state store, lock, or UI (Milestones 02 to 05)
- Changing what a job does, its manifest, or its log file format
- Moving or changing any log line: `JobService` keeps writing them, so the
  CLI output and every job log file stay identical for all front ends
- Cancelling a job that was not started through `JobService`

## Planned changes

### Events

A new module, `jobs/job_events.py`, holds frozen dataclasses and the union
type `JobEvent`. Every event has `at`, a local ISO 8601 timestamp from the
service's injectable clock, so tests are deterministic and a recorder does not
read the clock itself.

| Event | When | Main fields |
|-------|------|-------------|
| `JobStarted` | After the seed is drawn and the output directory exists, before run 1 | job name, job file, `source_text`, mode, total runs, output directory, input, model, seed and its source, cooldown seconds and its source, manifest path and log path (or `None`) |
| `RunStarted` | A run's command is launched | run number, total, pair name, positive, negative, input, output, last frame path (video) or `None`, redacted command |
| `RunOutput` | The child prints a line | run number, stream (`stdout` or `stderr`), text, `progress` as `(step, steps)` or `None`, `percent` (0-100) or `None` |
| `RunFinished` | A run ends, by any path | run number, status (`succeeded`, `failed`, `timed_out`, `interrupted`), exit code, seconds, output name kept or `None`, last frame name or `None` |
| `CooldownStarted` | A wait begins | after run number, seconds, local "until" time |
| `CooldownEnded` | A wait ends | seconds waited, whether it was cut short |
| `JobFinished` | The job ends, by any path | status (`succeeded`, `failed`, `interrupted`), exit code, completed runs, the signal if any |

Rules:

- **Always closed.** Once `JobStarted` is sent, `JobFinished` is sent exactly
  once, and a launched run gets a `RunFinished`, including when `run()` raises
  (a runner that cannot start, a bug, `KeyboardInterrupt` without handlers):
  status `failed`, and the exception then continues to propagate. Milestone
  02 relies on this, since a `running` row is otherwise how a crash shows.
  Errors before `JobStarted` (missing executable, bad job) raise as today and
  send no events.
- **Redaction.** `RunStarted.command` goes through
  `GenerationService.redact_command`. No event carries a credential value.
- **Threading.** `JobService.run()` takes `observer: Callable[[JobEvent], None]`
  (optional; the default ignores events) and calls it on the thread that runs
  the job, including `RunOutput`, because the runner drains child output on the
  thread that called `run()`. An observer for a UI must hand events to its own
  thread. An exception raised by an observer is logged and swallowed: a broken
  display must never stop a generation.
- **Composition.** `combine_observers(*observers)` returns one observer that
  calls each in order, isolating failures per observer. Milestone 02's recorder
  and a UI observer are combined with it.

### Job source text

`JobDefinition` gains `source_text: str`, the file's text read once in
`load_job`, left out of equality and repr. Job files reject unknown keys, so
the text cannot hold a credential. `JobStarted` carries it, so history shows the job as it ran even
if the file is edited later.

### Child output

`draw-things-cli` draws its progress bar with terminal codes: one bare newline
before the first bar, then `ESC[1A ESC[K` (cursor up, clear line) and a line
such as `Sampling... 3 / 20 [█] 15%` for every update. Model downloads redraw
one line with `\r`. `OutputProcessor` strips these codes, drops lines that are
empty afterwards, and parses the step counter (`progress`) and the percentage
(`percent`) from the clean text, so logs, manifests, and events never hold raw
escape sequences. A `[1/3]` file counter is not read as a step counter.

Events must not be built by parsing log text. `OutputProcessor` already takes a
`callback` (a `MessageCallback`) that it calls with each `ProcessMessage`
(stream, text, elapsed, and parsed `progress` and `percent`) right where it logs the line. So
the runner needs no new hook. What is missing is a way to give the factory the
callback:

- `RunnerFactory` (in `core/generation_service.py` and `jobs/job_service.py`)
  is a `Protocol` whose call takes a fourth argument,
  `on_message: MessageCallback | None = None`, always passed (`None` when
  nothing observes the job), so a factory with the wrong shape fails in a type
  check, not on the first observed job. `GenerationService.execute` gains a
  matching optional argument and passes it through.
- `cli/app.py`'s `create_runner` builds `OutputProcessor(callback=on_message)`.
- `JobService` passes a callback that emits `RunOutput`.

Loguru output is unchanged. A run with no callback behaves as before.

### Logging and the terminal

Loguru's sinks are process-global. `cli/app.py`'s `main()` calls
`configure_logging()` before any command, which adds stdout and stderr sinks
that would write over a full-screen UI. This milestone changes nothing there;
the `tui` command (Milestone 03) must call `logger.remove()` before it starts
Textual, and its tests assert no console sink remains. The job log file sink
(`add_job_log`) is a file and stays as is. `JobService` still writes every
job and run line, so a TUI-run job has the same log file as a CLI-run one.

### Cancellation

- `JobService.cancel(received_signal=signal.SIGTERM) -> bool` is safe to call
  from any thread. While a job is running it does what the signal handler does
  now: set the interrupt flag, forward the stop to the current runner with
  `request_shutdown`, and end a cooldown wait at once; it returns `True`. When
  no job is running it does nothing and returns `False`, so a stale cancel can
  never stop a later job.
- One `run()` at a time per `JobService` instance: the interrupt flag and the
  current runner are instance state. A second concurrent `run()` raises
  `RuntimeError`. The CLI keeps its module-level instance; the TUI makes one
  per job.
- **Races.** `_create_runner` already stores the runner and then checks the
  flag; `cancel()` sets the flag and then reads the runner. Keep that order,
  so a cancel landing between runner creation and `run()` still reaches the
  runner. A test covers it.
- **Waking a cooldown.** Today `interruptible_wait` creates its wake-up pipe
  inside the call, so nothing outside can write to it. It gains an optional
  `wake_fd: int | None` parameter: the non-blocking read end of a pipe the
  caller owns, which the wait also `select`s on. `JobService` creates one
  pipe per `run()`, passes its read end to the default cooldown, and closes
  both ends when `run()` ends. `cancel()` writes a byte to the write end,
  under the same lock that guards the pipe's lifetime, so it can never
  write to a closed or reused descriptor. A full pipe is ignored, since one
  byte is enough. The flag is set before the byte is written and checked
  again after `select`, so a cancel that arrives before the wait starts
  still ends it at once. The signal path is unchanged.
  Injected `cooldown` callables (tests) are not woken by `cancel()`; they see
  the flag through their own means.
- **Signal identity.** As today, a later signal overwrites the earlier one, so
  exit codes do not change. `JobFinished.signal` and the exit code use the
  last one.
- **Late cancel.** As with a signal, a cancel that lands after the last run
  has finished changes nothing: the job still ends `succeeded`, though
  `cancel()` returned `True`. `True` means the stop was requested, not that
  the job ended `interrupted`.
- The outcome is the same as a signal: no later run starts, the job is
  `interrupted`, and `JobFinished` names the signal used.

### Running off the main thread

No new `run()` parameter. `JobService` already takes `handle_signals`; the TUI
builds its service with `handle_signals=False`, and the process runner is
already created with signal handling off for jobs. `install_signal_handlers`
already returns `None` off the main thread and `interruptible_wait` already
tolerates `set_wakeup_fd` failing there, so this milestone adds a test that
proves a job runs to completion, and cancels, from a worker thread, and fixes
anything that test finds.

## Documentation

In the same change: update the Phase 2 bullet in
[architecture.md](../architecture.md) (events, `cancel()`, no `LogObserver`),
the code-layout paragraph in the [phase document](README.md), and the recorder
description in [Milestone 02](milestone-02-state-store-run-lock.md) (drop "composed
with the CLI's log observer"; it is combined with a UI observer instead). Add
the entries listed in [phase-2-changelog.md](phase-2-changelog.md) dated the
day it lands.

## Acceptance criteria

- `run-job` output, log file, manifest, and exit codes are byte-for-byte
  what they were before, for success, failure, timeout, Ctrl-C, and a signal
  during a cooldown. Existing tests pass unchanged.
- A test with a fake runner receives the expected event sequence for a
  three-run job with a cooldown, for a failed run, for a timed-out run, and
  for a cancelled job (during a run and during a cooldown).
- Events carry every field Milestone 02 stores per execution and per run, except
  what the store computes itself.
- When the runner raises, the observer still receives `RunFinished` (failed)
  and `JobFinished` (failed), and the exception propagates.
- `cancel()` called from another thread ends a running fake run and a
  cooldown wait, with no real signal sent, and returns `True`; called with no
  job running it returns `False` and does not affect the next job.
- A cancel between runner creation and `run()` still stops that run.
- A second concurrent `run()` on one service raises `RuntimeError`.
- A job runs, and can be cancelled, from a non-main thread with
  `handle_signals=False`.
- `RunOutput` events carry the child's lines in order, with `progress` where
  the line has it, from `OutputProcessor`'s callback; a run with no callback
  behaves as before.
- An observer that raises does not change the job outcome or stop the other
  observers passed to `combine_observers`.
- No event contains a credential value.
- `make check` passes.
