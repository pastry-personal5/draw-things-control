# Phase 3 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 3](README.md). Newest first.

## 2026-09-29

- **Change** [M05]: From a code review of the milestone, two fixes to the worker. The claim now publishes its
  `queue_entry_changed` `running` under the worker's lock, as the park and hold events already were, so a park made
  just as an entry is claimed never reaches a front end first; before, the TUI's feed then reset the pending
  reservation, and the Status widget never showed `parking`. A release that lands once the claimed job has ended now
  drops the between-jobs cooldown even when it lands before the worker reads the hold after that job; before, the
  worker could still wait the cooldown out.
- **Change**: Fixed lost state-store writes while `dtc serve` runs a job. Every store opened in WRITE or RUN mode
  opened and closed the database file itself to create it with mode 0600, which dropped every POSIX lock the process
  held on it, SQLite's included. A TUI that then opened and closed the database took itself for the last user and
  deleted the WAL; the server went on writing to that unlinked file, so its execution stayed at `running` with 0 runs
  succeeded in the Execution History while the job went on. The file is now created only when it does not exist
  (`O_EXCL`), and an existing one is never opened outside SQLite.
- **Design decision** [M05]: From a code review of the milestone:
  - Retention keeps a parked entry until a resume anywhere in the chain below it has a succeeded run, and keeps the
    resumes in between with it. Counting only the entries that resume it directly kept it forever when its own
    resume's first run failed and a resume of that resume went on, and once that resume aged out, the parked entry
    read as never resumed and could be resumed a second time. Pruning the resumes in between as before was rejected:
    the newest resume could no longer walk back to the parked entry.
  - A park saves its hold before it makes the reservation, and an unpark whose hold cannot be released keeps the
    reservation, so a failed write never leaves a park without its hold, or a hold without its park.
  - The executor waits out the rest of a cooldown only when a park was asked for during it (`CancelToken.park_count`,
    in place of the unused `park_requested`), so a `Cooldown` that returns early on its own is not called again and
    again. Documenting that a `Cooldown` must never return early was rejected: a fake that does would hang.
  - Both front ends word a park from one module, `services/queue_park_text.py`, which also says, from the run
    number, that a park on the job's last run lets it finish. Adding the park point to the API and the gRPC snapshot
    was rejected: it needs a proto change, and the Status widget works from events, not the API. An unpark says
    `the queue is not held` rather than `the queue is released`, since an unpark can release nothing; adding a field
    to the unpark response saying whether it released the hold was rejected as more API for one word.
- **Owner decision** [M06]: From a review of the plan:
  - One endpoint, `POST /v1/executions/delete`, deletes one execution or several, in place of
    `DELETE /v1/executions/{execution_id}` plus that batch. Every other action endpoint is a `POST`, and one endpoint
    has one response shape. Keeping both, and adding `POST /v1/executions/{execution_id}/delete` instead, were
    offered.
  - `GET /v1/executions` gains a `name` filter, matching as `/filter name` does, so `dtc history delete --name`
    selects what the TUI would. Using `--job` (one job file) on the CLI instead was offered.
  - Deleting the whole history takes an explicit `all`: `/delete filtered` with no filter is refused, and
    `/delete all` and `dtc history delete --all` are added. Typing the count first, and no guard, were offered.
  - A log or manifest that cannot be deleted leaves the row deleted anyway. The report now names each manifest that
    stayed, and warns that `dtc import-history` would bring it back under a new number. Refusing the execution was
    offered.
- **Design decision** [M06]: From the same review:
  - The deletion's check also takes `ServerContext.submission_lock`, before the worker's `_state_lock`. The plan said
    `_state_lock` alone kept a resume from resolving an execution being deleted. It does not: `resume_entry` runs
    `_resolve_chain` before `enqueue` takes `_state_lock`. A resume already holds the submission lock across both.
  - The batch route writes one audit row per ID itself, since `audited(...)` writes one per request. Actions are
    plain strings; `state/audit.py` has no list of them to extend.
  - With the server down, `d` and `/delete` behave as the Queue widget's `c` and `p` do: nothing is deleted, and
    Messages says the server cannot be reached.
  - The exit criterion "Delete and overwrite are always recoverable" and Milestone 07's "Nothing is permanently deleted
    by the server" are scoped to job files, since deleting an execution is final.

- **Owner decision** [M06]: Milestone 06, "Delete executions", is added to Phase 3 and planned, built between
  Milestones 05 and 07. From an interview on the plan:
  - A deletion removes the execution's row, its runs, its log, and its manifest, and keeps its outputs, so
    `dtc import-history` cannot bring it back. Keeping the manifest as retention does, moving the manifest and outputs
    to a trash folder, and a table of deleted manifests (schema 7) were offered.
  - A running execution, and one a queued or running entry uses, cannot be deleted. The execution a parked,
    interrupted, failed, or cancelled entry would resume from can be, after the dialog warns that the entry can no
    longer be resumed. The owner first chose to refuse any execution a queue entry could still resume from. On a
    follow-up question, since that would keep a failed chain's execution until retention pruned its entry (deleting
    queue entries is out of scope), the owner chose the warning instead. Refusing parked entries' executions only was
    offered.
  - The TUI asks in a modal dialog. Pressing `d` twice, and a command with `--yes`, were offered.
  - Bulk deletion is in scope, and `dtc history delete` is added too. Gating deletion behind `--allow-write`, and an
    MCP tool in Milestone 08, were offered and not chosen. For several executions, the dialog steps through them one
    at a time with Delete, Skip, Delete all, and Cancel. The owner asked for Delete, Delete all, and Cancel; Skip, and
    the step-through, were proposed on a follow-up question and chosen over the same without Skip and over one
    summary dialog for the whole selection.
- **Design decision** [M06]: Deletion goes through the API (`DELETE /v1/executions/{execution_id}`,
  `POST /v1/executions/delete`), audited as `delete_execution`, rather than the TUI writing the store: the TUI only
  browses, and every other write goes through `dtc serve`. The check and the delete run under the queue worker's
  `_state_lock`, so a resume cannot resolve an execution that is being deleted. "A resume would accept this entry"
  gets one definition, shared by `queue_resume` and the deletion's warning. The history's own marks (`Space`) and
  `/delete filtered` select several; a gRPC event telling other clients of a deletion was left out, since the
  history already reads a missing execution as gone.
- **Change** [M05]: [Milestone 05: Park and hold](milestone-05-park-and-hold.md) is done. A running entry can be parked
  from the TUI (`/queue park`, `/park`, `p`) and `dtc queue park`: it ends `parked` after its current run, keeping
  every run, and a resume continues it at the next run. Parking holds the queue until a release, the queue can be held
  on its own (`/queue hold`, `/hold`, `dtc queue hold`), and the hold survives a restart. Four audited endpoints,
  `park_requested` and the hold in the API's queue responses and gRPC snapshots, three new event kinds, the `parked`
  state and history filter, and `dtc queue add --wait`'s exit code 3.
- **Design decision** [M05]: While building the milestone:
  - Retention reads the parked entries it keeps once, before either prune (`QueueRepository.kept_parked`), and passes
    them to both. The plan had `Store.prune` prune executions first "so both read the same queue", but the execution
    prune would then delete the resume's runs, and the queue prune, finding no succeeded run, would keep the parked
    entry forever. Evaluating the condition in each prune's own SQL was rejected for that reason.
  - `GET /v1/queue/{queue_id}` (and the park and unpark responses) gain `between_runs_after_run`: the succeeded run
    the entry is between runs after, a cooldown included, until its next run starts. The plan let a front end word a
    park's outcome from "the current run and the cooldown", but `cooldown_until` is the wait between two queued jobs,
    None while a job runs, so a park between two runs (which takes effect after the first of them) read like one made
    before the job's first run. Wording both "parks after its next run" was rejected: it names the wrong run. It is
    kept past `CooldownEnded`, since a park ends the cooldown before the entry reads `parked`.
  - `POST /v1/queue/hold` and `/release` also return `changed`, false when the queue already was, or was not, held,
    so a front end can say "The queue is not held" for a release that did nothing, as the plan asks. Reading
    `GET /v1/queue` first was rejected: a hold could change between the two requests.
  - `/park` on an entry already parking says "Already parking" only while the queue is held. When a release has ended
    the hold, it parks again, which holds the queue again, as `/queue park` does (owner decision of this date). The
    plan's controller said "Already parking" in either case, which would have kept `/park` from re-holding.
  - The worker marks an entry finished, and `_fail_to_start` fails one, under `_state_lock` with a
    `_job_finished` flag, so `park_running` sees the entry either still running or finished, never between, and a
    park that loses the race with the job's own end is refused without holding the queue.
- **Owner decision** [M05]: From a third interview, after a review of the plan against the code:
  - History retention keeps a parked entry and its execution until an entry that resumes it has a succeeded run;
    after that, they age out as usual. This supersedes, for parked entries only, the [M01] owner decision "Finished
    entries stay pruned with the history, so an entry left past `history_retention_days` can no longer be
    resumed". Pruning them as today, and never pruning parked ones, were offered.
    - The owner first chose "until an entry resumes it". Every job start prunes, though, so the resume's own start
      would delete the parked entry. A resume whose first run then failed, or one cancelled before it ran, could no
      longer be resumed, since the chain walks back to the parked entry. On a follow-up question, the owner chose
      "until a resume has a succeeded run". Keeping the first answer was offered.
  - A park on an entry already parking holds the queue again when a release has ended the hold. The plan's table
    had it as a no-op, which was offered.
  - The plan drops its reasons based on the old class limit. `QueueHold` stays its own class, for cohesion. This
    revisits today's size-limit entry, which left the plan as written. Leaving it, and folding `QueueHold` into
    `QueueWorker`, were offered.
  - While a reservation is pending, the Status widget's Job bar reads `parking` in place of its end estimate.
    Keeping the full-job estimate was offered.
