# Milestone 06: Delete executions

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done
**Depends on:** [Milestone 02: HTTP API: read and run](milestone-02-http-api.md) (the executions endpoints, the audit
log) and [Milestone 05: Park and hold](milestone-05-park-and-hold.md) (the parked state, and what retention keeps for
a resume).

## Goal

Let a person delete executions from the history in the TUI: one at a time, several at once, or every execution the
history's filters show. Today an execution leaves the history only when `history_retention_days` prunes it. Failed
experiments, test runs, and duplicates stay in the Execution History widget until then.

A deletion is final. The TUI asks first, in a dialog. `dtc history delete` does the same from the command line.

## Behavior

### What a deletion removes

| Removed | Kept |
|---------|------|
| The execution's row and its runs (`ON DELETE CASCADE`) | The run outputs (videos, frames, last frames) |
| Its `.log` file, as retention deletes it | The audit log entry that records the deletion |
| Its manifest, the JSON file beside the outputs | The execution number: it is never given again, since the counter only goes up |

The manifest goes too (owner decision), because otherwise `dtc import-history` would find it and bring the execution
back under a new number. Only a regular `.json` file is deleted, never a symbolic link, as `_delete_log` does for
logs. A file that is already gone is no error. A log or manifest that cannot be deleted (a permission error, a
read-only volume) is logged as a warning, and the row is deleted anyway (owner decision). The report names each
manifest that stayed, and the TUI's Messages and `dtc history delete` say so: "E0012's manifest could not be deleted:
<path>; `dtc import-history` would bring it back under a new number". The importer never reuses a number, so the
execution would come back as a new one, never as E0012.

### What cannot be deleted

A deletion is refused, naming the reason, for:

- **A running execution**: "E0012 is running; it cannot be deleted".
- **An execution a `queued` or `running` queue entry uses**: its own execution, the execution it resumes
  (`resumes_execution`), or any execution its resume chain reads between the two (a resume of a resume whose own runs
  all failed): "Q0007 is queued to resume from E0012". A queued resume already holds its resume point, so it would
  run; but if it then ended with no succeeded run, its own resume would walk back through the deleted execution and be
  refused. The chain is walked as for the warning below.

### A resume the deletion ends

Anything else can be deleted, including the execution a `parked`, `interrupted`, `failed`, or `cancelled` entry
would resume from (owner decision). The entry can then no longer be resumed, and the dialog says so before it asks:
"Q0007 can no longer be resumed". This warning names each entry a resume would accept now (the resume check's own
rule: a resumable state, not yet resumed, a succeeded run in its chain, and runs left to do) whose resume chain
reads the execution. The chain is walked as `queue_resume._resolve_chain` walks it: the entry's own execution, then
each ancestor's, back to the one with a succeeded run. Whether the output file the resume would start from still
exists is not checked, so the warning errs toward showing.

After the deletion, a resume of the entry is refused: "Q0007's execution E0012 was pruned or deleted; it cannot be
resumed", where the message now says only "pruned". The same text reaches `GET /v1/queue/{queue_id}`'s
`resume_refused_reason`, through `preview_resume`.

Retention (`QueueRepository.kept_parked`) stops keeping a parked entry, and the chain of resumes below it, once
nothing in that chain can be resumed: when a finished entry of the chain links an execution that no longer exists.
Every entry below the parked one has no succeeded run (or retention would already have let the chain go), so the
newest entry's resume walks through each of them and is refused at the missing one; the entries above it are already
resumed. Only a finished entry counts, since a running entry links its execution number before `JobStarted` creates
the row. The chain then ages out like any finished entry. Checking only the parked entry's own execution would keep a
chain forever once the execution of a resume below it was deleted.

### One, several, or the filtered history

