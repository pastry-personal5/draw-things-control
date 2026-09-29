# Milestone 05: Park and hold

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done
**Depends on:** [Milestone 01: Queue and run manager](milestone-01-queue-run-manager.md) (the worker, cancel, and
resume) and [Milestone 03: Queue for people](milestone-03-queue-for-people.md) (`dtc queue`, the Queue widget, and
the shared `WatchEvents` feed the TUI follows jobs with).

## Goal

Let a person stop a running job without losing the run in progress. Today a cancel, `/stop`, or stopping the server
kills `draw-things-cli` at once, and the run it was making is lost, since `draw-things-cli` keeps nothing of a run
it did not finish ([research](../research/draw-things-cli-resume.md)). A long chain's run can take many minutes of
GPU time.

A person *parks* a running entry instead. The job goes on until its current run ends, then stops, keeping every run
it finished. A resume continues it at the next run and reruns nothing. Parking also *holds* the queue, so nothing
else starts on the GPU until the person *releases* it. The queue can also be held on its own.

## Terms

The owner asked for a term of its own, not "interrupted" or "stopped", for ending a job at a run boundary. The
owner chose "park" from park, land, wrap, and dock.

| Term | Meaning |
|------|---------|
| park (verb) | Ask a running entry to end once its current run finishes: `/queue park Q0007`, `/park`, `dtc queue park Q0007` |
| park reservation | The pending request to park an entry, from the moment it is made until the entry parks, finishes some other way, or the reservation is withdrawn |
| parking | How a running entry with a park reservation is shown (the Status widget, the Queue widget's State column, `dtc queue list`). The stored state stays `running` |
| parked | A final state, of the queue entry and of its execution: the job ended at a run boundary because it was parked. It can be resumed |
| unpark | Withdraw a park reservation before it takes effect; the job runs on: `/queue unpark Q0007`, `/unpark`, `dtc queue unpark Q0007` |
| hold, held | The queue is held: the worker starts no new entry. A running job is not stopped by a hold: `/queue hold`, `/hold`, `dtc queue hold` |
| release | End a hold: queued entries start again: `/queue release`, `/release`, `dtc queue release` |

None of these words is used yet as a state, status, or command. The words already in use are `queued`, `running`,
`succeeded`, `failed`, `cancelled`, `interrupted`, `stopping`, `cooling_down`, `idle`, `finished`, `ended`,
`not_started`, `starting`, `timed_out`, and `pending`.

## Behavior

### Park

| When the reservation is made | What happens |
|------------------------------|--------------|
| During a run that is not the job's last | The run finishes. If it succeeds, the job ends `parked`, with no cooldown after that run. If it fails or times out, the job ends `failed`, as today |
| During a cooldown between two runs | The cooldown ends at once and the job ends `parked` (owner decision). Nothing is lost: the last run has already succeeded, and the next has not started |
| During the job's last run | Accepted (owner decision). The job has no later run to park before, so it ends `succeeded` when the run succeeds, and the queue is held |
| Between the worker's claim and `JobStarted` | Kept pending and applied at `JobStarted`, as a cancel is today (`_start_guard`). The job parks after the first run it makes (run *k*+1 for a resume) |
| On a queued entry | Refused (`invalid_state`): nothing of it has started. `/queue cancel` removes it; `/queue hold` keeps it from starting |
| On a finished entry | Refused (`invalid_state`), naming its state, as a cancel is. This includes an entry that finished between the service's read and the worker's check: the queue was not held, so the call must not look accepted |
| On an entry already parking | No change to the reservation. If a release has ended the hold, the queue is held again, by this entry (owner decision). Otherwise a no-op, not an error |
| On an entry already being cancelled, or stopping with the server | Refused (`invalid_state`): the cancel has already cost the run |

- A cancel always stops at once. A cancel of a parking entry ends it `cancelled`, and loses the run in progress. A
  cancel that lands after the park took effect (below) changes nothing, and the entry ends `parked`, just as a
  cancel after the last run leaves a job `succeeded`.
- Stopping the server with a park reservation pending stops the run at once, as today, and the entry ends
  `interrupted`. The reservation itself is kept in memory only and is lost; the hold it made is kept (below).
- Unpark withdraws the reservation, and the job runs on as if it had never been made (owner decision). Unpark on a
  running entry with no reservation is a no-op. Unpark on a queued or finished entry is refused, naming its state,
  as a park is.
- A park *takes effect* when the executor commits to it: after a succeeded run that is not the job's last, once it
  has ended a cooldown, or at the start of a later run it landed just before. After that, unpark is refused (`invalid_state`, "Q0007 has already parked"), even while
  the entry still reads `running` for a moment. A job that runs on after an unpark still gets its full cooldown: if
  the park had already cut one short, the cooldown waits out the rest.
- A `parked` entry can be resumed like an `interrupted`, `failed`, or `cancelled` one. The resume starts at run
  *k*+1 with the original seed, from run *k*'s own output, and reruns nothing.
- The resume goes to the back of the queue, like any resume and any submission (owner decision), so entries queued
  while the job was parked run first. The claim order stays first-in, first-out.
- History retention keeps a parked entry and its execution until an entry that resumes it has a succeeded run
  (owner decision), so a parked job stays resumable past `history_retention_days`. After that, it ages out like any
  finished entry. Until then, the chain still walks back to it: a resume that was cancelled, or whose first run
  failed, starts from the parked entry's last run. Pruning it at the resume's own start would have broken that.

### The last frame

Every video run, an image-to-video one included, ends with its last frame extracted to a PNG beside its output
(`jobs/planning.py`'s `last_frame_path`, `jobs/run_finisher.py`). The next run chains from that frame, and a resume
starts from it. Parking never skips or shortens this (owner decision):

- A park takes effect only after the run has fully finished: draw-things-cli has exited, and
  `RunFinisher.finish` has tagged the video's colors, extracted the last frame, recorded it in `record.last_frame`,
  and measured the output. `RunFinished` then names the frame. The park check sits in `_run_runs` after
  `_run_step` returns, never in the launcher or the finisher.
- A job parks only after a run whose status is `succeeded`. For a video job, that already means its last frame was
  written: a failed extraction fails the run. So a parked execution's last run always has its last frame, and the
  resume continues from it (`queue_resume.py` takes `last.last_frame or last.output`, and refuses if the file is
  gone).
- The park flag is kept apart from `CancelToken.requested`, which `RunFinisher.finish` reads through
  `stop_requested`. A park reservation therefore never turns a failed extraction into `interrupted`. The run fails
  and the job ends `failed`, as without a reservation, and the queue stays held.
- A park requested while the frame is being extracted waits for the extraction, like one requested during the run.
- A park during the cooldown between runs finds the frame already written, since it was written before the
  cooldown began. A park during the last run changes nothing about that run, and its frame is extracted as always.

### Hold

- A park reservation holds the queue from the moment it is made, and the hold is saved in the state store (owner
  decision). However the parked entry ends (parked, succeeded on its last run, failed, cancelled, or stopped with the
  server), and across `dtc serve` restarts, nothing else starts until a release. A saved hold that cannot be read
  counts as held, with a warning, so a damaged row never starts the queue by surprise. A release clears it.
- The hold remembers the entry whose reservation made it. Unpark releases the hold only when that entry's
  reservation made it (owner decision). It stays when `/queue hold` made it, or when the queue was already held.
- `/queue hold` (and `dtc queue hold`) holds the queue directly (owner decision). A running job is not stopped: the
  worker starts nothing after it ends, however it ends. While the worker is idle, or in the cooldown between queued
  jobs, the hold takes effect at once. A hold on a queue already held is a no-op, but it makes the hold its own: a
  later unpark no longer releases it.
- `/queue release` (and `/release`, `dtc queue release`) ends the hold, and the worker claims the oldest queued
  entry at once (owner decision). A between-jobs cooldown the last job earned is dropped, not waited out, however
  little of it has passed. A release on a queue that is not held is a no-op, and says so.
- A held queue still accepts submissions and resumes; they wait `queued`. Nothing about submitting changes: the
  rules, the limits, `max_queued_jobs`, and the audit log apply as before.
- After a park that holds the queue and is then released while the run is still going, the entry still parks. The
  queue is no longer held when it ends, so the worker moves on as after a succeeded job, with the usual
  between-jobs cooldown. Only a release that lands after the job has ended starts the next entry at once.

## Scope

In scope:

- The park request in `jobs/`: a `parked` job status and a new exit code, 3.
- `parked` as a queue state and as an execution status, with the history filter, restart recovery, resume, and
  `import-history` reading it. Retention keeps a parked entry and its execution until a resume of it has a
  succeeded run.
- The worker's park reservation and its persistent hold. The service functions for park, unpark, hold, and release.
- Four audited HTTP endpoints, the reservation and the hold in the API's queue responses, and in gRPC events and
  snapshots (`make proto` regenerates the stubs).
- In the TUI: `/queue park|unpark|hold|release`, the `/park`, `/unpark`, `/hold`, and `/release` aliases, Queue
  widget keys, and the `parking`, `parked`, and held displays.
- In `dtc queue`: `park`, `unpark`, `hold`, and `release`, the new states in `list`, and `add --wait`'s exit code 3.

Out of scope:

- MCP tools for park and hold. [Milestone 08](milestone-08-mcp-server.md) is planned separately; the endpoints it
  would wrap exist after this milestone.
- Parking a run in the middle. The run is still the smallest unit that can be resumed.
- A park that takes effect after more than one run ("park after run 5"), and a park reservation on a queued entry.
- Stopping the server gracefully after the current run. Stopping the server still stops the run at once; park,
  then stop the server once the entry has parked.
- Holding the queue at startup with a `serve --paused` option. The saved hold covers the need.
- Automatic resume of a parked entry on release. A resume stays explicit.

## Planned changes

### `jobs/` and `core/`

- `core/exit_codes.py`: `EXIT_PARKED = 3`, the exit code of a job that parked. It is not a failure, and it is not
  the whole job, so `dtc queue add --wait && next-step` does not go on (owner decision). No queue command exits 3
  today. The job's own exit code in the execution row can still be 3 from a failed run whose draw-things-cli
  exited 3; the status tells them apart, and `add --wait` maps the queue state, never that code.
- `jobs/events.py`: `JobStatus.PARKED = "parked"`. `RunStatus` is unchanged: runs are never parked, only jobs.
- `core/process/signals.py`'s `CancelToken` gains `park()`, `unpark() -> bool`, `take_park() -> bool`, and a
  `park_requested` property, all under its lock. `park()` sets a flag and writes the wake-up byte, so a cooldown
  between runs ends. It never asks the runner to stop. `wait()`'s predicate also ends on the park flag.
  `take_park()` is the executor's check-and-commit: True, and the park has taken effect, when the flag is set.
  `unpark()` clears the flag, and returns False only once the park has taken effect. `begin()` clears both.
- `JobExecutor` gains `park() -> bool`, which returns False when no job is running, and `unpark() -> bool`, which
  returns False only once the park has taken effect. Both are safe from any thread. `_run_runs` calls
  `take_park()` at every run boundary after a run this execution made:
  - after a succeeded run that is not the last, before its cooldown;
  - after a cooldown that a park ended early;
  - at the top of each later run.

  A taken park ends the job with `manifest.status = JobStatus.PARKED` and exit code `EXIT_PARKED`, and logs
  `Job parked after run k/N`, where k is the chain's run number. `JobFinished` carries `status="parked"` and
  `signal=None`. `_cool_down` reports a cooldown a park cut short as `cut_short=True`, like a stop. When the park
  was withdrawn before `take_park()`, it waits out the rest instead.
- `RunLauncher` and `RunFinisher` do not change, and never read the park flag. The last frame is extracted before
  any park check ([The last frame](#the-last-frame)).
- It is not done as an observer that calls `cancel()` on `RunFinished`. That would work, since the loop already
  stops "before run k+1", but the job would end `interrupted`, with SIGTERM's exit code 143. That is the word the
  owner asked not to use.

### `state/`

- `state/queue.py`: `QueueState.PARKED`, added to `FINISHED_STATES`. The `state` and `executions.status` columns
  are plain `TEXT` with no `CHECK`, so no schema migration is needed.
- The hold is stored under a `queue_hold` key in the existing `settings` table (`SettingsRepository`), as JSON:
  `{"since": "<local timestamp>", "by": "Q0007"}`, or `"by": null` for a `/queue hold`. It is removed on release,
  through a new `SettingsRepository.delete(key)`. No new table and no migration. `state/schema.py`'s comment on the
  table names the hold beside the TUI's settings.
- Retention: `QueueRepository.prune` skips a `parked` entry unless some entry whose `resumes` is its number has a
  succeeded run in its linked execution (the count `SUCCEEDED_COUNT` already makes). `ExecutionRepository.prune` skips the execution such an
  entry links. `Store.prune` prunes executions first, so both read the same queue.
- `state/history_import.py` already copies a manifest's status as text; a test covers a `parked` manifest.

### `services/`

- `services/queue_hold.py` (new): `QueueHold`, which keeps the hold in memory and in the `settings` row
  (`is_held`, `hold(by)`, `release()`, `release_if_by(label)`, `snapshot()`). It is loaded once when the worker
  starts. It is its own class so the hold's memory, its row, and the reading of a damaged row stay together, apart
  from the worker's claim and cancel bookkeeping.
- `services/queue_worker.py`:
  - `park_running(entry_id, label) -> bool`, under `_state_lock`, as `cancel_running` does. It refuses when the
    entry is not the current one, or when a stop reason is already set. It sets `_park_pending`, holds the queue
    (`by=label`) unless it is already held, and calls `executor.park()`. On an entry already parking, it only holds
    the queue again when a release has ended the hold. A reservation made before `JobStarted` stays pending and is
    applied at `JobStarted` by the same guard as a cancel.
  - `unpark_running(entry_id, label) -> bool`: refuses when the entry is not the current one, or when
    `executor.unpark()` says the park has taken effect. Otherwise it clears `_park_pending`, and releases the hold
    if this entry's reservation made it.
  - Unlike a cancel, a reservation can be withdrawn. So the guard reads `_park_pending` and calls
    `executor.park()` under `_state_lock`, and unpark does its work under the same lock. An unpark can then never
    land between the guard's read and its call.
  - `park_requested(entry_id) -> bool`, for the API.
  - `hold()` and `release()`, which wake the worker.
  - `_claim_and_run_one` claims nothing while the queue is held (checked under `_state_lock`, so a hold and a claim
    never interleave).
  - `_wait_after` waits no cooldown while the queue is held. The between-jobs wait (`_poll_wait`) also ends when a
    hold lands. Nothing of that cooldown is remembered, so a release claims at once. When the queue is not held,
    `_wait_after` applies after a `parked` job as after a `succeeded` one, since its last run succeeded.
  - `_FINAL_QUEUE_STATE` maps `JobStatus.PARKED` to `QueueState.PARKED`.
  - `state()` gains `held`: not running a job, and held. A job running under a hold still reads `running`.
  - Every change of a reservation or the hold publishes an event (below).
- `services/queue_park.py` (new), modeled on `queue_cancel.py`: `park_entry`, `unpark_entry`, `hold_queue`, and
  `release_queue`, with `ParkRefusedError(InputError)` and code `invalid_state`, whose messages name the reason
  from the table above. When `park_running` finds the entry no longer current, `park_entry` reads it again and
  refuses, naming the state it reached.
- `services/queue_resume.py`: `RESUMABLE_STATES` gains `PARKED`. A resume never releases a hold.
- `services/queue_recovery.py`: `_EXECUTION_TO_QUEUE_STATE` gains `"parked"`. That covers a crash after the job
  parked but before its entry was marked.
- `services/queue_events.py`: new kinds `queue_park_changed` (`queue_id`, `park_requested`), published on a
  reservation and a withdrawal; `queue_held` (`since`, `by`); and `queue_released`. `queue_entry_changed` is left
  alone: it means a change of state, and the TUI's feed reads its `running` as the claim that comes before
  `JobStarted` (`pending_queue_id`).
- `services/history.py`: `STATUSES` gains `JobStatus.PARKED`, so `/filter status parked` works.
- `services/queue_reader.py` (the TUI's read-only fallback): `QueueSnapshot` gains the hold, read from `settings`,
  so the TUI shows it while the server is down.

### `server/`

- `POST /v1/queue/{queue_id}/park` and `POST /v1/queue/{queue_id}/unpark` return the entry as
  `GET /v1/queue/{queue_id}` does. The current run and the cooldown let a front end word the outcome ("parks after
  run 3/7", "on its last run"). `POST /v1/queue/hold` and `POST /v1/queue/release` return
  `{"held", "held_since", "held_by"}`.
  - All four are audited (actions `park`, `unpark`, `hold`, `release`; no target for the last two) and take
    `X-Dtc-Caller` like cancel.
  - `ParkRefusedError` maps to 409 through the existing `invalid_state` row.
  - `/v1/queue/hold` cannot clash with an entry's path: only `GET` takes `/v1/queue/{queue_id}`.
- `serializers.queue_entry` gains `park_requested`, passed by the routes from `worker.park_requested`, since the
  reservation is not in the row.
- `GET /v1/queue` and `GET /v1/queue/{queue_id}` gain `held`, `held_since`, and `held_by`, as the entry detail
  already carries the worker's `cooldown_until`. `worker_state` can read `held`. No client in `src/` reads
  `worker_state` today, so the new value breaks no mapping.
- `server/proto/monitor.proto`: `QueueEntrySnapshot` gains `optional bool park_requested = 11` and
  `optional bool queue_held = 12`. `WatchQueueEntry` sends a snapshot when either changes, and its comment, and
  `Event.kind`'s list of kinds, name them. `WatchEvents` carries the new event kinds with no message change, since
  events are JSON. `make proto` regenerates the stubs in `server/`, `cli/`, and `tui/`.
- `dtc serve` logs `Queue held since <time> (by Q0007); 'dtc queue release' starts it` at startup when a hold was
  saved.

### `cli/`

- `dtc queue park <Queue ID>`, `dtc queue unpark <Queue ID>`, `dtc queue hold`, and `dtc queue release`, each
  printing the entry or the hold, and mapping refusals to exit codes as the other queue commands do.
- `dtc queue list` shows `parking` for a running entry with a reservation, and a `Queue held since ... (by ...)`
  line. Its `--state` help, and `dtc queue resume`'s, name `parked`.
- `dtc queue show` adds `park reservation: yes` for a parking entry, and the hold.
- `dtc queue add --wait`:
  - `EXIT_CODES_BY_QUEUE_STATE["parked"] = EXIT_PARKED`.
  - For a parked entry, it prints `run 3/7 succeeded`, then `Q0007 parked after run 3/7; 'dtc queue resume Q0007'
    continues at run 4`, in place of `_print_outcome`'s `run 3/7 parked`. The run is the last one it saw start,
    numbered in the chain.
  - It prints a line when `park_requested` changes (`Q0007 parking: it ends after run 3/7`, `Q0007 runs on`), and
    once when it sees `queue_held` while its entry is `queued`.
  - Ctrl-C still cancels the entry, as today.

### `tui/`

- `tui/commands.py`:
  - `QUEUE_WORDS` gains `park`, `unpark`, `hold`, and `release`.
  - `COMMANDS` gains `("queue", "park <Queue ID>", ...)`, `("queue", "unpark <Queue ID>", ...)`,
    `("queue", "hold", ...)`, and `("queue", "release", ...)`.
  - Four top-level aliases (owner decision):
    - `("park", "", "Park the entry the draw-things-cli pane is following")`
    - `("unpark", "", "Withdraw the park reservation of the entry the draw-things-cli pane is following")`
    - `("hold", "", "The same as /queue hold")`
    - `("release", "", "The same as /queue release")`
  - `/queue resume`'s help names parked entries, and `/queue cancel`'s points to `/queue park` for keeping the run.
  - `completions` completes a queue ID after `/queue park` and `/queue unpark`, as after `/queue cancel`.
  - `KEYS` gains `p` (park the selected entry) and `u` (withdraw its reservation), for the Queue widget.
- `tui/controller.py`: `command_queue` gains the four actions. `command_park` and `command_unpark` mirror
  `command_stop`: they say "No job is running", "Already stopping", "Already parking", or "Not parking".
  `command_hold` and `command_release` call the same app methods as `/queue hold` and `/queue release`.
- `tui/queue_client.py` and `tui/app.py`: `park`, `unpark`, `hold`, and `release` calls, each saying the outcome in
  Messages. The words come from the response, which may already read `parked` when a park ended a cooldown:
  - `Q0007 parks after run 3/7; the queue is held ('/queue release' starts it again)`, or on the last run,
    `Q0007 is on its last run and will finish; the queue is held`
  - `Q0007 runs on; the queue is released` (or `...; the queue stays held`)
  - `Queue held` and `Queue released`
  - A submission or a resume made while the queue is held adds `; the queue is held ('/queue release' starts it)`.
- `list_queue` returns the hold fields beside the entries. `refresh_queue` already runs after every `queue_*`
  event, so it picks up the new kinds with no handler of their own. The app keeps the hold from it, and from the
  Queue pane's store fallback while the server is down.
- Queue widget (`tui/panes/queue.py`):
  - `p` parks, and `u` unparks, the selected row; neither asks for confirmation, as `c` does not.
  - The State cell reads `parking` for a running entry with a reservation.
  - The border title reads `Queue (held)` while held.
  - `parked` gets its own style.
- `LiveRun` and the Status widget:
  - `LiveRun.phase` gains `parking`, from a new `park_requested` flag; a `stopping` still wins. `PHASE_TEXT` names
    it, and the run line shows `parking` where it shows `stopping`.
  - `tui/feed.py` applies `queue_park_changed` to the flag of the followed entry, so a reservation made in another
    TUI or in `dtc queue` shows too. The flag is set at once from this TUI's own park and unpark responses, and
    seeded from `GET /v1/queue/{queue_id}` when the feed reseeds.
  - The Status widget reads `parking after run 3/7`. The Job bar's tail reads `parking` in place of its end
    estimate, as it reads `stopping` (owner decision). The Run bar keeps its estimate, since the job ends with that
    run.
  - It renders at once at every verbose level, as `/stop` does since Milestone 04.
- The hold is shown in two places (owner decision): the Queue widget's title, and the Status widget.
  - While no job runs, the Status widget reads `Queue held since 12:04 (by Q0007)`, or `Queue held since 12:04`
    for a `/queue hold`, with the date when the hold began on another day.
  - With nothing run this session, that is the widget's first line. Under a finished job, it is the third line,
    which that layout leaves empty.
  - `tui/text/status.py`'s `status_lines` takes the hold as an argument, as it takes the other process's state.
- When a followed job ends parked, Messages says `Q0007 parked after run 3/7 ('/queue resume Q0007' continues at run
  4)`. The run number is the chain's, from the last succeeded run, not `JobFinished.completed_runs`, which counts
  only this execution's runs.
- `tui/text/common.py`: `STATUS_STYLE` gains `parked`. `/describe queue` shows `Park reservation: yes` and the hold.

### Tests

- `tests/core/test_cancel_token.py`: park, unpark, and `take_park()` from another thread, and the wake-up byte.
- `tests/jobs/test_executor.py`: with a fake runner and a fake cooldown, the park cases above, the last-frame
  cases below, an unpark that races the commit, and a withdrawn park's cooldown waiting out its remainder.
- The worker, services, routes, and `dtc queue` tests follow the existing cancel tests. `tests/tui/fake_server.py`
  gains the endpoints, the hold fields, and the new event kinds. The gRPC fake in
  `tests/cli/test_queue_wait_grpc.py` gains `park_requested` and `queue_held`.

### Documentation, when it lands

- `docs/user-guide.md`:
  - park, unpark, hold, and release in the TUI (commands, keys, the Status widget) and in `dtc queue`;
  - the four endpoints in the endpoint table, and in the audit log's list of actions;
  - `parked` and exit code 3 in "Stopping, failures, and exit codes";
  - retention keeping parked entries.
- `docs/architecture.md`: a Milestone 5 section with the queue states, the executor's park request and its commit
  point, the worker's hold, and the new events.

## Acceptance criteria

- `/queue park Q0007` during run 3 of 7: run 3 finishes and succeeds, no cooldown follows, and both the entry and
  its execution read `parked` with 3 succeeded runs. `/queue resume Q0007` starts a new entry at run 4 with the
  original seed and run 3's output, and reruns nothing.
- An image-to-video job parked during run 3 has run 3's video and its last-frame PNG on disk. Both
  `RunFinished` and the manifest name the frame, and the resumed entry's run 4 takes that frame as its input.
  Executor tests with a fake frame extractor also cover these cases:
  - A park requested from inside the extraction: the frame is still written, and the job parks.
  - A failed extraction with a park reservation: the run and the job end `failed`, not `interrupted` or `parked`.
  - A park during the last run: that run's frame is still extracted.
- A park during the cooldown after run 3 ends the cooldown at once, and the job ends `parked` with 3 runs.
- A park during the last run ends the job `succeeded`, and the queue is held.
- A parking run that fails ends `failed`, and the queue is held. A cancel of a parking entry ends it `cancelled` at
  once, and the queue is held. A cancel that lands after the park took effect leaves the entry `parked`.
- `/queue unpark Q0007` before run 3 ends: the job runs on through run 7, with its cooldowns in full, and the hold
  its reservation made is released. A hold made by `/queue hold` survives the unpark. An unpark after the park took
  effect is refused, and the job parks.
- `/queue park Q0007` again, after a release while it is still parking, holds the queue again.
- A parked entry and its execution survive retention until a resume of it has a succeeded run, and can be resumed
  after `history_retention_days`. A resume started after that time whose first run fails can itself be resumed.
  After a resume's succeeded run, they are pruned as usual. A damaged saved hold counts as held.
- While held, nothing is claimed. Submissions and resumes are accepted and wait `queued`. A resume of a parked entry
  goes to the back of the queue. `/queue release` (or `/release`) starts the oldest entry at once, even when the
  last job's cooldown has not elapsed.
- The hold survives a `dtc serve` restart. A reservation does not, and the run it was on ends `interrupted` when
  the server stops.
- Parking a queued, finished, or cancelling entry is refused with `invalid_state`, and changes nothing. So is a
  park that loses the race with the job's own end: the queue is not held.
- The Status widget and the Queue widget show `parking` whether the reservation came from this TUI, another TUI, or
  `dtc queue park`, and after the feed reseeds. The Job bar reads `parking`. The Queue widget's title shows the
  hold, including from the store while the server is down, and while no job runs the Status widget reads
  `Queue held since ...`.
- `/queue park` and `/queue unpark` complete a queue ID, and `/help` lists `p` and `u`.
- `/park`, `/unpark`, `/hold`, and `/release` do the same as their `/queue` forms. `/park` and `/unpark` act on the
  entry the draw-things-cli pane is following.
- `dtc queue add --wait` on an entry that parks exits 3, printing the chain's run number (run 5, not 2, for a
  resume from run 4 that parks after run 5). `dtc queue park|unpark|hold|release` work and are audited.
- `/filter status parked` shows parked executions. `import-history` rebuilds a parked manifest as `parked`.
- No credential appears in any new event, response, or audit row.
- `make check` passes.

## Decisions this milestone supersedes

Recorded in the [changelog](phase-3-changelog.md) on 2026-09-29:

- The [M01] owner decision "Cancelling a running entry stops it at once, as planned. An after-run cancel beside it
  (recommended), and after-run as the default, were offered." A cancel still stops at once; park is the after-run
  action beside it.
- The [M01] owner decision "No pause: stopping the server stops the queue." The queue can now be held. Stopping the
  server still stops the queue.
- The [M01] owner decision "Finished entries stay pruned with the history, so an entry left past
  `history_retention_days` can no longer be resumed", for parked entries only. They are kept until a resume of them
  has a succeeded run. Other finished entries are pruned as before.
- The Phase 3 non-goals "letting a run finish before a stop or a cancel takes effect" and "pausing the queue".

## As built

Where the build differs from the plan above, recorded as design decisions in the [changelog](phase-3-changelog.md)
on 2026-09-29:

- Retention reads the parked entries it keeps once, before either prune, rather than having the execution prune go
  first.
- The entry detail, and the park and unpark responses, carry `between_runs_after_run`, and the hold and release
  responses carry `changed`.
- `/park` on an entry already parking parks again when a release has ended the hold.
- Retention counts the whole chain of resumes below a parked entry, not only the entries that resume it directly, and
  keeps the resumes in between with it until one of them has a succeeded run.
- `CancelToken` has `park_count` in place of `park_requested`, which nothing read. The executor waits out the rest of
  a cooldown only when a park was asked for during it.
- A park saves its hold before it makes the reservation, and an unpark whose hold cannot be released keeps the
  reservation.
- A reservation on the job's last run reads `Q0007 parking: it is on its last run and will finish` in
  `dtc queue add --wait`, and `parking on its last run` on the Status widget. Both front ends word a park from
  `services/queue_park_text.py`. An unpark says `the queue is not held`, not `the queue is released`.
