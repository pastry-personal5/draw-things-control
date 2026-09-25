# Milestone 01: Queue and Run Manager

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Phase 2](../phase-2/README.md), Milestones 01 and 02

## Goal

Run jobs one after another from a persistent queue, with the same
cooldown between jobs as between runs, and recover cleanly from a crash or
restart.

## Scope

In scope:

- A queue of jobs in the state store
- One worker that runs them, holding the run lock
- Job states, cancellation, and the cooldown between jobs
- Restart recovery and an explicit resume
- Support in `JobService` for starting mid-chain (needed by resume)

Out of scope:

- The HTTP and MCP interfaces (Milestones 02 and 04)
- Writing job files (Milestone 03)
- Priorities, scheduling for a time, or parallel workers

## Planned changes

### Queue

- A `queue` table in the state store: id, job name, job file, the job's
  exact YAML text and the settings it was resolved with (as in Phase 2's
  `job_runs`), state, position, submitted at, started at, finished at,
  the linked `job_runs` id, resume-of (a queue id or null), and an error
  message. Schema is a forward migration of the Phase 2 store.
- Submitting takes a job file name, validates it, and stores the text and
  settings. The stored text, not the file, is what runs: the worker parses it
  when the job starts with a new `load_job_text(text, global_config, ...)`
  in `jobs/job_definition.py`, which shares all validation with `load_job`
  (which becomes a thin wrapper that reads the file). Input files are
  checked when the job starts, so a file removed in the meantime fails the
  job, naming the path, before any run begins.
- States:

  | State | Meaning |
  |-------|---------|
  | `queued` | Waiting |
  | `running` | Its runs are in progress |
  | `succeeded`, `failed` | Finished |
  | `cancelled` | Removed while queued, or stopped while running |
  | `interrupted` | The process died or the server restarted mid-job |

  The wait between jobs is not an entry state: the finished entry stays
  `succeeded` or `failed`. The queue itself reports `cooldown_until` (null
  when not waiting), so clients can show the wait.
- FIFO order. Position is the submission order.
- Finished entries (`succeeded`, `failed`, `cancelled`, `interrupted`) are
  pruned with the rest of the history under `history_retention_days`
  ([Phase 2, Milestone 02](../phase-2/milestone-02-state-store-run-lock.md#retention)).
  `queued` and `running` entries never are. A pruned entry can no longer be
  resumed.

### Worker

- One worker thread inside the server process. The server takes the run lock
  at startup and keeps it for its whole lifetime, so the CLI and the TUI
  refuse to start runs while the server is up, even when the queue is idle
  (owner decision). To run a job by hand, stop the server. Both remain
  usable for browsing and history, and the busy message names `serve` as
  the holder.
- It takes the oldest `queued` entry and runs it with
  `JobService.run(observer=...)`, recording runs
  through the Phase 2 recorder.
- After a job finishes, the worker waits the finished job's resolved
  `cooldown_seconds` (from the snapshot) before starting the next one, using
  the interruptible wait. No wait when the queue is empty or the job failed
  or was cancelled. The wait ends at once on cancel or shutdown.
- Failure of one job does not stop the queue.

### Cancel

- `cancel(id)` on a `queued` entry marks it `cancelled` and it never starts.
- On the `running` entry it calls `JobService.cancel()`; the job becomes
  `cancelled` (not `interrupted`, which is reserved for crashes). The worker
  moves on to the next entry with no cooldown wait after a cancel.
- Cancel on a finished entry is an error that names its state.

### Restart recovery

On startup, before the worker starts:

- Entries `running` become `interrupted`, and their
  `job_runs` row is closed as `interrupted`.
- Entries `queued` stay queued and run in order.
- Nothing runs that was interrupted, until someone resumes it.

### Resume

- `resume(id)` is allowed for an `interrupted` (or `failed`) entry that
  has at least one succeeded run. It creates a new `queued` entry with
  `resume-of` set.
- The new entry starts at the first unfinished run. Its first input is the
  last frame (video) or output (image) of the last succeeded run, and it
  keeps the seed and run numbering of the original.
- It is refused, naming the missing path, when that file is gone. It is
  refused when the entry has no succeeded run (submit the job again), and
  when a resume of it is already queued.
- `JobService.run()` gains a `start_run` argument and an input override, so
  a chain can begin at run *k*. The manifest and record show the resumed
  numbering and that the job is a resume.

### Shutdown

On `SIGINT` or `SIGTERM` the server stops the current job through
`cancel()`, marks it `interrupted` (it did not finish and can be resumed),
leaves queued entries queued, releases the lock, and exits.

## Acceptance criteria

- With a fake runner, two queued jobs run in order, with the first job's
  cooldown between them, and no cooldown after the last job.
- Cancel works on a queued job, on a running job, and during the
  between-jobs cooldown; each ends in the right state.
- Killing the server process mid-job and starting it again leaves that job
  `interrupted` and its queued successors queued and running.
- Resume of a job with three succeeded runs of seven starts at run 4 with run
  3's last frame, and finishes as a chain with the original's seed; a resume
  with a deleted last frame is refused with the path.
- Editing or deleting the job's YAML after submission does not change the
  running job.
- The CLI and TUI refuse to run while the server holds the lock (exit 75).
- `make check` passes.