| How | What it deletes |
|-----|-----------------|
| `d` on the Execution History widget | The marked rows, or the selected row when none is marked |
| `Space` on the Execution History widget | Marks or unmarks the selected row (a mark column at the left) |
| `/delete execution <Execution ID> [<Execution ID> ...]` | The executions named |
| `/delete filtered` | Every execution the history's current filters show, including rows not yet paged in; refused with no filter: "No filter is set; use /delete all to delete the whole history" |
| `/delete all` | The whole history |
| `dtc history delete <Execution ID> ...` | The executions named |
| `dtc history delete --status STATUS --name TEXT` | Every execution the filters match, as `/filter` matches them; at least one filter |
| `dtc history delete --all` | The whole history |

Deleting the whole history always needs `all` to be written out (owner decision), so a filter left off by mistake
cannot select it. `dtc history delete` refuses a mix of IDs, filters, and `--all`, and refuses no selection at all.

A filtered or whole-history selection is read in full, every page, before anything is deleted: paging by offset while
deleting would skip rows. An execution that starts after that read is not part of the selection.

Marks are kept by execution, not by row position, so a re-read of the history (a poll, a job's events, paging) keeps
them. A filter change clears them, since the marked rows may no longer show; so does a deletion. A marked execution
that leaves the history some other way (retention, or another TUI deleting it) drops its mark.

### The confirmation dialog

The TUI's first dialog, and its first action that cannot be undone. Every earlier action (`/apply`, cancel, park,
hold) goes without one because it can be undone.

For one execution, the dialog names it and asks:

```text
Delete E0012 (walk, succeeded, 7/7 runs, 2026-09-28 14:03)?
Its log and manifest are deleted; its outputs stay.

  [Delete]  [Cancel]
```

