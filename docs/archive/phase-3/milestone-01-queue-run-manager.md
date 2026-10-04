# Milestone 01: Queue and run manager

**Phase:** [Phase 3: API server and MCP server for AI](README.md)
**Status:** done
**Depends on:** [Phase 2](../phase-2/README.md): [Milestone 01](../phase-2/milestone-01-job-events-cancel.md) (events and `cancel()`), [Milestone 02](../phase-2/milestone-02-state-store-run-lock.md) (state store and run lock), and [Milestone 11](../phase-2/milestone-11-clean-architecture.md) (`services/`, `JobRunSession`)

## Goal

Run jobs one after another from a persistent queue, with the same
cooldown between jobs as between runs, recover cleanly from a crash or
restart, and continue an interrupted chain from where it stopped.

## Scope

In scope:

- A queue of jobs in the state store, each entry a snapshot of what will run
- One worker that runs them while the server holds the run lock
- Entry states, cancellation, and the cooldown between jobs
- Restart recovery and an explicit resume
- Support in `JobExecutor` for starting a chain at run *k* (needed by resume)

Out of scope:

- The HTTP and MCP interfaces (Milestones 02 and 10), and the `serve`
  command that starts the worker (Milestone 02)
- Writing job files (Milestone 07)
- Priorities, reordering, scheduling for a time, or parallel workers
- Resuming an execution that `run-job` or the TUI started: those have no
  snapshot of their base configuration (see [Snapshot](#snapshot))
- The queue in the CLI and the TUI ([Milestone 03](milestone-03-queue-for-people.md))

## Planned changes

### Queue

- A `queue` table in the state store (schema 4, a forward migration). Each
  entry has a public ID, `Q` and at least four digits (`Q0007`), from a
  `queue` counter beside `execution` and `job`, so a number is never given
  twice and the store's row number stays internal, as for executions. It
  keeps: the job file's name, the [snapshot](#snapshot), the state, when it
  was submitted, started, and finished, its execution (once the job starts),
  the entry it resumes (or none), its resolved [resume point](#resume) when
  it is one, and an error message.
- Order is first in, first out, by ID. There is no position column: nothing
  reorders the queue.
- Submitting (a service in `services/`, called by the API) takes a job file,
  validates it, and stores the snapshot. The front end applies its own rules
  first (the API's are in
  [Milestone 02](milestone-02-http-api.md#rules-for-jobs-the-api-runs)).

### Snapshot

What runs is what was validated, whatever happens to the files or the
global configuration afterwards:

- the job file's exact text;
- the text of its base configuration from `data/params/`, since that
  decides the model, steps, and every other Draw Things setting of every run;
- the global configuration's `input_directory`, `output_directory`, and
  cooldown default, since those decide where a job's `input` and `output`
  resolve to and what a job with no `cooldown` of its own waits (owner
  decision: editing `config/global-config.yaml` and restarting the server
  must not change a queued entry, any more than editing a job file does);
- the settings it resolved to at submission (`ExecutionSettings`, as in
  Phase 2's `executions`), shown to clients.

When the job starts, the worker parses the snapshot with
`load_job_text(text, path, global_config, params_directory)` in
`jobs/parsing.py`, which gains a way to take the base configuration and the
global configuration from the snapshot instead of reading `data/params/` and
`config/global-config.yaml`. Editing or deleting the job file, its base
configuration, or the global configuration after submission changes nothing.
The job's `input` and `output` are resolved against the snapshotted global
configuration, and the input file is checked then: a file removed in the
meantime fails the entry, naming the path, before any run begins and
without an execution.

### States

| State | Meaning |
|-------|---------|
| `queued` | Waiting |
| `running` | Claimed by the worker; its runs are in progress |
| `succeeded` | Every run succeeded |
| `failed` | It could not start, or a run failed or timed out |
| `cancelled` | Cancelled while queued, or stopped by a cancel while running |
| `interrupted` | The server stopped or died while it ran |

- The execution in the history keeps its own four statuses: a cancelled
  entry's execution reads `interrupted`, as a Ctrl-C does. The worker knows
  which stop it asked for, so the entry says `cancelled` or `interrupted`;
  a run stopped from outside (its `draw-things-cli` killed) makes the entry
  `failed`.
- The worker links the entry to its execution when `JobStarted` arrives, by
  which time the recorder has written the execution's row.
- Finished entries are pruned with the rest of the history under
  `history_retention_days`
  ([Phase 2, Milestone 02](../phase-2/milestone-02-state-store-run-lock.md#retention));
  `queued` and `running` ones never are. An entry whose execution was
  pruned keeps no link to it and can no longer be resumed.

### Worker

- One worker thread, in `services/` (built on `JobRunSession`), so it has no
  web framework and is tested without one. The server takes the run lock at
  startup and keeps it for its whole lifetime, so the CLI and the TUI refuse
  to start runs while the server is up, even when the queue is idle (owner
  decision). To run a job by hand, stop the server; both remain usable for
  browsing and history. (Only until [Milestone
  03](milestone-03-queue-for-people.md): once it retires `run-job` and the
  TUI's direct run, queueing through `dtc queue` or the TUI's Queue widget
  becomes the only way to run a job at all, server up or down, and "stop
  the server to run a job by hand" stops being true.)
- The busy message says the server holds the lock and how to free it, not
  that a run is in progress: `The dtc server (PID 4123) holds the run lock
  while it is up; stop it to run a job by hand.` The TUI's Status widget
  says the same instead of `A job is running in another process`. (This
  message is itself replaced in Milestone 03, once there is no other way
  to run a job by hand to point to.)
- It claims the oldest `queued` entry (a state change that a concurrent
  cancel cannot interleave with) and runs it with
  `JobRunSession.run(observers=..., lock=...)`, which uses and leaves held
  the lock the server passes. The executor is built with
  `handle_signals=False`, since it runs off the main thread.
- A cancel that lands after the claim but before the executor has begun is
  kept and applied as the job starts, as the TUI does for a stop requested
  during start-up ([Phase 2 Milestone 04](../phase-2/milestone-04-tui-live-run.md)).
- A job's log file (`write_job_records`) holds that job's lines only. The
  job log is a Loguru sink, and today it copies every message of the
  process; in the server that would include API requests, so it is scoped
  to the job, child output included.
- An error that escapes a job fails its entry with the message, and the
  worker goes on to the next one. If the worker thread ever stops, the
  server's health reports it
  ([Milestone 02](milestone-02-http-api.md#endpoints)).

### Cooldown between jobs

- After a job that succeeded, when another entry is already queued, the
  worker waits that job's resolved `cooldown` applied to its last run's time
  (`CooldownPolicy.wait_after`, so `auto` waits a share of it) before
  starting the next one (owner decisions). No wait follows a job that
  failed, was cancelled, or was interrupted, and none starts when the queue
  is empty: a job submitted later starts at once.
- The wait lives in the worker, and `GET /queue` reports its end
  (`cooldown_until`). It uses the interruptible wait and ends at once on
  shutdown, so a restart starts the next queued job without it. A wait that
  finds no entry queued any more ends too.
- Cancelling during the wait cancels the entry named (a queued one), not the
  wait.
- Failure of one job does not stop the queue.

### Cancel

- `cancel(id)` on a `queued` entry makes it `cancelled`; it never starts.
- On the `running` entry it calls `JobExecutor.cancel()`: the current run
  and any cooldown between runs end at once. There is no transitional
  state: the entry stays `running` until `JobFinished` arrives, and then
  reads whatever that call actually caused (owner decision). Usually that
  is `cancelled`, and a [resume](#resume) reruns the lost run from its
  start; but a cancel that lands after the job's last run has already
  finished changes nothing (`JobExecutor.cancel()`'s own contract), so a
  race between a cancel and the job's natural end leaves the entry
  `succeeded` or `failed`, never `cancelled`, matching the execution's own
  final status. A second `cancel(id)` on an entry already stopping is a
  no-op, not an error: `JobExecutor.cancel()` is idempotent while a job is
  running.
- Cancel on a finished entry is an error that names its state.

### Restart recovery

On startup, while holding the lock and before the worker starts:

- A `running` entry whose execution is itself still `running` becomes
  `interrupted`, and the server calls `Store.sweep_interrupted` itself to
  close that execution as `interrupted`. `JobRunSession` sweeps only when a
  job starts, and while the server holds the lock the history shows a
  `running` row as running, so waiting for the next job would leave a
  crashed execution looking alive.
- A `running` entry whose execution already reads `succeeded` or `failed`
  (the server died after the run finished but before the entry's own row was
  updated) takes that status instead: forcing it to `interrupted` would make
  a finished chain look resumable at a run number past its end (owner
  decision).
- A `running` entry whose execution already reads `interrupted` (a graceful
  shutdown closed the execution, as ["Shutdown"](#shutdown) describes, but
  the process died before the entry's own row caught up) takes that status
  too, the same as the `succeeded`/`failed` case above.
- A `running` entry with no linked execution at all (the crash landed
  between the worker's claim and `JobStarted`, so the recorder's row was
  never written) is put back to `queued`, not `interrupted`: nothing of it
  ever ran, so nothing needs a resume, matching the 2026-09-25 owner
  decision that a job "queued and never started" is re-queued
  automatically. The worker therefore links the entry to its execution as
  early as `reserve_execution_id` fires, not only on `JobStarted`, so this
  window is as short as `JobRunSession` allows, not the whole run.
- `queued` entries stay queued and run in order.
- Nothing that was interrupted runs again until someone resumes it (owner
  decision).
- A server that starts while an earlier server's `draw-things-cli` is still
  alive (an orphaned child of a `SIGKILL`ed server) does not reach any of
  the above: `RunLock.acquire()` already refuses it, naming the PID, as it
  does for `run-job` and the TUI today ([Phase 2 Milestone
  02](../phase-2/milestone-02-state-store-run-lock.md#run-lock-corerun_lockpy)).
  The server's own crash recovery depends on this guard staying in place
  (owner decision, phase-3-changelog.md).

### Resume

What can be resumed is set by `draw-things-cli`
([research](../../research/draw-things-cli-resume.md)): a run writes its output
only when it has finished, and the CLI has no resume, checkpoint, or partial
output of its own. The smallest unit this project can resume is therefore
one run:

- A resume reruns the interrupted run from its start, and the time already
  spent on it is lost. Cancel and shutdown stop a run at once (owner
  decisions), so only a stop during a cooldown between runs loses nothing.
- A resume starts only from a succeeded run's file. A run killed mid-way may
  leave a truncated video at its output path, which the executor keeps (as
  since Phase 1) and the API marks incomplete
  ([Milestone 02](milestone-02-http-api.md#endpoints)); nothing reads it.
- The rerun keeps the chain's seed, input, and settings, but identical
  pixels are not promised: that was not verified.
- A run cut short while `draw-things-cli` downloads a model loses no
  download: the CLI continues it on the next run, since jobs never turn
  `--download-missing` off.
- Nothing is retried or resumed on its own: a failed run ends the job, and
  someone resumes it (owner decision).

The rules:

- `resume(id)` is allowed for an `interrupted`, `failed`, or `cancelled`
  entry. It creates a new `queued` entry that resumes it, with the same
  snapshot.
- `resume(id)` resolves the resume point once, at request time, and stores
  it on the new entry: the first run number, its starting input (that run's
  last frame or output), and the seed. Once accepted, the resume does not
  walk the chain again, so pruning an ancestor entry's execution after
  `resume(id)` has returned cannot invalidate an already-accepted resume
  (owner decision).
- The resume point comes from the last succeeded run of the chain: the
  entry's own, or, when it has none, that of the entry it resumed, and so
  on. It keeps the original's seed and run numbering, so run *k* of a resume
  is run *k* of the chain.
- The new entry gets the next queue ID, like any submission, and so lands
  at the back of the FIFO queue, behind whatever is already waiting: the
  general rule that nothing reorders the queue applies to a resume too
  (owner decision). It does not jump ahead of other queued work, even
  though it continues something already in progress.
- It is refused, naming the reason, when:
  - no run of the chain succeeded (submit the job again) — distinct from an
    entry that did have one, but whose execution was later pruned;
  - the entry already has a resume (resume the newest one instead);
  - the file it would start from, that run's last frame or output, is gone
    (the path is named);
  - the job's own first input is gone: parsing the snapshot resolves the
    job's size from it, as it did the first time (the path is named);
  - an entry in the chain, back to the last succeeded run, had its
    execution pruned after `history_retention_days` before the resolution
    above could read it (owner decision: finished entries are pruned with
    the history).
- `JobRunOptions` gains a resume point: the first run number, its input,
  and the seed. `JobRunSession.run` gains the same, since it builds
  `JobRunOptions` itself: the worker's only entry point into a run, so it
  is where the resume point actually reaches the executor. The executor
  starts the chain there, skips run 1's resized copy (run 1 is not run
  again), and numbers runs from *k*. `JobStarted`, the manifest, and the
  execution (a schema 4 column) record the first run and the execution it
  resumes (`E0012`); the history and the TUI's execution detail show both.
- A resumed execution's manifest holds only the runs it actually made (run
  *k* onward), at positions 0, 1, 2, ... of its `runs` list, so the list
  position is no longer the run number once a manifest can start above run
  1. `import-history` ([Phase 2 Milestone
  02](../phase-2/milestone-02-state-store-run-lock.md#history-import))
  takes a manifest's per-run `batch` field the same way it already ignores
  it for the plain case: it must number a resumed manifest's runs from its
  `first_run`, not from 1, or re-importing one after the database is lost
  (the case `import-history` exists for) would misnumber every run of it.

### Shutdown

The server's shutdown asks the worker to stop: it cancels the running job
at once (owner decision), ends any wait, and waits for the worker thread to
finish, so no `draw-things-cli` outlives the server. This build of
`draw-things-cli` dies on `SIGTERM` without cleanup, so the runner's shutdown
grace has nothing to wait for; it is kept for a build that handles the
signal. The job's entry becomes `interrupted`, and a resume reruns the run
it was in; queued entries stay queued. Only after the worker thread has
ended, its entry updated and its execution closed, does shutdown release
the run lock, so a second `dtc serve` (or `run-job`, or the TUI) started
right after never races the first one's own recovery of that entry. The
lock is released last.

## Acceptance criteria

All with a fake runner and a fake clock or wait:

- Two queued jobs run in order, the second after the first job's cooldown
  applied to its last run's time. No wait follows a failed or cancelled job,
  or a success with nothing queued: a job submitted then starts at once.
- Cancel works on a queued entry, on a running entry (in a run, in a
  cooldown between runs, and between the claim and the job's start), and on
  a queued entry during the between-jobs wait; each ends in the right state,
  and the running-entry cases leave the history's execution reading
  `interrupted` (a queued entry cancelled before or during the wait never
  started, so it has none).
- An entry left `running` by a killed server is `interrupted` after a
  restart, with its execution closed, and its queued successors run. An
  entry left `running` whose execution had already finished `succeeded`,
  `failed`, or `interrupted` before the crash takes that status instead,
  never a second, forced `interrupted`. An entry left `running` with no
  linked execution at all (the crash landed before `JobStarted`) is
  `queued` again after a restart, not `interrupted`, and runs normally.
- A resume of an entry with three succeeded runs of seven starts at run 4
  with run 3's last frame and the original seed, and finishes the chain; a
  resume of that resume, after it failed at run 5, starts at run 5 again,
  from run 4's last frame, never from run 5's leftover file. Each refusal
  above names its reason or path. A resume already accepted keeps its
  resolved starting input, run number, and seed, and still runs, even if the
  ancestor entry it depended on is pruned afterward. If a resumed
  execution's manifest is later imported by `import-history` (the database
  having been lost), its runs are numbered from its own first run, never
  from 1.
- Editing or deleting the job file, its base configuration, or
  `config/global-config.yaml`, after submission does not change what the
  entry runs. Submitting an invalid job file (a bad name, a missing key, a
  input file that does not exist) is refused, naming the reason, and stores
  no queue entry.
- While the worker holds the lock, `run-job` exits with 75 and names the
  server, and the TUI refuses `/apply` with the same message.
- Starting the server while an earlier server's `draw-things-cli` is still
  alive (its `dtc serve` was `SIGKILL`ed) refuses to start, naming the
  orphaned child's PID, and starts no worker.
- A job's log file holds none of the server's own lines.
- Stopping the server while an entry is running cancels it at once,
  ends any between-jobs wait immediately, leaves queued entries queued,
  and does not release the run lock until the worker thread has actually
  ended, so no `draw-things-cli` outlives the process and no second `dtc
  serve` can race the entry's own update to `interrupted`.
- An error that escapes one job (raised by a collaborator, not a normal job
  failure) fails only that entry, with the message, and the worker goes on
  to run the next queued entry rather than stopping.
- `make check` passes, and `docs/architecture.md` describes the queue and
  the worker. (The user guide describes them with `serve`, in Milestone 02.)