- **Design decision** [M05]: From the same review:
  - A park takes effect when the executor commits to it through `CancelToken.take_park()`: after a succeeded run,
    after a cooldown the park ended, or at the top of a later run. After that, unpark is refused. A cooldown cut short
    by a park that was withdrawn before the commit waits out the rest. Without a commit point, an unpark landing
    after a park ended a cooldown would start the next run with no cooldown.
  - The worker's guard applies a pending park, and unpark withdraws one, under `_state_lock`, since a reservation,
    unlike a cancel, can be withdrawn. The cancel guard's read-then-call outside the lock would let an unpark slip
    between the two.
  - Reservations publish a new `queue_park_changed` event, not `queue_entry_changed` with `park_requested`. The TUI's
    feed reads `queue_entry_changed`'s `running` as the claim before `JobStarted`, and would reset
    `pending_queue_id` on every reservation.
  - A park that loses the race with the job's own end is refused, naming the state it reached, since the queue was
    not held. `cancel_entry` accepts the same race, because a cancel of a finished entry changes nothing a caller
    relies on.
  - A saved hold that cannot be read counts as held, with a warning. Treating it as released was rejected: a
    damaged row would then start the queue by surprise.
  - `QueueEntrySnapshot` also gains `queue_held`, so `dtc queue add --wait` can say its entry waits behind a hold.
    An extra `GET /v1/queue` would miss a hold made later. The entry detail already carries the worker's
    `cooldown_until`, as precedent.
- **Owner decision** [M05]: From a second interview on the plan:
  - Milestone 05 is built next, before Milestone 07. Building it after 07, or last after 09, was offered.
  - Unpark releases the hold only when that entry's own reservation made it. A hold made by `/queue hold`, or one
    already in place before the reservation, stays. This narrows the earlier [M05] answer, "Unpark withdraws a
    reservation, and releases the hold that reservation made". Clearing the hold on every unpark was offered.
  - A resume of a parked entry goes to the back of the queue, like every resume since Milestone 01. Putting it at
    the front, or at the parked entry's original place, was offered.
  - A reservation made during the job's last run is accepted: the job ends `succeeded`, and the queue is held.
    Refusing it, and pointing to `/queue hold`, was offered.
  - `/queue release` starts the next entry at once, even when the last job's between-jobs cooldown has not elapsed.
    Waiting out the rest of that cooldown was offered and recommended.
  - The TUI gets four top-level aliases: `/park` and `/unpark`, which act on the entry the draw-things-cli pane is
    following, and `/hold` and `/release`. `/park` alone, and `/park` with `/unpark`, were offered.
  - The hold is shown in the Queue widget's title and, while no job runs, in the Status widget
    (`Queue held since 12:04 (by Q0007)`). The title alone, and the title with the bottom status line, were
    offered.