For several, it steps through them in history order, showing its place, with four choices (owner decision; Skip was
added to the owner's Delete, Delete all, and Cancel):

```text
Delete E0012 (walk, failed, 3/7 runs, 2026-09-28 14:03)?  1 of 12
Its log and manifest are deleted; its outputs stay.
Q0007 can no longer be resumed.

  [Delete]  [Skip]  [Delete all]  [Cancel]
```

- **Delete**: delete this one, and show the next.
- **Skip**: keep this one, and show the next.
- **Delete all**: delete this one and every remaining one, without asking again.
- **Cancel**: stop; what was already deleted stays deleted.

Keys: `y` or `Enter` deletes, `s` skips, `a` deletes all, `n` or `Escape` cancels. The resume warning shows on each
execution it applies to. Before **Delete all**, the dialog says how many of the remaining executions end a resume.
Refused executions (running, or used by a queued or running entry) are left out before the dialog opens, and Messages
names each one with its reason, so the dialog never offers one it cannot delete. When nothing is left, no dialog opens.

After the deletions, Messages says how many were deleted, names the entries that can no longer be resumed, and names
any the server refused (an execution that started, or that an entry was queued to resume from, while the dialog was
open). The history is read again at once, the cursor moves to the nearest
remaining row, the marks are cleared, and the Execution widget clears if its execution is gone.

If a request fails partway through (the server stopped, or the connection dropped), the dialog closes. Messages shows
the client's error and how many were deleted before it. Nothing is retried, and what was deleted stays deleted.

### When the server is down

Deleting goes through the API, like cancel and park. With the server down, `d` and `/delete` open no dialog, and
Messages shows the queue client's own error ("Cannot reach the server at ...; is 'dtc serve' running?"), as `c` and
`p` on the Queue widget do. `dtc history delete` exits as `dtc queue` does when it cannot reach the server.

### The command line

`dtc history delete` asks once, "Delete 12 executions? [y/N]", listing them first with any resume each ends; `--yes`
skips the question. The listing and its warnings come from a dry run of the same endpoint (`"dry_run": true`, one
request per 200 IDs), so they use the server's own refusal and warning rules; it does not need one request per
execution. What the dry run refuses is listed with its reason and left out of the question.

- Answering no deletes nothing and exits 0: "Nothing was deleted".
- Without a terminal, it refuses unless `--yes` is given, deleting nothing, with exit code 2.
- Filters that match no execution delete nothing and exit 0: "No executions match the filter". An ID that does not
  exist is reported as missing, with exit code 2 (see [`cli/`](#cli)).

## Scope

In scope:

- Deleting one or many executions through the API, audited, from the TUI and `dtc history delete`
- The refusals above, checked atomically with queue submissions and resumes, and the resume warnings
- A `name` filter on `GET /v1/executions`, matching as `/filter name` does
- The TUI's confirmation dialog, row marks, and `/delete`

Out of scope:

- Deleting run outputs, or moving anything to a trash folder ([Milestone 07](milestone-07-job-file-management.md)'s
  `.trash/` is for job files)
- Deleting queue entries, or single runs of an execution
- Undo
- Telling other clients about a deletion: no gRPC event. Another TUI drops the rows when it next reads the history
  (`/get history`, a filter, or paging), and its detail for a deleted execution already reads "That execution is no
  longer in the history"
- An MCP tool ([Milestone 08](milestone-08-mcp-server.md) may add one later), and gating deletion behind `--allow-write`
- Retention's own pruning, which still leaves manifests in place, so `dtc import-history` can bring back an execution
  retention pruned. That is not changed here; only an explicit deletion removes the manifest.

## Planned changes

### `state/`

- `ExecutionRepository.delete(numbers, *, in_use)`: one `BEGIN IMMEDIATE` transaction that reads each execution,
  skips a missing, `running`, or in-use one, and deletes the rest; returns the deleted numbers with each one's
  `log_path` and `manifest_path`, and the skipped ones with their reasons. No schema change.
- `QueueRepository.resume_links()`: the queue rows the refusal and the warning read (state, `execution_number`,
  `resumes`, `resumes_execution`, and the linked execution's succeeded-run count and whether it still exists, which
  the chain walk needs), in one query.
- `QueueRepository.kept_parked()`: leaves out a parked entry's whole chain once a finished entry of it links an
  execution that no longer exists (`_KEPT_PARKED_SQL` gains a second exclusion beside the succeeded-run one: a
  `LEFT JOIN executions` with no match, for an entry not `queued` or `running`).
- `Store.delete_executions(numbers)`: reads `resume_links()` and calls `ExecutionRepository.delete`, then deletes each deleted execution's log (`_delete_log`) and
  manifest (a new `_delete_manifest`, `.json` only, never a symbolic link) after the transaction commits. It returns
  the manifests that stayed: `_delete_manifest` returns whether the file is gone, and a manifest it skips (a symbolic
  link, or not `.json`) counts as kept, since the importer could still find it.

### `services/`

- `services/history_delete.py`: `executions_in_use(store) -> dict[int, str]`, each execution a queued or running
  entry uses (its own, its `resumes_execution`, and those its chain reads between them), with that entry;
  `resumes_ended(store, numbers) -> dict[int, list[str]]`, the entries each deletion would leave unresumable; and
  `delete_executions(store, numbers, *, dry_run=False) -> DeleteReport` (deleted, refused with reasons, missing, the
  resumes ended, and `manifests_kept`; a dry run fills in the same fields and deletes nothing).
- "The executions an entry's resume chain reads" has one definition, shared with `queue_resume`: `_resolve_chain`'s
  walk, from the entry's own execution back through its ancestors to the first one with a succeeded run, moves into a
  helper both call. The warning adds the resume checks (the state and already-resumed checks, and the runs-left check)
  on top of it; the in-use refusal does not. Both read the queue once, from `resume_links()`, not one
  `queue.list()` per entry as `_resumed_by_some_entry` does today. `_linked_execution`'s refusal says "was pruned or
  deleted".
- The check and the delete run under two locks, taken in the order a resume takes them:
  - `ServerContext.submission_lock`, taken by the route. A resume already holds it around the whole of
    `resume_entry`, from `_resolve_chain` to the insert. `_state_lock` alone would leave a gap: `_resolve_chain` runs
    before `enqueue` takes that lock, so a delete could land between the two, and the resume would queue with a
    `resumes_execution` that no longer exists.
  - The worker's `_state_lock`, through `QueueWorker.delete_executions(numbers)`, as `enqueue` runs a submission. It
    keeps a claim or a job start from landing between the check and the delete. Like `enqueue` and
    `cancel_queued`, the method lives in `QueueClaimGate`, which shares that lock, and `QueueWorker` forwards to it.
    Only the transaction runs under the lock; the log and manifest files are deleted after it is released.
  - A dry run takes both locks too, so what it reports matches what a delete at that moment would do.
- `HistoryReader` is unchanged: the TUI still reads the history from the store, in `BROWSE` mode, and writes through
  the API.

### `server/`

| Method and path | Purpose |
|-----------------|---------|
| `POST /v1/executions/delete` | Delete one or several (owner decision: this one endpoint, no `DELETE` method): body `{"executions": ["E0012", ...], "dry_run": false}`, 1 to `MAX_LIMIT` (200, `server/pagination.py`) IDs, 422 `invalid_input` otherwise or for an ID that does not parse; 200 with `deleted`, `refused` (ID and reason), `missing`, `resumes_ended`, and `manifests_kept` (ID and path). With `"dry_run": true`, the same answer for what a delete would do now, deleting nothing |
| `GET /v1/executions` | Gains `name`: matches the job name or file name as `/filter name` does (the store's `name_contains`, not its exact-match `name`) |

- A refused or missing ID is not an error status: the batch answers 200 with each ID's outcome, so a front end
  reports them all at once.
- The body is a plain dataclass, like `SubmitBody`, and the route checks the count and each ID itself, raising an
  `InputError` that names the field (`executions`). Pydantic constraints would fail before the route's own audit
  could record the refusal.
- Every ID gets its own audit row, `delete_execution`, with the outcome `ok`, `invalid_state`, or `not_found`.
  `audited(...)` records one row per request, so the route calls `store.audit.record` once per ID instead. A request
  refused as a whole (an unknown `X-Dtc-Caller`, a bad count, or an unparsable ID) gets one row, with no target and
  the error's code, and the caller recorded as `api` when the header is what failed, as the queue routes do
  (`_resolve_caller`, moved from `routes_queue.py` to `server/caller.py` so both routers share it). A body that FastAPI's
  own validation refuses (not JSON, or `executions` not a list) is recorded by `errors.py`'s `RequestValidationError`
  handler. Today `_audit_unparsed_submission` records only `POST /v1/queue`; it gains a table from an audited POST path
  to its action (`/v1/queue` to `submit`, `/v1/executions/delete` to `delete_execution`). A dry run deletes nothing and writes no audit row. Actions are plain
  strings (`state/audit.py` keeps no list of them).
- `dtc history delete` sends `X-Dtc-Caller: cli`, and the TUI sends `tui`.
- It is registered always, with no `--allow-write`: that flag guards writes to job files, and an execution is the
  server's own record.
- A front end that deletes more than 200 pages the IDs itself, one request per page.

### `cli/`

- `cli/history_app.py`: a `dtc history` group with `delete`, over the synchronous client, like `cli/queue_app.py`.
  IDs, or `--status` and `--name`, or `--all`, resolved to IDs with `GET /v1/executions`, every page read before
  anything is deleted; the listing from dry runs; then the deletions, 200 IDs per request; `--yes`.
- Exit codes: 0 when every execution asked for was deleted, `EXIT_INVALID_INPUT` (2) when any was refused or missing,
  after deleting the rest; the output names each, and any manifest that could not be deleted (exit 0 still).

### `tui/`

- `tui/screens.py`: `DeleteDialog(ModalScreen)`, with one execution's summary, its place (`1 of 12`), its resume
  warning, and the buttons above. The stale docstrings of `tui/screens.py` and `tui/text/jobs.py`, which name a
  confirmation that no longer exists, are fixed.
- `tui/panes/history.py`: `Space` marks a row, `d` deletes the marked rows or the selected one, a mark column
  (`history_cells` gains it), marks held as a set of execution numbers that `show_page` reapplies and `set_filter`
  clears, and `remove_rows(numbers)` after a deletion, which also moves the cursor and clears the marks. The table's
  row keys are row ids, not execution numbers, so it maps them through `row_id_for`.
- `tui/commands.py`: `/delete execution <IDs...>`, `/delete filtered` (refused with no filter), and `/delete all`,
  completion for all three, and the `d` and `Space`
  keys in `/help`.
- `tui/controller.py`: collects the targets (the filtered set is read page by page from the store, in full, before the
  dialog opens), leaves out the running and in-use ones (`executions_in_use`) and reads each one's resume warning (from the store, for the dialog; the server checks
  again), runs the dialog, and sends the deletions as the user confirms them: one `POST /v1/executions/delete` with
  one ID per **Delete**, and batches of up to 200 for **Delete all**.
- `tui/queue_client.py`: `delete_executions`.
- `tui/text/`: the dialog's summary line, and the Messages lines for deleted, refused, and left-out executions.

### Tests

- `tests/state/`: `delete` removes the row and its runs, keeps the counter, and skips running and in-use ones;
  logs and manifests go, symbolic links and non-`.json` files stay; a missing file is no error; `kept_parked` drops a
  parked chain once the parked entry's execution, or the execution of a finished resume below it, was deleted, and
  still keeps it while a running resume below links a number whose row does not exist yet.
- `tests/services/`: the refusal for queued and running entries, including an execution a queued resume's chain
  reads between its own and its `resumes_execution`; a dry run reports what a delete would and changes nothing; a
  manifest that cannot be deleted is reported and
  the row still goes; the resume warning for each resumable state, a
  resume already made, a chain with no succeeded run or no runs left (no warning), and a chain of resumes through an
  ancestor; a resume refused after its execution is deleted; a resume and a delete of the same execution racing,
  the delete landing after `_resolve_chain` and before the insert, which the submission lock keeps out. The existing
  `assertRaisesRegex(..., "was pruned")` in `tests/services/test_queue_resume.py` still matches the new text; a test
  for "was pruned or deleted" is added beside it.
- `tests/server/`: the endpoint's per-ID outcomes, the 422s (no IDs, over 200, an unparsable ID), one audit row per
  ID, one row for a request refused as a whole (an unknown `X-Dtc-Caller`, a bad count, a body FastAPI refuses), no row
  for a dry run or an unauthenticated request, and `GET /v1/executions?name=`.
- `tests/cli/`: `dtc history delete` with IDs, filters, `--all`, `--yes`, the prompt answered yes and no, no terminal,
  filters that match nothing, a mixed or empty selection refused, a filtered selection over more than one page read
  in full before any deletion, the server down, and exit codes.
- `tests/tui/`: the dialog's four choices, marks (kept across a re-read, cleared by a filter change), `/delete filtered`
  beyond the first page and refused with no filter, `/delete all`, the server down before the dialog and partway
  through it, the left-out and refused messages, the cursor and Execution widget after a deletion.
- `dtc import-history` after a deletion does not bring it back.

### Documentation, when it lands

- `docs/user-guide.md`: deleting executions, under "Execution history and the run lock"; the endpoint and the `name`
  filter under "Server: HTTP API and gRPC monitoring"; `dtc history delete`.
- `docs/architecture.md`: a Milestone 6 section: the delete path (TUI or CLI, the API, the submission lock and then the worker's `_state_lock`, the store),
  the refusals, and the resume warning. Also, the Phase 2 TUI module table still lists `confirm.py` ("the yes-or-no
  dialog"), which no longer exists; that row is corrected to name `DeleteDialog` in `screens.py`.
- The phase changelog, and this document's status and an "As built" section.

## Acceptance criteria

- `d` on a history row, confirmed, deletes that execution: its row, runs, log, and manifest are gone, its outputs
  stay, and the history, the cursor, and the Execution widget update at once.
- Marked rows, `/delete execution` with several IDs, `/delete filtered`, and `/delete all` step through the dialog; **Delete**,
  **Skip**, **Delete all**, and **Cancel** each do what the dialog says, and Cancel keeps what was already deleted.
- A running execution, and one a queued or running entry uses (its own, the one it resumes, or one its resume chain
  reads), are never deleted, by the TUI, the CLI, or the API, and the refusal names the reason and the entry.
- Deleting the execution a parked, interrupted, failed, or cancelled entry would resume from is warned about first,
  names the entry after, and a later resume of it is refused with "was pruned or deleted"; a parked chain left with
  nothing to resume is pruned by retention like any finished entry.
- A queued resume never loses its resume point to a deletion racing it.
- Every deletion through the API, refused ones included, has an audit row `delete_execution`: one per ID, or one for
  a request refused as a whole. A dry run has none.
- `/delete filtered` with no filter, and `dtc history delete` with no selection, are refused; the whole history is
  deleted only by `/delete all` or `dtc history delete --all`.
- `dtc history delete` deletes by ID, filter (`--name` matching as `/filter name` does), or `--all`, asks unless
  `--yes`, and exits 2 naming any it could not delete. A filtered or whole-history selection is read in full before
  anything is deleted.
- A manifest that could not be deleted is named, with the warning that `dtc import-history` would bring it back.
- With the server down, the TUI and `dtc history delete` delete nothing and say the server cannot be reached.
- `dtc import-history` does not bring a deleted execution back, and no execution number is given twice.
- `make check` passes.

## As built

Where the build differs from the plan above, recorded as design decisions in the [changelog](phase-3-changelog.md)
on 2026-09-30:

- The TUI checks its selection with a dry run of `POST /v1/executions/delete` before the dialog opens, rather than
  reading `executions_in_use` and the resume warnings from the store. The dry run already runs the server's own rules
  under its locks, and it is the request that proves the server is up and takes the token before any dialog opens.
  So `services/history_delete.py` has no public `executions_in_use` or `resumes_ended`: `delete_executions` (and its
  dry run) is the one entry point, and the in-use and warning rules are its private helpers.
- `ExecutionRepository.delete` takes `dry_run` and reports without deleting, and the log and manifest go in
  `Store.delete_execution_files`, not in a `Store.delete_executions`. The in-use rule walks resume chains in
  `services/`, which `state/` cannot import, and the files must go after the worker's lock is released.
- `DeleteDialog` is in `tui/delete_dialog.py`, not `tui/screens.py`: `tui/deletion.py` pushes it, and `screens.py`
  imports the controller that imports `deletion.py`.
- A mark is an `*` before the row's ID, not a column of its own: a column would widen the Execution History widget
  past its narrowest (36 columns, at 80x34) with no rows at all.
- Collecting the targets, the dialog loop, and the requests are `tui/deletion.py`'s `DeleteFlow`, run on the app's
  `delete_executions` worker, not `tui/controller.py`, since `d` on the pane and `/delete` both start them.
  `HistoryReader` gains `every` (every page of a filter) and `by_numbers` for it; the TUI still reads the history from
  the store.
- `dtc history delete` refuses an empty `--status` or `--name`: the server reads an empty `name` as no filter, which
  would select the whole history without `--all`.
- The chain walk is `services/resume_chain.py`'s `walk_chain`, which `queue_resume._resolve_chain` and
  `history_delete` both call.
- The in-use reasons name the execution: "Q0007 is running with E0012" for a running entry's own execution,
  "Q0007 is queued to resume from E0012" for its `resumes_execution`, and "... to resume through E0011" for an
  execution its chain reads between the two.
- `dtc queue`'s HTTP client moved to `cli/api_client.py`, which `dtc history` shares. `CliServices` gains
  `stdin_is_terminal`, so a test can answer the question.
