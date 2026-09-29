# Milestone 03: Queue for People

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md), [Milestone 02: HTTP API](milestone-02-http-api.md)

## Goal

Give a person the server's queue from the CLI and the TUI, and retire every
other way to run a job: `dtc serve`'s worker becomes the only thing that
ever invokes `draw-things-cli` (owner decision). `run-job` is removed;
`dtc queue add` (with a new `--wait`) replaces it, and the TUI's `/apply`
submits to the queue instead of running the job itself. Both submit, list,
cancel, and resume through the API, and both can watch an entry update
live, over the gRPC monitoring service
([Milestone 02](milestone-02-http-api.md#monitoring-grpc)).

## Scope

In scope (owner decision):

- `dtc queue` subcommands, as a client of the API, including `add --wait`
- A Queue widget in the TUI that submits, cancels, and resumes through the
  API too, not only shows the queue; `/get queue`, and `/describe` for an
  entry
- The TUI's `draw-things-cli` pane, fed live over the same gRPC connection
  as the Queue widget, since the TUI no longer runs the child itself and so
  can no longer read its output directly
- Removing `run-job` and its direct-execution path in the TUI's `/apply`

Out of scope:

- Following a job live from any other command: `dtc queue`'s only gRPC use
  is `add --wait` watching the one entry it just submitted
- Running any job without the server, at all
- Resuming an execution recorded before this milestone, or any execution
  with no snapshot of its own (it predates the queue, or the server crashed
  before recording one)

## Planned changes

### Server-side changes (`state/`, `server/`, the gRPC service)

Everything below is a client of an API Milestone 02 already built; this section gathers what that API and the
gRPC service still need for this milestone, in one place, rather than leaving it implicit in the front-end
sections that follow.

- **Run counts on the queue itself (schema 6).** The Queue widget's rows, `dtc queue list`, and `dtc queue show`
  must all show "run 3/7" or "5 of 7 succeeded" for an entry that has not started yet, not only for one with a
  linked execution. `ExecutionRow.total_runs` and its own `succeeded` (a correlated-subquery column,
  `execution_rows.py`'s `SUCCEEDED_COUNT`) answer this once a job has an execution row, but a `queued` entry, and a
  `failed` one that never reached `JobStarted` (`_fail_to_start` clears its link), have none. Reparsing `job_text`
  per row on every list read (a YAML parse and a base-configuration merge) is real work this milestone should not
  add to a listing re-read on every relevant event. `NewQueueEntry` and `QueueRow` (`state/queue.py`) gain
  `total_runs: int`, set once from `job.run_count`: `submit_job` (`services/queue_submit.py`) already parses the
  job before storing its snapshot, so this reads a value already in hand. `resume_entry`
  (`services/queue_resume.py`) parses the resumed job only inside its `if before_submit is not None:` block today
  (`before_submit(job, job.run_count - chain.first_run + 1)`, the same `job.run_count` this reuses); since
  `total_runs` is needed on every resume, not only a checked one, that parse moves out of the conditional so a
  direct call (as tests that do not pass `before_submit` already make) still gets a `total_runs`. Either way it is
  the whole chain's count, unaffected by where a resume starts, matching `JobFinished.total_runs`'s own convention.
  Schema 6: `ALTER TABLE queue ADD COLUMN total_runs INTEGER`, backfilled for rows already in the database --
  `UPDATE queue SET total_runs = (SELECT e.total_runs FROM executions e WHERE e.execution_number =
  queue.execution_number) WHERE execution_number IS NOT NULL` -- which leaves `total_runs` NULL on a pre-migration
  row with no execution (a queued, or a never-started, entry): SQL cannot parse the stored YAML to recover it, so a
  handful of pre-existing such entries show no total until they are next resubmitted; every entry from this
  milestone on always has one.
- **Succeeded, per entry.** `queue_entry()` (`server/serializers.py`) gains `"total_runs": row.total_runs` and
  `"succeeded"`: 0 for an entry with no linked execution, `(row.resume_first_run or 1) - 1 + execution.succeeded`
  for one that has started -- the same `first_run - 1 + succeeded` convention `tui/text/history.py` and
  `JobLogWriter` already use for a resumed chain's true progress ([Milestone 01](milestone-01-queue-run-manager.md)
  design decision). `QueueRepository.list_active` and `.list_finished` (`state/queue.py`) gain the join needed to
  compute this without one query per row: `LEFT JOIN executions ON executions.execution_number =
  queue.execution_number`, selecting `SUCCEEDED_COUNT` and `executions.first_run` alongside the queue columns. A
  plain `executions.by_number()` per row, as `_last_run_seconds` already does for the single-entry endpoint, does
  not scale to a list of up to `max_queued_jobs` active entries plus a full page of history -- the same reasoning
  that moved `_queued_count` to a bare `COUNT(*)` in the Milestone 02 review. `GET /queue` and `GET /queue/{id}`
  both read this one serializer path, so `dtc queue list`, `dtc queue show`, and the Queue widget's rows agree; the
  TUI's own read-only fallback reader (below, "while it is down") runs the same joined query directly against the
  store, so the queue reads the same whether or not the server is up.
- **The gRPC target.** `dtc queue add --wait`, the TUI, and (Milestone 08) `dtc mcp` each need a gRPC client
  alongside their HTTP one, but every client today is configured with `--server-url` alone (an HTTP URL); nothing
  says how to reach `--grpc-port` (default 8766, `server/serve.py`, which need not equal the HTTP port) and no
  client-facing flag exists for it. Milestone 08's own plan already assumes `dtc mcp` "also holds a generated gRPC
  client" built from `--server-url` alone, without saying how -- the same gap, not new to this milestone.
  `GET /v1/health` gains `"grpc_port": options.grpc_port` (no new auth: health is already unauthenticated, and a
  port number is not a credential); every gRPC client this phase builds derives its target from the HTTP host it
  was already given, plus this field, needing no gRPC flag of its own. `--allow-remote-server` (already gating the
  HTTP target) covers the derived gRPC target too, since it names the same host.
- **`monitor.proto`:** `QueueEntrySnapshot` gains `optional int32 total_runs = 10`, set from the entry's own stored
  column in `MonitorServicer._snapshot` (`grpc_service.py`), so `add --wait`'s live output can print "run 3/7" the
  same way the Queue widget does (below, under "The `queue` commands", for how `--wait` reads a run's start and
  finish from the fields `WatchQueueEntry` already streams, with no further field needed for that). `run_summary`
  (Milestone 02's `server/serializers.py`) also gains `"pair": run.pair`, and `execution_detail` gains
  `"manifest": row.manifest_path` and `"log": row.log_path`: `state/execution_rows.py`'s `RunRow` and
  `ExecutionRow` already carry all three; `GET /executions/{id}` simply never served them, and the TUI's live-output
  seeding (below) needs them to rebuild a running job's full state from this one endpoint.

### The `queue` commands

| Command | API |
|---------|-----|
| `dtc queue add JOB [--wait]` | `POST /queue`, then `WatchQueueEntry` if `--wait` |
| `dtc queue list [--state STATE]` | `GET /queue` |
| `dtc queue show <Queue ID>` | `GET /queue/{id}` |
| `dtc queue cancel <Queue ID>` | `POST /queue/{id}/cancel` |
| `dtc queue resume <Queue ID>` | `POST /queue/{id}/resume` |

- `JOB` is a job ID or a job file's name, as the API takes, or a path to a
  file directly in `data/jobs/`, which the command turns into its name. The
  server runs only the files there.
- `--wait` (`run-job`'s replacement) submits, prints the entry's ID, then reads `WatchQueueEntry`, which streams
  `state`, `current_run`, and (a server-side addition above) `total_runs` -- enough to print each run's start and
  finish with no further field: a run's start is `current_run` advancing (`run 3/7 started`), and its finish is
  inferred rather than read directly -- `current_run` advancing again means the previous run succeeded, and the
  entry reaching one of `FINISHED_STATES` while `current_run` still names the last run means that run is the one
  that ended the chain, so `--wait` prints its outcome from `state` and `error` once the stream reaches a final
  state. It exits with a code drawn from the entry's own final `state`, not `EXIT_CODES_BY_ERROR_CODE` (that table
  maps a *submission* refusal, which `add` without `--wait` already exits through the same way `dtc queue`'s other
  commands do, since it never watches a job outcome): 0 for `succeeded`; `exit_code_for_signal(SIGINT)` (130) for
  `cancelled`, matching `run-job`'s own Ctrl-C; `exit_code_for_signal(SIGTERM)` (143) for `interrupted`, matching
  what a direct `SIGTERM` to `run-job` itself produced, since an interrupted entry is one the *server* stopped
  mid-run, not the chain's own doing; and 1 for `failed`. A new table beside `EXIT_CODES_BY_ERROR_CODE`,
  `EXIT_CODES_BY_QUEUE_STATE` (`core/exit_codes.py`), holds this mapping. Ctrl-C cancels the entry
  ([Milestone 01](milestone-01-queue-run-manager.md#cancel) rules) before
  the command exits, as `run-job`'s own Ctrl-C did. Without `--wait`, `add`
  returns as soon as the entry is queued, as before.
- A queued job meets the same
  [rules and limits](milestone-02-http-api.md#rules-for-jobs-the-api-runs)
  as an agent's (owner decision: the API cannot tell a person from an agent,
  since they share the token). The audit log names the caller `cli`.
- Each command takes `--server-url` (default `http://127.0.0.1:8765`),
  `--token-file` (default `ProjectPaths.server_token`), and
  `--allow-remote-server`, with the same rule as `dtc mcp`. `add --wait`'s gRPC client needs no `--grpc-port` of
  its own: it reads `GET /v1/health`'s `grpc_port` (a server-side addition above) and pairs it with `--server-url`'s
  own host.
- Output is text for people, the entry's ID first
  (`Q0007 queued: walk.yaml`); `list` is a table, and `show` gives the
  state, the execution ID, the current run, the end of any wait, the error,
  and whether the entry can be resumed, from which run, or why not.
  `cancel` stops a running entry at once, losing the run in progress
  ([Milestone 01](milestone-01-queue-run-manager.md#resume)), and says so.
- An API error exits with the code the CLI gives the same error code
  (`EXIT_CODES_BY_ERROR_CODE`; the new codes exit with 2). A server that
  cannot be reached, or that refuses the token, exits with 1 and a message
  naming `dtc serve`. The token is never printed.
- The HTTP client lives in `cli/`, importing `httpx` only inside the
  commands, beside the gRPC client `add --wait` uses. `mcp_server/` and
  `tui/` keep their own: front ends never import each other, and
  `mcp_server/` imports nothing else from the package.

### Retiring `run-job`

- The `run-job` command and its module are removed from `cli/`. It ran
  `draw-things-cli` itself, taking the run lock directly
  ([Phase 1](../archive/phase-1/README.md)); `dtc queue add --wait` is its
  replacement, going through the server instead.
- `run-job`'s own `--executable` and `--shutdown-grace` have no equivalent
  on `dtc queue add`: they governed how `run-job` drove `draw-things-cli`
  directly, and only `dtc serve`'s own flags of the same name mean anything
  now, since only the server ever starts a run.
- Anything that scripted `run-job` breaks outright and must move to
  `dtc queue add --wait`, which needs a running `dtc serve` that `run-job`
  never did (**Change**, recorded in the phase-3 changelog).

### The TUI's queue

- A Queue widget in the right column, between Job Definition and Execution
  History, always shown, 5 rows (owner decision): each entry's ID, its job
  file's name without the extension, its state, and its runs (succeeded of
  total, or `run 3/7` while it runs, from `queue_entry()`'s `total_runs` and
  `succeeded`, a server-side addition above -- read directly off each row
  `GET /queue` already returns, with no per-entry follow-up call). The
  running entry comes first, then
  the queued ones in order, then the finished ones, newest first.
- The minimum terminal size grows by the widget's height, to about 80x41,
  measured when it is built; the user guide gives the new size.
- While the server is up, the widget subscribes to `WatchEvents` (the same
  gRPC client the MCP server uses) and updates on every entry it names,
  instead of polling; while it is down, it falls back to the read-only
  reader in `services/`, beside `HistoryReader`, not over HTTP, on the
  history's 5-second check, exactly as before. Either way it shows the queue
  whether or not the server is up.
- `/queue add [JOB]` submits (the current Job Definition's file when `JOB`
  is omitted), `/queue cancel Q0007` and `/queue resume Q0007` act on an
  entry: all three call the API with `httpx`, the same rules and limits as
  `dtc queue` and the caller header `tui`, and show the API's error inline,
  naming `dtc serve` when it cannot be reached. `c` on a selected row is a
  shortcut for `/queue cancel` on it.
- `/apply` becomes an alias for `/queue add` with no `JOB`: it no longer
  runs the current Job Definition itself
  ([Phase 2](../archive/phase-2/README.md)), since the TUI never invokes
  `draw-things-cli` (**Change**). Its old busy message ("another run is in
  progress") no longer applies to it; a server it cannot reach is the only
  way it fails now.
- `/get queue` prints the entries in Messages. `/describe Q0007` (the noun
  inferred from the letter, as for `J` and `E`) prints one entry: its state,
  job, times, execution ID, the entry it resumes and the one that resumes
  it, and its error. Enter on a row does the same. Tab completes queue IDs,
  and moves command line, Job Definition, Queue, Execution History,
  Execution.

### The TUI's live output

Today the `draw-things-cli` pane (`CliPane`) and the run line read `LiveRun`,
updated from the `JobExecutor` the TUI ran itself
([Phase 2 Milestone
04](../archive/phase-2/milestone-04-tui-live-run.md)). Once the TUI never
invokes `draw-things-cli`, that source is gone, and this milestone's plan
must name its replacement: the same
[`WatchEvents`](milestone-02-http-api.md#monitoring-grpc) subscription the
Queue widget already opens, with `include_output=true`, so the child's
output lines (`run_output`) reach the TUI too, not only the entries' state
(owner decision: streaming every line of `draw-things-cli`'s output into the
TUI is what `include_output` was built for, and nothing has used it yet).
On the server side this needs nothing new: `QueueWorker` already passes
`self._events.job_event` as an observer of every run
([`services/queue_worker.py`](../../src/draw_things_control/services/queue_worker.py)),
so every `RunOutput`, progress lines included, already reaches the backlog
and the stream today; only the TUI's own consumer is missing.

- One `WatchEvents` call, not two: the Queue widget's existing subscription
  is opened with `include_output=true` and feeds both the Queue widget and
  the pane, so queue and job events stay in one order and the TUI never
  holds two gRPC streams open at once.
- It runs as an async Textual worker (`grpc.aio`, the same client library
  `dtc queue add --wait` and `dtc mcp` use, on the app's own asyncio loop),
  not a thread: `LiveRun.apply` and the pane's widgets are called directly
  from it, with no `post_message`/`call_from_thread` hop, and `LiveRun`'s
  existing single-thread assertion still holds, since the whole app runs on
  one thread now. Textual cancels a `@work` worker when its owner unmounts,
  which ends the gRPC call cleanly; unlike Phase 2's backstop, nothing needs
  to stop a child on the way out, since the job the pane was watching is the
  server's, not the TUI's, to stop.
- `jobs/events.py` gains `event_from_dict`, the exact inverse of
  `event_to_dict`: given an `Event`'s `kind` and `data_json`, it rebuilds the
  matching `JobEvent` dataclass, tested by a round trip through
  `event_to_dict` for every event kind. It is the first reader of
  `data_json` (Milestone 02 built the encoding; nothing decoded it, since
  `dtc queue add --wait` reads only the typed `QueueEntrySnapshot`) and
  lives beside `event_to_dict` so the two stay in sync.
- `LiveRun` (`tui/live_run.py`) drops its `JobDefinition` constructor
  argument: the TUI does not read the running entry's snapshot before it
  seeds the pane (below), only events and two HTTP reads. It starts empty;
  `JobStarted` seeds it (job name, file, mode, seed and its source,
  cooldown, `total_runs`) and now also pre-sizes the run table to
  `total_runs` rows, each a placeholder pair name (`"?"`, as `_run()`
  already fills one in when an out-of-order run number needs it), which its
  own `RunStarted` overwrites with the real pair name from `RunStarted.pair`
  when it starts (**Change** from Phase 2: the pair name of a run not yet
  started reads `"?"`, since the TUI holds no schedule to read it from
  before the event arrives, but the table's length, and so `len(live.runs)`
  everywhere it is already read — `estimate.py`'s `job_estimate` and five
  places in `text/status.py` that print "run N/`total`" — is right from
  `JobStarted` on, with no change needed at any of those call sites).
- **Correction**: `JobStarted` *does* carry the execution's own ID. `jobs/executor.py`'s `job_started_event` sets
  `execution_id=manifest.execution_id`, and `JobRecords.open` always builds a manifest with the `execution_id` its
  caller reserved, regardless of `write_job_records` -- only the manifest and log *paths* are conditional on that
  flag, not the ID itself. The worker (`QueueWorker`) links a claimed entry to its execution as early as
  `reserve_execution_id` fires, before `JobStarted` publishes, so every queue-driven run's `JobStarted` already
  names its execution. The milestone's first draft claimed otherwise, and had `CliPane.new_job` read
  `GET /queue/{id}` on every live `JobStarted` to recover an execution ID it already has, and infer which queue
  entry a run belongs to from `queue_entry_changed`'s ordering relative to it (**Change**, correcting both: see the
  phase-3 changelog). `CliPane.new_job` instead reads `execution_id` straight off the event -- no HTTP call, and no
  ordering inference, for the live-start case; the ordering guarantee still matters for a different reason (below,
  "Seeding") when no live `JobStarted` was seen at all. What job events still lack, and do not need: a *queue* ID
  (`Q0007`). Nothing in `jobs/events.py` names one, since a job can run outside the queue in every phase before
  this one, and coupling `jobs/` to `state/queue.py`'s IDs would buy the pane nothing once it already has the
  execution ID everything else it reads (the manifest, the log, `GET /executions/{id}`) is keyed by.
- Seeding, not just live events: the backlog is 2000 events, shared by every
  client and every kind of event including `run_output`, so a verbose job
  can push its own `JobStarted` out of the backlog within itself, well
  before a client that was not already subscribed gets a chance to see it;
  a client cannot lean on backlog replay to learn of a job already running.
  So on startup, and whenever a `Reset` arrives, the pane does not wait for
  a live `JobStarted`: it opens the `WatchEvents` subscription first (so no
  event from this point on is missed), reads `GET /queue?state=running`
  (unpaged, and there is at most one), and, when one comes back, seeds
  `LiveRun` straight from `GET /queue/{id}` and `GET /executions/{id}`
  instead of from a `JobStarted` it may never see: `job_name`, `job_file`,
  `mode`, `model`, `seed`, `seed_source`, `cooldown`, `cooldown_source`, and
  `total_runs` from the execution, its `manifest`/`log` paths (below), a
  `RunState`/`FinishedRun` per run the execution already lists (each with
  its stored `pair`, below), and the still-running run's live position layered
  on from the entry (`current_run`, `current_run_elapsed_seconds`,
  `current_step`, `current_step_total`, `cooldown_until`) — the same two
  reads `/describe` already makes, and the same fields the running entry's
  row in the Queue widget already reads. Events the stream already buffered
  while the two reads were in flight are applied after seeding, in order;
  since applying a `RunStarted` or `RunFinished` for a run the snapshot
  already named is just overwriting it with itself or its next state, seeding
  first and then draining the buffer is never out of order in a way that
  matters. `latest_past_run`, from the TUI's existing read-only store
  connection (the Queue widget's own store fallback already opens one),
  seeds `LiveRun.past_run` exactly as the local start flow once did.
- **Server-side changes this seeding needs** are gathered under "Server-side changes" above, not repeated here:
  `run_summary`'s `pair` and `execution_detail`'s `manifest`/`log` fields (an endpoint Milestone 02 already
  shipped, recorded against it in the phase-3 changelog since it changes an endpoint that milestone already
  shipped), and the queue's own `total_runs`/`succeeded` this milestone adds. `GET /queue/{id}`'s `current_run`,
  `current_run_elapsed_seconds`, `current_step`, `current_step_total`, and `cooldown_until` already cover
  everything about the active run that the execution's own stored rows cannot (a run in progress has no `seconds`
  or `exit_code` yet); nothing further is needed for seeding beyond what "Server-side changes" already lists.
- Attaching mid-run recovers the job's own state in full, as above, but not
  its output lines: the state store never held `draw-things-cli`'s raw
  output (the job's log file does, only when `write_job_records` is on, and
  only on the machine running `dtc serve`), so the pane always starts its
  output from the moment it attaches, marked ("earlier output not shown")
  whenever seeding finds a run already under way. A stream that merely
  drops and reconnects, rather than opening fresh, resumes with the last
  `Event.id` the pane saw, and the backlog replays the gap, output lines
  included, losing nothing; only once that ID no longer explains itself
  does the server send `Reset` instead, which reseeds the pane exactly as
  attaching does.
- With the server down, the pane has no fallback, unlike the Queue widget's
  read-only store reader: the state store never held `draw-things-cli`'s raw
  output, so the pane says `dtc serve` is not running.
- The pane's own display rules are unchanged, only their source is: the
  2000-line cap, a step-progress line updating the run line instead of
  filling the pane, and stderr's own styling.
- This retires the rest of the TUI's direct-execution plumbing alongside
  `/apply`'s own retirement above: `DrawThingsApp.job_executor`, and the
  `--executable` and `--shutdown-grace` flags `tui_command` passes it
  (`cli/app.py`); the `on_unmount` cancel backstop and `tui_command`'s
  `finally` cancel; and `SignalGuard`'s handling of SIGHUP, SIGTERM, and
  SIGINT, which stopped a child the TUI ran itself, and which a signal no
  longer needs to do (Textual's own shutdown on the signal is enough).
  `dtc tui` drops `--executable` and `--shutdown-grace`; neither means
  anything once the TUI never starts a run. It gains `--server-url`, `--token-file`, and `--allow-remote-server`,
  the same three flags and defaults `dtc queue` and `dtc mcp` already take (undocumented in the milestone's first
  draft, which built the Queue widget's HTTP and gRPC clients without saying where they get a server to talk to):
  the Queue widget's HTTP client, its `WatchEvents` subscription, and the live-output pane riding on that same
  subscription all read them once, at startup. `/stop` becomes an alias for
  `/queue cancel` on the entry the pane is following, the same rule
  `/queue cancel` and `c` already give (no separate confirmation, as
  neither of those has one); `/quit` no longer asks to stop a job first,
  since quitting the TUI no longer stops anything running on the server
  (**Change**, the same one `/apply` already makes).

### Documentation

The user guide adds `dtc queue` (`add --wait` included) to the commands and
the server section, and the Queue widget, its commands, `/apply`'s new
meaning, the `draw-things-cli` pane's new source, and the new minimum size
to the TUI section. `run-job`'s section is removed, and `--shutdown-grace`
and `--executable` are removed from `dtc tui`'s, replaced by `--server-url`,
`--token-file`, and `--allow-remote-server`.

## Acceptance criteria

- Each `dtc queue` command, run with `CliRunner` against the app (a test
  passes an `httpx` transport and a fake gRPC channel to it), calls its
  endpoint, prints the result, and exits with the mapped code on an error; a
  job that breaks a rule is refused naming the field, with exit code 2.
- `dtc queue add --wait` against a fake `WatchQueueEntry` stream prints each run's start (from `current_run`
  advancing) and infers its finish (the next start, or the entry's own final `state`), and exits 0, 130, 143, or 1
  for `succeeded`, `cancelled`, `interrupted`, and `failed` respectively (`EXIT_CODES_BY_QUEUE_STATE`, not
  `EXIT_CODES_BY_ERROR_CODE`, which stays for `add`'s own submission refusal); a Ctrl-C during the wait cancels the
  entry first. Without `--wait`, `add` returns immediately, as before. `dtc queue add --wait`, the TUI's live
  output, and (Milestone 08) `dtc mcp` all derive their gRPC target from `GET /v1/health`'s `grpc_port` and the
  already-configured HTTP host, with no gRPC flag of their own; a non-loopback host is refused the same way the
  HTTP target already is.
- With no server, or a wrong token, each command, `add --wait` included,
  exits with 1 and names `dtc serve`; a non-loopback `--server-url` is
  refused without `--allow-remote-server`. The token never appears in the
  output.
- `run-job` is gone: the CLI has no command by that name, and nothing in
  `cli/` imports `draw-things-cli`'s runner directly any more.
- The Queue widget lists a test store's entries in the stated order, and
  updates live from a fake `WatchEvents` stream while the server is up, and
  from the store's reader, on the 5-second check, while it is down; `/get
  queue`, `/describe Q0007`, and Tab completion work either way.
- `/queue add`, `/apply` (its alias), `/queue cancel`, and `c` on a row call
  the API (a fake `httpx` transport) with caller `tui`, meet the same rules
  and limits as `dtc queue`, and show its error inline, naming `dtc serve`
  when the server cannot be reached. `/apply` no longer starts
  `draw-things-cli` itself under any circumstance.
- The `draw-things-cli` pane, driven by a fake `WatchEvents(include_output=true)`
  stream, shows a run's stdout and stderr lines (styled apart), bracketed
  output as written (never as markup), and step progress on the run line
  only, whether the entry was submitted by the TUI, `dtc queue`, or a fake
  agent client; `event_from_dict(event_to_dict(event))` round-trips every
  event kind. `job_estimate`'s fraction and remaining time, and every
  "run N/total" line in `text/status.py`, are correct from `JobStarted` on,
  before any run but the first has started.
- Started against a fake `GET /queue?state=running`, `GET /queue/{id}`, and
  `GET /executions/{id}` (a running entry and its execution, some runs
  already finished, one in progress), the pane seeds job name, mode, model,
  seed, cooldown, the manifest and log paths, every run's pair and status,
  and the active run's elapsed time and step, all without a single
  `WatchEvents` event; output starts empty, marked "earlier output not
  shown". With no entry running, it seeds nothing and waits. A reconnect
  with the last `Event.id` seen replays the gap, output lines included,
  losing nothing; a `Reset` reseeds the pane the same way attaching does.
  With no server, the pane says so, with no store fallback.
- `server/serializers.py`'s `run_summary` includes each run's `pair`, and
  `execution_detail` includes `manifest` and `log`, both already stored and
  now served; a test reads them back from `GET /executions/{id}`.
- `queue_entry()` includes each entry's `total_runs` and `succeeded`: 0 and the job's run count for a freshly
  submitted entry with no execution yet, and `first_run - 1 + execution.succeeded` once one is linked, for a plain
  submission and a resume alike; `GET /queue` and `GET /queue/{id}` agree, and `QueueRepository.list_active`/
  `list_finished` compute `succeeded` in one joined query, not one lookup per row (a test asserts the query count).
  A pre-schema-6 queue row with a linked execution backfills `total_runs` from it; one with none reads `total_runs`
  as `None` until resubmitted.
- Quitting the TUI, with `ctrl-c`, a signal, or `/quit`, while the pane is
  following a run ends the app at once and leaves the job running on the
  server; nothing in `tui/` imports `JobExecutor` or `jobs/executor.py`'s
  runner any more.
- `dtc tui --server-url ... --token-file ... --allow-remote-server` are accepted, defaulting the same way `dtc
  queue`'s do, and reach the Queue widget's HTTP and gRPC clients; `dtc tui` no longer accepts `--executable` or
  `--shutdown-grace`.
- At the new minimum size, every widget keeps its rows.
- Browsing the queue writes nothing to the state store; only `/queue add`
  (and `/apply`), `/queue cancel`, and `/queue resume` do, and only through
  the API.
- `make check` passes; the user guide documents `dtc queue`, `/apply`'s new
  meaning, and the Queue widget, and no longer documents `run-job`.