- **Owner decision**: The size limits ([development-rules.md](../development-rules.md#project-layout),
  `tests/test_architecture.py`) are relaxed: modules rise from 800 to 3200 lines, classes from 250 to 1600, and
  functions from 40 to 800. This supersedes the earlier owner decision of this date that raised modules from 400 to 800 and kept the
  class and function limits. 1200, 500, and 80 were offered. The `generate` exemption from the function limit is
  removed, since `generate` (65 lines) is now well under it, and the rules say "single-purpose functions" rather than
  "small, single-purpose functions". Ruff's rules and pyright's mode are unchanged. The Milestone 05 plan, which
  places some code by the old class limit, is left as written.
- **Owner decision** [M05]: For image-to-video jobs, the last frame is extracted after every run, even when the job
  parks. A park takes effect only after the run's finish (color tags, last frame, measurement) and `RunFinished`,
  and only after a succeeded run, which for a video job means the frame was written. The park flag stays apart from
  `CancelToken.requested`, so a failed extraction under a reservation fails the run, as it would without one, and
  is never reported as interrupted. See [Milestone 05's "The last frame"](milestone-05-park-and-hold.md#the-last-frame).
- **Owner decision** [M05]: A new milestone, [Milestone 05: Park and hold](milestone-05-park-and-hold.md), is
  planned. It is numbered 05, as the owner asked. With it, a person can make a reservation to end a running job once its current
  run finishes, keeping every run it finished. The owner asked for a term of its own, not "interrupted" or
  "stopped", and chose "park": park (the command), parking (while the run finishes), and parked (the final state
  of the entry and its execution). The pending request is a *park reservation*. Land, wrap, and dock were offered.
- **Owner decision** [M05]: From an interview on the plan:
  - This supersedes the [M01] owner decision that rejected an after-run cancel. A cancel still stops at once, and
    park is the after-run action beside it.
  - It also supersedes the [M01] "No pause" decision. After a reservation, the queue is held until a release, so
    no other entry starts. Moving on to the next entry, and stopping `dtc serve` after the run, were offered.
  - The hold begins when the reservation is made and is saved in the state store. However the entry ends, and
    across `dtc serve` restarts, nothing starts until `/queue release`. Holding only when the entry actually parks
    was offered, with the hold either saved or kept in memory only.
  - The queue can also be held directly with `/queue hold` and `dtc queue hold`. Holding only through a park was
    offered and recommended.
  - Unpark withdraws a reservation, and releases the hold that reservation made.
  - A reservation made during a cooldown between runs parks at once. Parking after the next run was offered.
  - `dtc queue add --wait` exits with a new code, 3, for a parked entry. 0 and SIGTERM's 143 were offered.
  - Reservations are made from the TUI and from `dtc queue`; MCP tools stay with Milestone 08. TUI only, and adding
    the MCP tool to Milestone 08 now, were offered.
  - The Phase 3 non-goals that ruled out letting a run finish first and pausing the queue are updated.
- **Design decision** [M05]: How the plan builds it:
  - Parking is its own request on `JobExecutor` (`CancelToken.park()`) with a new `JobStatus.PARKED`. The rejected
    alternative was an observer that calls `cancel()` at `RunFinished`. That needs no executor change, since the
    loop already stops "before run k+1", but the execution would read `interrupted` with exit code 143, the word
    the owner asked not to use.
  - No schema migration: the state columns are `TEXT` with no `CHECK`, and the hold is a `queue_hold` row in the
    existing `settings` table.
  - The reservation is kept in the worker's memory only, since stopping the server kills the run anyway.
  - The hold is its own `QueueHold` class, because `QueueWorker` is at the 250-line class limit.
  - Cases the interview left open:
    - A reservation on a queued entry is refused.
    - A cancel overrides a reservation, and a reservation after a cancel is refused.
    - Stopping the server with a reservation pending ends the entry `interrupted`.
    - A resume never releases a hold.
  - Refinements of the interview's answers:
    - `/queue hold` on a queue that is already held makes the hold its own, so a later unpark no longer releases
      it.
    - A release while the entry is still parking lets it park. The worker then moves on to the next entry after
      the between-jobs cooldown, as after a succeeded job.
    - A hold that lands during the cooldown between queued jobs ends that cooldown at once.
- **Change** [M04]: The TUI draws less while a job runs. Reported as the TUI responding slowly during a run; measured
  during a live run: the TUI itself answered a key in about 2 ms and was idle (0.3% CPU, 15 feed events in 14 minutes),
  while draw-things-cli held the GPU at 100%, which the terminal (Wave, an Electron app) needs to draw every frame. That
  points to the terminal's drawing waiting on the GPU, not to the TUI. The TUI's share: every second it redrew the Status widget, the run line,
  and the status line whether or not they had changed, each with a layout pass that also redrew both logs' scrollbars in a
  second frame. They are now `tui/widgets.py`'s `SteadyText`, which skips an unchanged text and draws a changed one
  without layout: one frame a second instead of two (about 3.4 KB/s instead of 6.1 KB/s), and none while nothing changes.
  `/verbose low` still draws them once a minute. A Status widget line too long for its width now ends in an ellipsis
  instead of wrapping and pushing the lines below it out of view: Textual drops a Rich Text's own `no_wrap` and
  `overflow`, so `styles.tcss` sets them on `#status`.
- **Change** [M04]: Fixes from reviewing Milestone 04. Low drops the active run's step readings
  (`LiveRun.forget_progress`) when chosen mid-run or when attaching at low, so the Status widget's `step N/M` line no
  longer freezes (that run then estimates from elapsed time, as a run begun at low does); `/verbose` and a confirmed
  `/stop` render at once instead of at low's next once-a-minute refresh; a job's start is logged in Messages again
  (`Job started: ...`, lost with the same Milestone 03 dead branch as the history refresh); and the TUI's signal
  exit codes, signal registration, and the server's run-lock status line have tests again.
- **Change** [M04]: Milestone 04 is implemented and done: `/verbose high|medium|low` (kept in
  `config/tui-preferences.yaml`), `include_output` on the shared `WatchEvents` stream by level with a reconnect across
  the low boundary, medium's per-run first-minute window, and low's once-a-minute periodic rendering. See the
  milestone's "Built" section for the message wording and the cases the plan left open (medium between runs, the
  startup notice, what low hides in the run line).
- **Change** [M04]: The `tests/tui` harness now runs the app against a fake `dtc serve` (`tests/tui/fake_server.py`).
  Removed with it, because they tested local job execution that Milestone 03 retired: the `/apply` confirmation and
  its tests, the busy-lock, `run-job` file, and signal-stops-a-local-job tests in `test_live_run.py` (rewritten around
  the feed, with the tests that still describe live behavior ported: the history cursor and row updates, the
  redacted command, the estimate from a past run), `tests/tui/sigterm_app.py` and its SIGTERM test,
  `tests/tui/fake_runs.py`, and the `--executable` and `--shutdown-grace` `dtc tui` option tests. Stale expectations from Milestone 03 were updated (the Queue widget in
  the layout and Tab order, `/apply [<Job ID>]` and `/describe queue` in usage, `LiveRun`'s constructor in
  `test_estimate.py`). `MainScreen`'s running-job methods moved to `tui/running_job.py`'s `RunningJobView`, taking
  the class under the size limit; `tests/test_architecture.py` passes again.
- **Change** [M04]: Fixed a Milestone 03 regression found while porting the history tests: a job's start never reached
  the history. `JobStarted` arrives through `RunningJobView.job_started` (as the feed calls it), but the code that
  read the history again and moved its cursor to the new execution sat in `job_event`'s `JobStarted` branch, which
  the feed never reaches, so the new row only appeared when the job ended. `job_started` now does it (for an attach
  too), and the dead branch is gone.
- **Design decision** [M04]: `MainScreen.on_mount` shows a job the feed already followed as an attach (`seeded=True`),
  and re-runs the feed-connected notice, since a fast local server can connect and seed before the screen is mounted,
  which lost the pane's content and the startup low notice. No earlier decision changed.
- **Owner decision** [M03]: Milestone 03 is marked done. Its code landed in `feat(queue): add queue CLI and TUI live
  output`; the owner accepted it as complete even though `tests/tui`'s harness still uses the pre-Milestone-03 app
  constructor and `MainScreen` is over the class size limit (both are Milestone 04's first steps), and the user
  guide and `docs/architecture.md` do not yet describe `dtc queue` or the Queue widget.
- **Owner decision** [M04]: The `draw-things-cli` pane shows progress lines (`Processing... [ ] 2  %`,
  `Sampling... N / 40 [ ] N  %`), which Milestone 03 kept out of it (they only updated the run line); the owner
  reported them as missing. Fixed first, before the verbose levels, which then treat them as ordinary output lines.
- **Owner decision** [M04]: After a review of the milestone against the code, three earlier [M04] entries below
  are superseded. Low mode does not poll `GET /queue/{id}` (the step counter is not shown there; run number,
  elapsed time, and cooldown still update, rendered once a minute, and the person is told so when low is chosen).
  Medium's window restarts when `/verbose medium` is typed, as well as at each run's start (a switch mid-run shows
  output at once). Lines hidden by medium or low are never shown later. The review also found that
  `tests/test_architecture.py` already fails on `main` (`MainScreen` is 252 lines, over the 250 limit) and that the
  `tests/tui` harness still uses the constructor Milestone 03 replaced; the owner chose to fix both as Milestone
  04's first step.
- **Owner decision**: The module size limit ([development-rules.md](../development-rules.md#project-layout),
  `tests/test_architecture.py`'s `MAX_MODULE_LINES`) rises from 400 to 800 lines, to give modules more headroom
  before a split is required. The class (250) and function (40) limits are unchanged.
- **Owner decision** [M04]: A new milestone, numbered 04 (an unused number, so the build order stays numeric; drafted as 10 first, then moved before anything was committed) and built next (before Milestone 07), gives the TUI a `/verbose
  high|medium|low` command over the live output Milestone 03 just built: `high` is today's behavior; `medium`
  streams every line but the pane only shows a run's first minute of it; `low` stops the server from sending bare
  output at all and slows every periodic widget to a once-a-minute refresh. See
  [Milestone 04](milestone-04-tui-verbose-mode.md).
- **Owner decision** [M04]: The level changes the actual `WatchEvents` request, not just local rendering (the
  alternative considered and rejected): entering or leaving `low` reconnects the shared stream with a different
  `include_output`, with a message telling the person a reconnect is happening. `high` and `medium` both request
  every line and differ only in what the pane does with them, so no reconnect happens between those two. The
  reconnect resumes from the last event ID when the backlog can still explain the gap; it cannot always: the
  backlog stores every event regardless of any subscriber's filter, and a subscriber's own last-seen ID only
  advances on events it actually receives, so a run chatty enough to push more than 2000 filtered `run_output`
  events through the backlog while the TUI sits in low mode can leave that ID too old to explain by the time the
  level changes back. The server then sends `Reset`, handled by the pane's existing reseed path — the same one a
  real disconnect already takes, not a new one.
- **Design decision** [M04]: `server/grpc_service.py`'s `_wanted` filters the whole `"run_output"` kind on
  `include_output`, progress/percent lines included (`jobs/events.py` gives both the same kind), so low mode loses
  the run line's live step counter along with bare output (elapsed time is unaffected: it is timed locally from the
  live `RunStarted`, which low mode still receives). Rather than keep `include_output=true` just for progress, low
  mode polls `GET /queue/{id}` every 60 seconds for `current_step`/`current_step_total`. It does not reuse
  `tui/feed.py`'s `_seed_active_run` to apply the response: that function assumes it runs before any live event, and
  a periodic poll has no such guarantee — a response for a run that finished (or a job that ended) while the `GET`
  was in flight would otherwise resurrect it with a synthetic `RunStarted`. A narrow `_refresh_step` re-checks the
  poll is still current (same live run, not ended, still low) before applying anything, and drops a stale response
  outright. Structured job/run/cooldown events are never `run_output` and keep arriving live at every level, so
  history refresh and run-end detection are not delayed by being in low mode.
- **Design decision** [M04]: Medium's one-minute window is measured from each run's own `RunStarted`
  (`LiveRun.run_started_at`), not from when `/verbose medium` was typed: switching into medium mid-run shows
  nothing until the next run starts. This is the plain reading of "the first 1 min of run," not a rule about the
  command itself.
- **Owner decision** [M04]: The chosen verbose level persists across `dtc tui` restarts, rather than always
  starting at `high`.
- **Design decision** [M04]: Persistence lives in a new `config/tui-preferences.yaml` (gitignored,
  `ProjectPaths.tui_preferences`), read leniently (any problem falls back to `high`) and written only by the TUI.
  Writing the level into `config/global-config.yaml` was rejected: that file is out of scope for this (phase 3's
  own non-goals already rule out writing it from any interface), machine-wide, shared with `dtc serve`, and
  strictly validated against a closed key set, none of which fits a single TUI session's display preference.

## 2026-09-28

- **Correction** [M03]: The milestone document's "Job events carry no queue ID or execution ID of their own
  (`JobStarted` has none)" was wrong: `jobs/executor.py`'s `job_started_event` already sets
  `execution_id=manifest.execution_id`, and `JobRecords.open` builds that ID into the manifest regardless of
  `write_job_records` (only the manifest and log *paths* are conditional on it), so every queue-driven run's
  `JobStarted` already names its execution. The plan built on the wrong claim: it had the pane read
  `GET /queue/{id}` on every live `JobStarted` to recover an ID already on the event, and infer which queue entry a
  run belonged to from `queue_entry_changed`'s ordering relative to `JobStarted`, an inference the event's own
  `execution_id` makes unnecessary for the live-start case (the ordering guarantee is still used, for a different
  reason, by the seeding path below, which has no live `JobStarted` to read at all). See
  [Milestone 03](milestone-03-queue-for-people.md#the-tuis-live-output) for the corrected text.
- **Design decision** [M03]: Server-side additions Milestone 03 needs beyond Milestone 02's API, found on a review
  of the milestone document against the code before any of it was built.
  - The queue table gains `total_runs` (schema 6), set once from `job.run_count` at submission and at resume, so a
    `queued` entry (no linked execution yet) can still show "run 0/7" instead of nothing. `queue_entry()` gains
    `total_runs` and `succeeded` (the `first_run - 1 + succeeded` convention the resumed-execution display already
    uses, [Milestone 01](milestone-01-queue-run-manager.md)'s design decision on resumed progress), computed by a
    join in `QueueRepository.list_active`/`list_finished` rather than one `executions.by_number()` lookup per row,
    which does not scale the way `_last_run_seconds`'s single lookup (Milestone 02) does. Recomputing the total
    from the stored `job_text` on every read, avoiding the new column entirely, was rejected: it repeats a YAML
    parse and a base-configuration merge on every list read of a queue a client re-reads on every relevant event.
  - `GET /v1/health` gains `grpc_port`, so a client configured with `--server-url` alone (every client this phase
    and Milestone 08 add) can derive its gRPC target without a second flag; today nothing names how `dtc queue add
    --wait`, the TUI, or `dtc mcp` are meant to find `--grpc-port` (default 8766, distinct from the HTTP port), and
    Milestone 08's own document already assumes a gRPC client built from `--server-url` alone without saying how.
    A new `--grpc-url`/`--grpc-port` flag on every gRPC-using client was considered and rejected: it duplicates
    information the server already knows and must be kept in sync with `--grpc-port` by hand.
  - `QueueEntrySnapshot` gains `total_runs`, read once from the entry's stored column, so `dtc queue add --wait`
    can print "run 3/7" the way the Queue widget does; `--wait` infers a run's finish from the next run's start or
    the entry's own final `state`, needing no separate "run finished" field.
  - `dtc queue add --wait` exits with a code drawn from the entry's own final `state` (a new
    `EXIT_CODES_BY_QUEUE_STATE` table in `core/exit_codes.py`: 0 `succeeded`, 130 `cancelled`
    (`exit_code_for_signal(SIGINT)`, matching `run-job`'s own Ctrl-C), 143 `interrupted`
    (`exit_code_for_signal(SIGTERM)`, since an interrupted entry is one the server stopped mid-run, not the chain's
    own doing), 1 `failed`), not `EXIT_CODES_BY_ERROR_CODE`: that table maps a submission refusal, which is all
    `add` without `--wait` ever exits through, and was never meant to cover a job's own outcome. Giving `failed`
    and `interrupted` the same code, since neither retries automatically and a script cannot yet act on the
    difference, was considered and left as a follow-up if it turns out to matter.
  See [Milestone 03](milestone-03-queue-for-people.md#server-side-changes-state-server-the-grpc-service) for the
  full plan.
- **Change** [M03]: A gap outside the server, found on the same review: the milestone document never said where
  `dtc tui` gets the server it talks to. `dtc tui` gains `--server-url`, `--token-file`, and `--allow-remote-server`,
  the same three flags `dtc queue` and `dtc mcp` already take, in place of the `--executable`/`--shutdown-grace` it
  drops (see [Milestone 03](milestone-03-queue-for-people.md#the-tuis-live-output)).
- **Change** [M02]: `run_summary` (`server/serializers.py`) gains `"pair": run.pair`, and `execution_detail` gains
  `"manifest": row.manifest_path` and `"log": row.log_path`. `state/execution_rows.py`'s `RunRow` and `ExecutionRow` already
  carried all three; `GET /executions/{id}` simply never served them. Needed so Milestone 03's TUI can rebuild a running job's
  full state (every run's pair name, and the manifest/log paths the finished-job summary shows) from this one already-built
  endpoint, instead of only from events it may not have seen (see the [M03] entry below).
- **Design decision** [M03]: Supersedes this entry's own "`estimate.py`'s `job_estimate` moves off the run table's length ...
  onto `JobStarted.total_runs`" line, below, and the milestone doc's matching text: `len(live.runs)` is read as the job's
  total run count in six places, not one (`estimate.py` and five in `text/status.py`), and patching every reader individually
  was the wrong fix. `LiveRun` instead pre-sizes its run table to `total_runs` placeholder rows the moment `JobStarted` is
  applied (`total_runs` is one of `JobStarted`'s own fields), so `len(live.runs)` already reads right everywhere, with no
  change needed at any of those call sites; each row's pair name fills in from `RunStarted.pair` when its own run starts, in
  place of the placeholder. Also added: a client cannot always learn of a job already running by waiting for a live
  `JobStarted`, since the backlog is 2000 events shared by every client and kind, `run_output` lines included, so a verbose
  job can push its own `JobStarted` out of the backlog within itself, before a newly-attaching client (the TUI on startup, or
  after a `Reset`) ever opens the stream. The pane now subscribes to `WatchEvents` first, then reads
  `GET /queue?state=running` and, for the entry it names, seeds `LiveRun` straight from `GET /queue/{id}` and
  `GET /executions/{id}`, rather than a `JobStarted` it may never see; this is what surfaced the two fields the entry above
  adds. See [Milestone 03](milestone-03-queue-for-people.md#the-tuis-live-output) for the corrected plan.
- **Owner decision** [M03]: The `draw-things-cli` pane must keep showing every line of `draw-things-cli`'s output, live, once
  Milestone 03 takes direct execution away from the TUI, over the same gRPC channel the Queue widget uses to watch the queue.
  The plan's TUI section named the Queue widget's `WatchEvents` subscription but never said what would feed the pane once
  the TUI stops running jobs itself: retiring direct execution ([Phase 2 Milestone
  04](../archive/phase-2/milestone-04-tui-live-run.md)) silently drops the pane's only source (`LiveRun`, fed from a
  locally-run `JobExecutor`'s events), and nothing in the plan named a replacement. See
  [Milestone 03](milestone-03-queue-for-people.md#the-tuis-live-output) for the corrected plan.
- **Design decision** [M03]: The Queue widget's existing `WatchEvents` call is extended with `include_output=true` rather than
  opened a second time, so the pane reads the child's `run_output` lines from the one gRPC stream the TUI already holds open
  (Milestone 02 built `include_output` for exactly this; nothing had used it yet), keeping queue and job events in one order
  and the server-side event wiring untouched (`QueueWorker` already forwards every `RunOutput` to the backlog). Two
  alternatives were rejected: a second `WatchEvents` call scoped to output alone, which would double the gRPC connections and
  could deliver a job event and its own output out of order across the two streams; and tailing the job's log file, which
  exists only when `write_job_records` is on and only on the machine running `dtc serve`, not a remote one reached through
  the API. `jobs/events.py` gains `event_from_dict`, the first decoder of `Event.data_json` back into a `JobEvent`. `LiveRun`
  drops its `JobDefinition` argument and builds its run table from `RunStarted` events as they arrive, instead of from
  `job.schedule()` read up front, since the TUI no longer holds the running entry's job file; `estimate.py`'s `job_estimate`
  moves off the run table's length for the job's total run count, onto `JobStarted.total_runs`, so the estimate stays correct
  while the table is still filling in. Since job events carry no queue or execution ID, the pane relies on the worker's own
  order (`queue_entry_changed` to `running` always precedes that entry's `JobStarted`) to know which entry a run belongs to,
  and reads `GET /queue/{id}` once per `JobStarted` for the execution ID `/describe` already reads the same way. This also
  retires the rest of the TUI's direct-execution plumbing (`job_executor`, `--executable`, `--shutdown-grace`, the unmount
  cancel backstop, `SignalGuard`'s child-stopping signal handlers): `/stop` becomes an alias for `/queue cancel` on the entry
  the pane follows, and `/quit` no longer asks to stop a job first, mirroring `/apply`'s own change to submit through the
  queue instead of running a job directly.
- **Change** [M02]: A second review pass, of the fixes above. Supersedes that entry's "the queue routes publish
  `queue_entry_changed`" line: publishing an entry's own 'queued' or 'cancelled' change now goes through the worker
  (`QueueWorker.enqueue` and `.cancel_queued`, new; the shared lock and the actual DB write and publish live in a new
  `QueueClaimGate`, `services/queue_claim_gate.py`, extracted to keep `QueueWorker` under the 250-line class limit),
  not the routes calling `store.queue.submit`/`cancel_queued` themselves and publishing after. The route-level
  version raced the worker: a submission or resume could be claimed, and its 'running' published, before the route's
  own 'queued' publish ran, so a client watching events could see a running entry reported as queued. `enqueue` and
  `cancel_queued` run the insert (or the queued-cancel) and the publish under the same lock
  `_claim_and_run_one` claims with, so the two can never interleave. `submit_job` and `resume_entry` take the actual
  insert as a new `enqueue` parameter (`Enqueue`, a plain type alias, not `QueueWorker` itself: `queue_worker.py`
  already imports `queue_submit.py` for `parse_snapshot`, and importing back would cycle); `routes_queue.py` passes
  `context.worker.enqueue`, and tests that call `submit_job`/`resume_entry` directly, not caring about the event
  stream, pass nothing and get the old direct-insert behavior unchanged.
- **Change** [M02]: `dtc serve` bound its ports only inside `_serve_async`, after `QueueHost.start()` had already
  started the worker thread. A taken `--grpc-port` (or, previously, an HTTP `--port` uvicorn's own bind refused with
  a bare `sys.exit(1)`) could land after the worker had already claimed a queued entry, which the refusal then tore
  down as `interrupted` for what was really a configuration mistake. `QueueHost.start(start_worker=False)` now builds
  the worker without starting its thread; `serve.py` binds the gRPC port (`add_insecure_port`, as before) and now
  also the HTTP port itself, both as `InputError` (exit 2, naming `--port` or `--grpc-port`), and only then calls
  `host.start_worker()`. The HTTP bind (`_bind_http_socket`) mirrors what asyncio's own `loop.create_server` does
  from a host and a port alone -- every address `getaddrinfo` resolves the host to, `SO_REUSEADDR` on POSIX (a first,
  simpler version skipped this, and so failed a quick restart on the same port while the old socket sat in
  `TIME_WAIT`), and `IPV6_V6ONLY` on an IPv6 one -- rather than a single plain `socket.bind`, which also only ever
  bound one of the two addresses `--host localhost` can mean. Handed to uvicorn's private `Server._serve(sockets=...)`,
  which `listen()`s and closes them. Either port taken now fails before anything is claimed.
- **Change** [M02]: `GET /v1/queue` was documented as "not paged: the queue is bounded by `max_queued_jobs`", which
  only ever bounded the *queued* entries; finished ones stay until `history_retention_days` prunes them, and
  `history_retention_days: 0` keeps them forever. Queued and running entries (bounded, and there is at most one
  running) still always come back in full; finished ones now page, newest first, after them on the first page only,
  through the same `limit`/`cursor` (`server/pagination.py`) `/jobs`, `/executions`, and `/audit` use.
  `QueueRepository` gains `list_active` and `list_finished` for the split, and a plain `count` (a `SELECT COUNT(*)`)
  the queued-jobs limit check now uses instead of loading every queued row just to len() them.
- **Owner decision** [M02]: The audit log's own spec line, "one entry for every such request, refused ones included",
  did not hold for two refusals that happen before a route's own `audited()` block ever opens: an unknown
  `X-Dtc-Caller` (a FastAPI dependency raising ahead of the route body) and a body FastAPI's own validation refuses
  (`POST /v1/queue` with a missing or unparsable `job`). Both are now recorded. `X-Dtc-Caller` is no longer resolved
  by a shared `get_caller` dependency; each audited route reads the raw header itself and resolves it inside its own
  `audited()` block, raising there so a bad value is recorded, with the documented default caller `api`, like any
  other refusal. The malformed-body case, which never reaches a route body at all, is recorded directly in
  `errors.py`'s `RequestValidationError` handler: it still reads `X-Dtc-Caller` itself, since a header is parsed
  independently of the body, recording the real caller when it is valid and `api` only when it is not, and it
  re-checks the bearer token itself first (`is_authenticated`, new, shared with `require_auth`) rather than assuming
  body validation runs after it: an unauthenticated request must never be recorded, exactly as `require_auth`'s own
  401 never is.
- **Owner decision** [M02]: A rejected `Host` header (the DNS-rebinding guard) keeps its 400 status, an explicit
  exception to the error table's `invalid_input` → 422 row: 400 is the ordinary status for a request naming the
  wrong server, and 422 is not a natural fit for it.
- **Owner decision** [M02]: From an interview on the review findings above.
  - Event order: the worker publishes (chosen, above); the route holding the worker's own lock around its insert,
    and leaving the race but documenting that a client should re-read state rather than trust an event's own state,
    were also offered.
  - Startup order: take the lock and recover, bind both ports, then start the worker (chosen, above); starting the
    worker only once both servers are listening but otherwise keeping uvicorn's own HTTP bind failure, and leaving
    the existing order as is, were also offered.
  - `GET /queue` paging: page the finished entries only, after the active ones in full (chosen, above); paging the
    whole list like the other list endpoints, and leaving it unpaged but documenting the real bound as retention, not
    `max_queued_jobs`, were also offered.
  - The two pre-route audit refusals: recorded with the documented default caller `api` (chosen, above); a new fixed
    marker (`unknown`) naming the refusal itself, and not auditing either case at all, were also offered.
  - The `Host` header's status: kept at 400 (chosen, above); returning 422, following the error table as written
    with no exception, and a new `invalid_host` code at 400, were also offered.
  - Efficiency: all three offered fixes were taken (`_queued_count`'s `COUNT(*)`, `GET /inputs`'s `InputCatalog`, and
    `preview_resume`'s header-only check, accepting that trade-off).
  - A `CLAUDE.md` re-exporting `AGENTS.md`, so a review with no project instructions of its own would still read
    them: declined.
- **Change** [M02]: Three efficiency fixes from the same review. `GET /v1/inputs` re-read every image's header
  (Pillow's lazy `Image.open`, not a full decode, but still real I/O and parsing) on every page, including images a
  later page's own slice would discard; `InputCatalog` (`services/input_listing.py`) now caches by each file's own
  modification time and size, the same idea `JobCatalog`'s own file cache already uses, and re-reads only a changed
  or newly-seen file. `GET /queue/{id}`'s resumability preview (`preview_resume`) fully decoded the candidate input
  image on every call; it now only checks that the file exists and its header is readable (`decode_input=False`),
  since it is a preview, not the resume itself -- a real `resume_entry` still decodes fully, since it is the one that
  actually starts a run from the image (a corrupt-but-header-readable image can therefore preview as resumable and
  still be refused by the real resume; accepted trade-off).
- **Change** [M02]: Fixes from a review of the Milestone 02 change. `POST /v1/queue` now checks the API rules and
  limits inside `submit_job` (a new `before_submit` hook, as `resume_entry` already had), on the job parsed from the
  exact text stored, instead of on an earlier read of a file that could change before the snapshot, and still before
  the input image is decoded. The queue routes
  publish `queue_entry_changed` for a submission, a resume, and a queued entry cancelled (`cancel_entry` now returns
  whether it cancelled one directly), since the worker never sees those. The token is compared as bytes, so a
  non-ASCII `Authorization` value is a 401 rather than a 500; an empty token file is refused, and a failed write no
  longer leaves one behind. `WatchEvents` reads the latest ID before replaying, so an event appended between two
  separate reads is no longer skipped. `WatchQueueEntry` no longer sends a message on every poll just because
  `current_run_elapsed_seconds` moved. A request FastAPI's own validation refuses now gets the stable error shape
  (`invalid_input`, `message`, `field`). A `--grpc-port` that cannot be bound is an `invalid_input` error, not a
  traceback. `tests/state/test_audit.py` no longer assumes the machine runs in UTC.
- **Change** [M02]: `GET /queue/{id}` and `WatchQueueEntry` gain `current_step`/`current_step_total`: the active
  run's latest reading from `draw-things-cli`'s own progress bar (`RunOutput.progress`, already carried on the raw
  event stream, but not previously surfaced on the at-a-glance queue-status surfaces a client polls or watches
  instead of subscribing to every event). `WorkerStatus.observe_run` (`services/queue_worker_status.py`) now also
  watches `RunOutput`, resetting the reading to `None` on every `RunStarted` (so a new run never briefly shows the
  previous run's last step) and clearing it on `RunFinished` and `entry_released()` alike, so nothing claimed next
  can read a stale one. `monitor.proto`'s `QueueEntrySnapshot` gains the two fields (both set together, or neither).
- **Change** [M02]: Milestone 02 is implemented and done: `server/` (the FastAPI app and its routes, serializers,
  auth, host check, pagination, the caller header, error-code-to-status mapping, the audit context manager, and the
  event backlog), the gRPC monitoring service (`server/grpc_service.py`, `server/grpc_auth.py`, generated from
  `server/proto/monitor.proto`), `dtc serve` (`server/serve.py`, wired into `cli/app.py`), `services/api_rules.py`,
  `services/input_listing.py`, `services/queue_events.py`, `services/queue_worker_status.py`, and schema 5's
  `audit_log` table (`state/audit.py`). `make proto` (protoc, then a `sed` fix for its non-package-aware import) is a
  new `make check` dependency; its output is `.gitignore`d and excluded from the architecture and size tests, as
  planned.
- **Design decision** [M02]: Two signal-handling bugs found while smoke-testing a real `dtc serve` process (curl,
  a gRPC client, real SIGTERM/SIGINT) before calling the milestone done, both in `server/serve.py`'s `_serve_async`.
  Starting `grpc.aio.server()` *before* uvicorn's `Server.capture_signals()` is entered leaves SIGTERM and SIGINT at
  their default, immediate-kill disposition for the rest of the process's life (observed empirically: grpc's C core
  appears to reset it during its own startup), so no cleanup ever ran, uvicorn's own included. Fixed by entering
  `capture_signals()` first and starting the gRPC server inside it, awaiting the private `Server._serve()` instead of
  the public `Server.serve()` (which would re-enter `capture_signals()` itself). Separately, even with that fixed, the
  run lock still was not released: `capture_signals()`'s own `finally` re-raises the caught signal, at its restored
  default disposition, once its `with` block exits, which happens *before* control returns to any caller code sitting
  outside that block — so cleanup written in `run()`'s own `finally` (after `asyncio.run()` returns) raced the
  re-raised signal and typically lost. Fixed by moving `QueueHost.stop()` into `_serve_async`'s own `try/finally`,
  nested inside the same `capture_signals()` block, where it is guaranteed to complete first; `run()`'s outer
  `finally` is now only a backstop for a non-signal exit. Neither bug is one a synchronous or in-process async test
  can catch; `tests/server/test_serve.py` covers both with a real subprocess and real signals.
- **Change** [M02]: `EventBacklog.since()` (`server/event_backlog.py`) read `last_event_id < self._events[0].id - 1`
  to decide whether an ID is too old to explain and needs a `Reset`, which on an empty backlog short-circuited past
  the check and returned `()` (replay nothing, stay subscribed) instead of `Reset` for a stale ID from a previous
  server run — exactly the case the wall-clock-seeded ID scheme exists to catch without a separate run token. Fixed
  to compare against `self._next_id` when the backlog is empty, caught by a test that reconnects with an ID from
  before the server started.
- **Design decision** [M02]: `QueueWorker` (`services/queue_worker.py`) gains `is_alive()`, `state()`,
  `cooldown_until()`, and `current_run()`, for `GET /health`, `GET /queue`, `GET /queue/{id}`, and `WatchQueueEntry`
  to read without parsing logs, as the architecture rules require; the bookkeeping is a `WorkerStatus` object
  (`services/queue_worker_status.py`), extracted to its own module to keep `QueueWorker` under the 250-line class
  limit. The worker also takes an optional `on_event` sink (`services/queue_events.py`'s `QueueEventPublisher`),
  forwarded through `QueueHost` (which gained a public `store` property for the same routes), publishing Phase 2 job
  events and the queue's own transitions (entry state changes, the between-jobs wait's start and end) for
  `WatchEvents` to stream. None of this was named at the level of individual methods in the milestone plan, which
  described the endpoints and RPCs but not the service-layer surface they read from.

- **Owner decision** [M02]: From an interview on the milestone plan's review below.
  - `GET /executions`' job filter matches a job reference (an ID or file name, resolved exactly like `{job}`
    elsewhere), not free text against `name:`. Filtering by the display name field, which could span several files
    sharing one, was offered.
  - `GET /health` stays 200 with `worker_alive: false` when the worker thread has died, so it remains a plain
    liveness check whose body a caller must read. Reading 503 once the worker is dead was offered.
  - The caller-identification header is `X-Dtc-Caller`, restricted to `cli`, `tui`, or `mcp`; absent defaults to
    `api`, and any other value is refused with `invalid_input` naming the field. Storing whatever string a caller
    sends, unvalidated (recommended), was offered: the owner wants the audit log's caller column kept to a known set.
  - The pagination cursor (`GET /jobs`, `/executions`, `/inputs`, `/audit`) is a server-chosen opaque token, so the
    scheme behind it can change later without a contract break. Treating the last row's own public ID as the cursor
    (recommended: simpler, and no leak since the IDs are already shown) was offered. Default and maximum `limit` are
    both 200, matching `services/history.py`'s existing `PAGE_SIZE`.
- **Design decision** [M02]: `CancelRefusedError` and `ResumeRefusedError` ([M01](milestone-01-queue-run-manager.md))
  gain their own `code = "invalid_state"`, so the API's error table (`invalid_state` → 409) has something to key on;
  both were plain `InputError` subclasses with no `code` of their own, which would otherwise read `invalid_input`
  (422). `EXIT_CODES_BY_ERROR_CODE` (`core/exit_codes.py`) also gains a row for each of Milestone 02's four new error
  codes (`timeout_required`, `outside_directory`, `limit_exceeded`, `invalid_state`), all exiting 2
  (`EXIT_INVALID_INPUT`), since Milestone 03's `dtc queue` already assumes that table covers them when it turns an
  API error into an exit code.
- **Change**: `ResumeRefused` and `CancelRefused` are now `ResumeRefusedError` and `CancelRefusedError`, like every
  other exception in the package; the M01 design decision below, that they are `InputError`s and not
  `NotFoundError`s, is unchanged.
- **Change** [M01]: `_fail_to_start` now clears a queue entry's linked execution number when it fires (the job never
  reached `JobStarted`, so no execution row was ever created for the number reserved for it), so a later resume
  reads the entry as never having started rather than mistaking the dangling number for one that ran and was pruned.
  `submit_job` also wraps a bad or since-removed base configuration name in `InputError`, matching every other
  refusal `load_job_text` raises.
- **Change** [M01]: `QueueRepository.requeue`, used by restart recovery for the identical window (a number reserved,
  then the crash landing before the execution row was inserted), left the entry's `execution_number` in place; only
  `finish`'s own `clear_link` cleared it, for the in-process exception path. Recovery could requeue an entry with a
  dangling link, and a later cancel-then-resume of it would read `_linked_execution`'s "row missing" as "pruned" and
  refuse the resume outright, instead of the "never started" outcome the design above intends. `requeue` now clears
  `execution_number` too, matching `finish(..., clear_link=True)`.

## 2026-09-27

- **Change** [M01]: Milestone 01 is implemented and done: schema 4 (the `queue` table, and `first_run`/`resumes` on
  `executions`), `state/queue.py`'s `QueueRepository`, the worker, resume, restart recovery, and the host in
  `services/`, `JobExecutor`'s resume support, the job log's scoping, and the busy message naming the server. No
  front end reaches it yet (Milestone 02's API is what will call `submit_job`, `resume_entry`, and `cancel_entry`);
  tests drive them directly, with a fake runner and real (short) waits for the between-jobs cooldown.
- **Change** [M01]: A review against the acceptance criteria found and fixed three worker bugs before calling the
  milestone done: `stop()` did not keep a cancel pending for a job still between the claim and `begin()` (it stopped
  a job already running, but let one caught in that narrow window run to completion); the between-jobs wait
  re-parsed the entry's snapshot, which re-checks the job's own input file and could raise out of `claim_and_run_one`
  (never re-parses now: `_run_claimed` returns the already-parsed job for the wait to reuse); and `claim_and_run_one`
  could claim an entry after a shutdown was already requested and run it anyway (it now requeues that entry
  instead, untouched, matching the no-linked-execution recovery case). Test coverage was also completed: cancel
  between the claim and the start, cancel during the cooldown between runs, the between-jobs wait's exact seconds
  (an injectable `wait_between_jobs`, as the executor's own cooldown is faked), a resume run through the real worker
  with real recorded rows (not hand-built ones), an accepted resume surviving its ancestor being pruned, and the
  orphaned-child refusal through `QueueHost` (a `child_check` parameter, forwarded to `RunLock`, as `run-job`'s own
  tests already use).
- **Design decision** [M01]: An entry linked to an execution that is later pruned keeps its stored `execution_number`
  pointing at that now-missing row, rather than being cleared. This supersedes the milestone document's "an entry
  whose execution was pruned keeps no link to it": a cleared link cannot be told apart from an entry that never
  started, which is exactly the distinction `resume_entry` needs ("no succeeded run" versus "was pruned"); a plain
  number, unlike a foreign key, safely points at nothing once its row is gone (see the `queue` table's own comment in
  `state/schema.py`).
- **Design decision** [M01]: `ResumeRefused` and `CancelRefused` are `InputError`s, not `NotFoundError`s: the entry
  named always exists (a missing one is a separate, plain `NotFoundError`), so refusing to resume or cancel it is
  invalid input, not a 404, for whichever status-code table Milestone 02 builds.
- **Design decision** [M01]: `submit_job` parses the job twice: once from disk to learn the base configuration's name
  and validate the input, then again from the exact text about to be stored (`base_config_text`), so a base
  configuration edited between the two reads cannot be captured half-written; what is stored is validated in the
  form it is stored, not merely read twice.
- **Design decision** [M01]: A resumed execution's own `JobFinished.total_runs` and `.completed_runs` (and so
  `ExecutionRow.total_runs` and the `succeeded` count SQL already gives) stay what they were before this milestone:
  `total_runs` is the whole chain's `run_count` (unchanged by where it starts), and `completed_runs`/`succeeded` count
  only this leg's own runs, from `first_run` on, since that manifest holds only those. Read alone, this understates
  the chain's true progress once `first_run > 1` (a completed 3-run chain resumed at run 2 would show "2/3", as if
  unfinished), so every display of it adds back the runs before `first_run` (all of them succeeded, by construction,
  in whichever execution ran them): the job's own log line (`JobLogWriter`) and the TUI's Execution History
  (`tui/text/history.py`) both show `first_run - 1 + completed_runs` of `total_runs`, and the Execution widget's
  detail names the execution it resumes so the rest of the chain can be found. `import-history` reads a manifest's
  own `total_runs` (a new manifest field, since a resumed manifest's `runs` list holds only its own) rather than
  `len(runs)`, so a re-imported resumed execution's `total_runs` is the whole chain's too, not just the leg the
  manifest recorded; a manifest from before this field falls back to `len(runs)`, correct for the plain (never
  resumed) case every such manifest is. `dtc queue`/the HTTP API (Milestone 02+) must add the same when they report a
  resumed entry's progress.
- **Design decision** [M01]: The TUI shows a resumed execution's `resumes` and `first_run` in its detail (Execution
  History), and its Status widget's "another process" line becomes the server's own busy message, PID included, only
  when the server itself holds the lock; an ordinary `run-job` or TUI holder elsewhere keeps the generic wording,
  unchanged. A `lock_holder`/`lock_holder_message` pair in `core/run_lock.py` reads the lock file's holder without
  taking it, for this and any future read-only display.
- **Design decision** [M01]: A resumed queue entry stores the execution number its resume point's last succeeded run
  came from (`resumes_execution`), resolved once at `resume()`, separately from `resumes` (the queue entry it
  continues, for walking the chain). The two differ once a resume-of-a-resume's immediate ancestor never itself
  succeeded: the execution "it resumes" (shown on `JobStarted`, the manifest, and the resumed execution's own
  `resumes` column) is the one further back that actually has the succeeded run, not the immediate ancestor. The
  first plan named only one link.
- **Design decision** [M01]: `state/executions.py` was split: the row dataclasses (`ExecutionRow`, `RunRow`,
  `NewExecution`, `NewRun`, `ExecutionSettings`) moved to `state/execution_rows.py`, re-exported from
  `state/executions.py` so nothing that already imports them changed; adding `first_run` and `resumes` would
  otherwise have pushed the module over the 400-line limit.
- **Design decision** [M01]: The job log's scoping (a Loguru sink that copied every message) uses
  `logger.contextualize(dtc_job=True)` around the executing job (`JobRecords.open`), with the sink filtering on that
  key. This works without a per-job token because everything a job logs happens on the one thread that runs it
  (contextvars are not inherited by the reader threads the process runner spawns, but those never call the logger
  themselves, only queue raw lines); a server's own lines, logged outside that block, never carry the key.
- **Design decision** [M01]: The wait between queued jobs, and the worker's idle wait for the next submission, poll a
  `threading.Event` in small slices (20 ms) instead of the wake-pipe scheme `CancelToken.wait` uses for the wait
  between runs, so the between-jobs wait can also end early when the queue empties (a cancel of the entry it was
  waiting for), which a fixed-duration wait cannot observe on its own without re-checking.
- **Owner decision** [M01]: From an interview on the milestone document's review below. A `running` entry left with no linked execution at all (the crash landed before `JobStarted`) is re-queued (recommended); marking it `interrupted` or `failed` instead were offered. A resumed entry lands at the back of the FIFO queue, as any new submission does (recommended); jumping it ahead of other queued work was offered. A `cancel(id)` that races the job's own natural end leaves the entry reading the real outcome, `succeeded` or `failed` (recommended); always forcing it to `cancelled` regardless of timing was offered.
- **Change** [M01]: The milestone document was reviewed against the code and fixed; still no code. The decisions below record each fix; no earlier decision changed.
- **Design decision** [M01]: Restart recovery gains two cases the first plan missed. A `running` entry whose execution already reads `interrupted` (a graceful shutdown closed the execution before the process died) takes that status, the same as an execution already `succeeded` or `failed`. A `running` entry with no linked execution at all (the crash landed between the worker's claim and `JobStarted`, before the recorder's row existed) is put back to `queued`: nothing of it ran, matching the 2026-09-25 owner decision that a job "queued and never started" is re-queued automatically. The worker links the entry to its execution as early as `reserve_execution_id` fires, not only on `JobStarted`, to keep this window short.
- **Design decision** [M01]: Restart recovery also depends on the existing run-lock orphan-child guard: a server that starts while an earlier server's `draw-things-cli` is still alive refuses to start, naming the PID, before any of the recovery above runs. The first plan never named this dependency or tested it.
- **Design decision** [M01]: Cancel on a `running` entry does not unconditionally read `cancelled`. There is no transitional state while it stops; the entry reads whatever `JobFinished` actually reports. A cancel that lands after the job's last run has already finished changes nothing (`JobExecutor.cancel()`'s own contract), so that race leaves the entry `succeeded` or `failed`, never `cancelled`. A second `cancel(id)` on an entry already stopping is a no-op, not an error.
- **Design decision** [M01]: A resumed execution's manifest holds only the runs it made, starting at position 0 for its own first run, so list position is no longer the run number once a manifest can start above run 1. `import-history` must number a resumed manifest's runs from its `first_run`, or reimporting one after the database is lost (the case it exists for) misnumbers every run of it. The first plan did not say `import-history` needed a change.
- **Design decision** [M01]: `JobRunSession.run` gains the resume point alongside `JobRunOptions`, since it builds `JobRunOptions` itself and is the worker's only entry point into a run. The first plan named only `JobRunOptions`.
- **Owner decision** [M01]: From an interview on resume, after the research on what `draw-things-cli` can resume ([draw-things-cli-resume.md](../research/draw-things-cli-resume.md)).
  - Stopping the server stops the running run at once, as planned; a resume reruns it from its start. Letting the first Ctrl-C finish the run (recommended), continuing an entry stopped that way on restart, and finishing the whole job were offered.
  - Cancelling a running entry stops it at once, as planned. An after-run cancel beside it (recommended), and after-run as the default, were offered.
  - No automatic retry: a failed run ends the job, and resume stays explicit. Retrying a failed run once, and resuming a failed entry once on its own, were offered.
  - A run that did not succeed keeps its leftover output file, as since Phase 1, and the API marks it incomplete. Renaming it `-partial`, and deleting it, were offered.
  - Finished entries stay pruned with the history, so an entry left past `history_retention_days` can no longer be resumed. Keeping resumable entries until they are resumed or dismissed was offered.
- **Owner decision**: From the same interview.
  - No pause: stopping the server stops the queue. Pause and unpause, and a `serve --paused` start option, were offered.
  - The API shows every execution in the history with its prompts, whichever front end ran it; showing only the queue's executions was offered.
  - Milestone 03 is built right after Milestone 02 (order 01, 02, 03, 07, 08, 09), so the owner has the queue before agents can write jobs, and Milestone 09's review covers `dtc queue`. Building it last was offered.
- **Design decision** [M01]: Resume, re-planned from the research.
  - The smallest unit that can be resumed is one run. `draw-things-cli` writes a run's output only when the run ends, and has no resume, checkpoint, or partial output of its own, so a resume reruns the run that was cut short, and only a stop during a cooldown between runs loses nothing.
  - A resume starts only from a succeeded run's file: a killed run may leave a truncated video at its output path.
  - The rerun keeps the seed, but identical pixels are not promised, since that was not verified.
  - A model download cut short is continued by `draw-things-cli` itself, since jobs never turn `--download-missing` off.
  - The shutdown grace is kept, though this build of `draw-things-cli` dies at once on `SIGTERM`.
- **Design decision** [M02, M08]: What an agent needs to act on this.
  - `GET /executions/{id}/outputs` says whether each output is complete, which it is only when its run succeeded.
  - `GET /queue/{id}` says whether the entry can be resumed, from which run, or why not.
  - `GET /inputs` lists images in sub-directories too, by the relative path a job's `input` takes.
  - The MCP server gains `get_capabilities`, which its tool descriptions already pointed agents to. Tool descriptions say that a cancel loses the run in progress and that a resume reruns it.

- **Owner decision**: From an interview on the revised plans.
  - Agents do not continue a finished chain with a new job: no output becomes a new job's input, and only a resume continues from one, so an agent writes every prompt of a chain into one job. This settles the open question below, which the phase document drops. A `continue_from: E0012` job key and inputs from the output directory were offered.
  - People get the queue too, in a new Milestone 03: `dtc queue add`, `list`, `show`, `cancel`, and `resume` call the server over HTTP, and the TUI shows a read-only Queue widget with `/get queue` and `/describe Q0007`. Keeping the queue out of the CLI and the TUI (recommended), only one of the two, top-level commands, and a `dtc queue watch` were offered.
  - The Queue widget is always shown, 5 rows, in the right column between Job Definition and Execution History, and the minimum terminal size grows by its height (to about 80x41). Taking the draw-things-cli pane's place while the server holds the lock (recommended), and showing the widget only while the queue has entries, were offered.
  - A job queued with `dtc queue` meets the API's rules and limits, as an agent's does, since the API cannot tell them apart: they share the token, and the caller header names itself. A second token that only `dtc queue` reads, exempting it, was offered.
  - The wait between queued jobs follows only a job that succeeded, and only when the next entry is already queued as it finishes; a job submitted later starts at once. This keeps the first plan and supersedes the [M01] design decision below that waited after any job that ran, measured from its finish (recommended). Waiting after any job but a cancel was offered too.
  - The server keeps `--allow-remote-bind`: a non-loopback `--host` is refused without it. This supersedes the [M02] design decision below that dropped it, so the [M02] design decision of 2026-09-25 stands. Loopback only (recommended) was offered.
  - Confirmed as recommended: limits of 100 runs and 48 h (50 runs and 12 h, and no time cap, were offered); resume of `interrupted`, `failed`, and `cancelled` entries (without `cancelled`, and `interrupted` only, were offered); `POST /validate` without `--allow-write` (only with it was offered); and no resume of executions that `run-job` or the TUI started (resuming any execution through the queue was offered).
- **Design decision** [M01]: The wait between jobs lives in the worker. Shutdown ends it, so a restart starts the next queued job without it, and a wait that finds no entry queued any more (the one waiting was cancelled) ends, since no wait starts for an empty queue. An entry keeps no `cooldown_until`; `GET /queue` reports the worker's.
- **Design decision** [M02, M08, M03]: Since the server may listen beyond loopback, its clients take `--server-url`, and `dtc mcp` and `dtc queue` refuse a non-loopback one unless `--allow-remote-server` is given, the client's side of `--allow-remote-bind`, so a mistyped URL cannot send the token elsewhere. With `--allow-remote-bind`, `serve` warns that the token crosses the network in plain HTTP, and the `Host` check accepts the bound address.
- **Design decision** [M03]: The TUI reads the queue from the state store through a reader in `services/`, not over HTTP, so it shows the queue whether or not the server is up, as it shows the history. `dtc queue` goes through the API, so the rules, the limits, and the audit log apply (caller `cli`). Its HTTP client is its own, in `cli/`: front ends never import each other, and `mcp_server/` imports nothing else from the package. It takes a job ID, a job file's name, or a path to a file directly in `data/jobs/`.
- **Change** [M03]: Milestone 03, "Queue for people", is planned.

- **Change**: The Phase 3 plans were reviewed against the code and revised; still no code. The decisions below record each change. The phase document gains an open question for the owner, continuing a chain with a new job, which the owner decision against reusing outputs as inputs rules out today. Owner decisions are unchanged.

- **Design decision** [M01]: A queue entry's snapshot also holds the text of its base configuration, and the parser takes it from there instead of `data/params/`. `load_job_text` reads `config_file` from disk, so the stored job text alone did not make "what runs is what was validated" true: an edited base configuration would change a queued job, and a resume would continue a chain with other settings. Storing the parsed `JobDefinition` stays rejected, as before.
- **Design decision** [M01]: The worker, resume, the submission rules and limits, and the event backlog live in `services/`, not `server/`, so they have no web framework and are tested without one; `server/` holds only HTTP. This replaces the phase document's "`server/` holds the API and queue worker".
- **Design decision** [M01]: Queue entries have public IDs (`Q0007`) from a counter, as executions and jobs do, and the API names executions by `E0012`; store row numbers stay internal, as Phase 2 decided. The `position` column is dropped: the order is by ID, and nothing reorders the queue.
- **Design decision** [M01]: The wait between queued jobs follows every job that started a run, however it ended. It is measured from when the job finished, so a job submitted during it starts when it ends, a restart honors what is left, and an empty queue does not end it. The first plan skipped it after a failed or cancelled job and when the queue was empty, which let the next job start on a hot GPU; the owner decision (the server waits the finished job's cooldown before starting the next) names no exception. A job that failed before run 1 is followed by no wait. Cancelling during the wait cancels the queued entry named, not the wait.
- **Design decision** [M01]: Resume, completed.
  - A `cancelled` entry can be resumed too, so stopping a long chain to free the GPU does not lose it.
  - It starts after the last succeeded run of the chain, following earlier resumes back, and an entry can be resumed once.
  - It is refused when the job's own first input is gone (parsing the snapshot resolves the job's size from it) or when its execution was pruned.
  - `JobRunOptions` gets a resume point with the seed, since the planner draws a new random seed for a job that sets none, and the executor skips run 1's resized copy.
  - A cancelled entry's execution reads `interrupted`, as a Ctrl-C does, and a run stopped from outside makes the entry `failed`.
- **Design decision** [M01]: Three gaps the code shows.
  - `JobExecutor.cancel()` does nothing before the job has begun, so a cancel that lands between the worker's claim and the job's start is kept and applied at the start, as the TUI does ([Phase 2 Milestone 04](../archive/phase-2/milestone-04-tui-live-run.md)).
  - The job log file is a Loguru sink that copies every message of the process; in the server it would take in API lines, so it is scoped to its job.
  - The busy message names the server and how to free the lock, instead of `Another run is in progress` while the queue is idle.
- **Design decision** [M02]: The server binds to loopback only, and `--allow-remote-bind` is dropped. This supersedes the [M02] design decision of 2026-09-25. Beyond loopback the bearer token would cross the network over plain HTTP, which the non-goals already exclude; an SSH tunnel serves remote use. A `Host` header check keeps web pages out through DNS rebinding.
- **Design decision** [M02]: `serve` gains `--executable` and `--shutdown-grace`, since it runs jobs as `run-job` and `tui` do. It loses `--data-dir`: the server serves the project's `data/jobs/` only, the one directory with job IDs and the one Milestone 07 writes.
- **Design decision** [M02]: A job is named by its job ID or its file name, resolved against the listing of `data/jobs/` with `JobCatalog.find`, never joined to a path. Its `name:` field is not an identifier, since two files can share one and a file's name need not match it. Symbolic links in `data/jobs/` are not read, since a YAML error quotes the file.
  - `/history` becomes `/executions`, and `/outputs/{execution_id}` becomes `/executions/{id}/outputs`.
  - `POST /jobs/{name}/validate` is dropped: `GET /jobs/{job}` returns validity, and Milestone 07 adds `POST /validate` for drafts.
- **Design decision** [M02]: One set of rules for every job the API runs or writes, checked at submission, resume, and write: `run_timeout_seconds` is set (owner decision), the input is inside the input directory (owner decision), and, new, `output.directory` is inside the global output directory. A job's `output.directory` may be absolute or climb out with `..`, so an agent-written job could otherwise create directories anywhere. The first plan checked the input directory only on writes.
- **Design decision** [M02]: The limits move under an `api_limits` mapping in the global configuration, with defaults for long chains: `max_job_runs` 100 (was 50) and `max_job_seconds` 172800, 48 h (was 43200, 12 h). The worst case counts each wait between runs as the job's cooldown applied to `run_timeout_seconds`, since a wait follows only a run that succeeded within it. At 12 h, a job with the example job's settings (`run_timeout_seconds: 3600`, the default `auto` cooldown, so at most 1800 s a wait) could have at most 8 runs over the API (8 × 3600 + 7 × 1800 = 41,400 s); at 48 h it can have 32, which suits the long chains Phase 3 is for. A resume counts the runs it has left.
- **Design decision** [M02]: Smaller API decisions.
  - Error codes map to HTTP statuses in one table in `server/`, as they map to exit codes for the CLI. The new codes are `DtcError`s.
  - `GET /queue/{id}?wait=N` waits at most 30 seconds for a change, so agents need not poll fast; no call waits for a generation.
  - Event IDs name the server's run, so a `Last-Event-ID` from before a restart gets `reset`.
  - The OpenAPI schema is behind the token, and the interactive docs are off.
  - Each milestone writes its part of the user guide as it lands, as the documentation rules require. The first plan left it all to Milestone 09.
- **Design decision** [M07]: Drafts and writes.
  - `POST /validate` checks job text exactly as a write would and is always available, since it writes nothing, so an agent can fix a draft before writes are on.
  - Writes and deletes reach only `data/jobs/<name>.yaml` for a valid job name, never a file the owner named otherwise.
  - A file with the same stem in another letter case or suffix makes a create or replace fail with 409, since the catalog finds jobs by stem and macOS file systems usually ignore case.
  - A replace with unchanged text makes no backup.
  - `.gitignore` gains `data/jobs/.trash/` and `data/jobs/.backups/`: `data/**` ignores them, but `!data/**/*example*` would bring back a trashed example job.
- **Change** [M07]: `.trash/` and `.backups/` are under `data/jobs/`, where Phase 2 moved the job files. The owner decision of 2026-09-25 named `data/.trash/` and `data/.backups/` when job files were in `data/`; the decision itself is unchanged.
- **Design decision** [M08]: `mcp_server/` imports nothing else from the package, so `cli/app.py` passes it the server URL and the token file's path, and `dtc serve` and `dtc mcp` become allowed imports between front ends, as `dtc tui` is.
  - `dtc mcp` keeps stdout for the protocol.
  - The tool list is read from `/capabilities` whenever a client asks for it, not once at start, so it follows a server restarted with or without `--allow-write`.
  - Tools are named after the endpoints (`get_queue_entry`, `validate_job_text`).
  - The full flow is tested in process, through the SDK's in-memory session and `httpx` sent straight to the app, with one test over stdio against a process. An end-to-end stdio test against a live API would need a listening server in the tests.
- **Design decision** [M09]: The credential test changes. A job cannot produce `--api-key` or `--remote-shared-secret`, since its keys reach only the override flags, so "submit a job whose command would include them" cannot be written. A structural test checks that no job key reaches a secret flag, and a run stored with an unredacted command must be served redacted. The acceptance criterion that `README.md` documents the server becomes the user guide, since the README stays short.

- **Change**: Phase 2 is done and archived under `docs/archive/phase-2/`; links from these documents now point there. The plans were checked against the code: `load_job_text` and the `lock` parameter of `JobRunSession.run` already exist, so Milestone 01 no longer lists them as new. `start_run` and the input override in `JobRunOptions`, the `queue` table, and the `fastapi`, `uvicorn`, `httpx`, and `mcp` dependencies are still to build. No decision changed.

- **Change**: [Phase 2 Milestone 11](../archive/phase-2/milestone-11-clean-architecture.md) renamed and moved the code these plans name: `JobService` is `JobExecutor` (with `JobRunOptions`), `jobs/job_definition.py` is `jobs/parsing.py`, and `install_signals` is `handle_signals`. The queue is a repository in `state/` with a worker on `JobRunSession`, not `jobs/job_queue.py`. The milestone documents were updated to match; no decision changed.

- **Owner decision** [M01, M03]: Whether the TUI could start a run directly while the server is up was reconsidered and rejected: the server keeps the run lock for its whole lifetime, unchanged. The TUI gets a run while the server is up by submitting to the queue instead, as `dtc queue` already did.
- **Owner decision** [M03]: The TUI's Queue widget stops being read-only. `/queue add [JOB]`, `/queue cancel Q0007`, and `/queue resume Q0007` (and a `c` shortcut for cancel on a selected row) call the API exactly as `dtc queue` does: the same rules and limits, the same error shown inline naming `dtc serve` when it cannot be reached, and caller `tui` in the audit log. This supersedes the design decision above that it "reads the queue from the state store... It never writes" and the "offers no action" acceptance criterion of the same date; reading still falls back to the state store, read-only, while the server is down.
- **Design decision** [M02, M03, M08]: A gRPC monitoring service (`grpc.aio.server()` beside uvicorn in `dtc serve`, its own loopback port and token interceptor) replaces `GET /events` and `GET /queue/{id}?wait=` (the design decision and the Event stream section above) as the one way to watch for change, for every client: the TUI's Queue widget, `dtc mcp`'s `get_queue_entry(wait_seconds=...)`, and any future one. SSE was rejected even though it needed no new dependency and already had a full design in this file (per-run event IDs, `Last-Event-ID` resume, a bounded backlog, `reset` on a gap); WebSocket was rejected too, needing a new client-side dependency and its own hand-built version of that same resumption scheme, for no gain over SSE's. gRPC was chosen instead, for one typed, code-generated contract every client shares, at the cost of a new dependency (`grpcio`, dev-only `grpcio-tools`), a second port, and a second auth mechanism to build and test.
- **Owner decision** [M01, M03]: Tightened further: the TUI never invokes `draw-things-cli` directly, full stop, not only while a server happens to be up. This supersedes the entry above conditioned on "while the server is up": submitting to the queue is now the TUI's only way to run a job, and it requires the server to be reachable, whether or not one happens to be running right now.
- **Owner decision** [M03]: `run-job` is retired for the same reason: `dtc serve`'s worker becomes the only thing that ever invokes `draw-things-cli`, across every front end. `dtc queue add` gains `--wait`, submitting then watching the entry over gRPC to completion and exiting with its outcome code, as `run-job`'s replacement; anything that scripted `run-job` must move to it, and now needs a running `dtc serve` that `run-job` never did. Milestone 02's "`run-job` and the TUI... are not limited" no longer holds once this lands: nothing runs a job unlimited any more, since nothing runs one outside the queue.
- **Design decision** [M03]: `dtc queue` gains a gRPC client after all, for `add --wait` alone; `list`, `show`, `cancel`, and `resume` stay HTTP-only. This supersedes "`dtc queue` gains no gRPC client: `dtc queue watch` stays not chosen" above: `add --wait` is not the rejected `dtc queue watch` (it follows only the one entry just submitted, in the foreground, and exits when that entry finishes, rather than following the whole queue indefinitely).
- **Owner decision** [M01]: Whether the run lock's mechanism (a cross-process `flock`, with the holder and its child's PID recorded) should be simplified, now that only the server's worker ever takes it, was raised and rejected: kept as-is. It still stops two `dtc serve` instances on one project, and the child-PID check still backs the server's own crash recovery, an orphaned `draw-things-cli` after a killed server.
- **Design decision** [M02, M03, M08]: The gRPC `.proto`'s generated stubs are not committed. `make proto` (`grpcio-tools`, dev-only) regenerates them into a `.gitignore`d directory, and `make check` depends on that step. Committing them, so `make check` never needs `grpcio-tools` installed, was considered and rejected: it risks the checked-in code drifting from a `.proto` edited without a regeneration, which not committing them cannot do.

## 2026-09-25

- **Change**: Phase 3 is planned. The phase document and five milestone
  documents are written; no code yet.

- **Design decision** [M01]: A queued job stores its exact YAML text and
  resolved settings, and the worker re-parses the text when the job starts
  (`load_job_text`). Serializing a parsed `JobDefinition` was rejected (see
  the Phase 2 changelog). Input files are checked at start, so a file removed
  while queued fails the job with its path named.
- **Design decision** [M02]: uvicorn owns signal handling; jobs run with
  `install_signals=False`, and uvicorn's shutdown hook calls `cancel()`.
- **Design decision** [M02]: The server refuses a non-loopback bind unless
  `--allow-remote-bind` is given, so a mistyped `--host` cannot expose job
  control on the network.
- **Design decision** [M07]: Agents submit job YAML text, not structured
  fields, and the server validates it with the same code as `validate-job`
  before writing the text unchanged. The alternative, a structured JSON job
  schema, was rejected because it would need a second definition of a job.
- **Design decision** [M01]: The resolved job definition is snapshotted into
  the database when a job is queued. Editing or deleting the YAML file
  afterwards cannot change a queued or running job, and editing or deleting a
  queued or running job's file is refused anyway.
- **Design decision** [M01]: Resume creates a new queue entry linked to the
  interrupted one, starting at the first unfinished run with the last
  succeeded run's last frame as its input. It is refused when that file is
  gone. Automatic resume was rejected (below).
- **Design decision** [M02]: Limits (queue length, runs per job, total
  runtime, file size) are built with the first submission endpoint, not
  after the write tools, and apply even with the write flag on, because a
  valid job written by an agent can still run for many hours. (They were
  first planned for M09; moved so nothing that accepts agent input ships
  without them.)
- **Design decision** [M02]: Every write action and every submission is
  recorded in an `audit_log` table (time, action, job name, outcome), without
  credentials or prompt text. Built with the first endpoints, like the limits
  (first planned for M09); M09 tests that coverage is complete.

- **Owner decision**: The server holds the run lock for its whole lifetime,
  not per job. While `serve` is up, the CLI and the TUI cannot start runs,
  even when the queue is idle; to run a job by hand, stop the server.
  Holding the lock per job (idle server does not block, worker waits when a
  hand-run holds it) was recommended and not chosen.
- **Owner decision**: A job submitted or written through the API must set
  `run_timeout_seconds`, so its worst-case runtime is bounded. There is no
  global default timeout. CLI and TUI runs are unaffected. A global default
  and an assumed per-run cost were the alternatives.
- **Owner decision**: The API token lives in `config/server-token`
  (0600, gitignored, generated on first `serve`), not in
  `global-config.yaml`, so the secret stays out of a file that may be shared
  or committed. The owner first said "token in global config"; the file
  was proposed and chosen. The MCP server reads the same file.
- **Owner decision**: History retention (14 days by default, overridable
  with `history_retention_days`) also applies to finished queue entries. The
  audit log is exempt; see the Phase 2 changelog for the setting.
- **Owner decision**: Interrupted jobs are marked `interrupted`
  after a crash or restart and can be resumed with an
  explicit action. Jobs that were queued and never started are re-queued
  automatically. Automatic resume of interrupted jobs was rejected: an
  unattended restart must not start heavy GPU work by surprise. Resume is a
  new feature; phase 1 listed resuming a job as a non-goal.
- **Owner decision**: The cooldown also applies between queued jobs: after a
  job finishes, the server waits that job's cooldown before starting the
  next one, holding the run lock. A separate `job_gap_seconds` setting was not
  chosen.
- **Owner decision**: The MCP server is a thin client of the HTTP API, so
  one server process owns the GPU and the queue. Calling the service
  in-process from MCP was not chosen because a second process could collide
  on the GPU.
- **Owner decision**: The queue and job state live in the SQLite state store
  from Phase 2, and the queue survives restarts.
- **Owner decision**: Agents may create, edit, and delete job files, confined
  to `data/`. Delete moves the file to `data/.trash/`, and every edit or
  overwrite keeps the previous version under `data/.backups/`. These
  actions need an explicit server option (`--allow-write`); without it the
  write tools and endpoints do not exist.
- **Owner decision**: The server runs in the foreground, binds to 127.0.0.1,
  and requires a bearer token. Wrapping it in launchd is left to the user.
- **Owner decision**: Agents may use only input files already in the
  configured input directory. There is no upload endpoint and no reuse of
  outputs as inputs.
- **Owner decision**: Agents get paths and metadata for outputs, not file
  contents or thumbnails.
