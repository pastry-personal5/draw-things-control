# Milestone 06: Delete executions

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
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
- **An execution a `queued` or `running` queue entry uses**: its own execution, or the execution it resumes
  (`resumes_execution`): "Q0007 is queued to resume from E0012".

### A resume the deletion ends

Anything else can be deleted, including the execution a `parked`, `interrupted`, `failed`, or `cancelled` entry
would resume from (owner decision). The entry can then no longer be resumed, and the dialog says so before it asks:
"Q0007 can no longer be resumed". This warning names each entry a resume would accept now (the resume check's own
rule: a resumable state, not yet resumed, a succeeded run in its chain, and runs left to do) whose resume chain
reads the execution. The chain is walked as `queue_resume._resolve_chain` walks it: the entry's own execution, then
each ancestor's, back to the one with a succeeded run. Whether the output file the resume would start from still
exists is not checked, so the warning errs toward showing.

After the deletion, a resume of the entry is refused: "Q0007's execution E0012 was pruned or deleted; it cannot be
resumed", where the message now says only "pruned". A parked entry whose execution was deleted is no longer kept by
retention (`QueueRepository.kept_parked`), since nothing can resume it; it ages out like any finished entry.

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

### When the server is down

Deleting goes through the API, like cancel and park. With the server down, `d` and `/delete` open no dialog, and
Messages shows the queue client's own error ("Cannot reach the server at ...; is 'dtc serve' running?"), as `c` and
`p` on the Queue widget do. `dtc history delete` exits as `dtc queue` does when it cannot reach the server.

### The command line

`dtc history delete` asks once, "Delete 12 executions? [y/N]", listing them first with any resume each ends; `--yes`
skips the question, and without a terminal it refuses unless `--yes` is given.

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

## Planned changes

### `state/`

- `ExecutionRepository.delete(numbers, *, in_use)`: one `BEGIN IMMEDIATE` transaction that reads each execution,
  skips a missing, `running`, or in-use one, and deletes the rest; returns the deleted numbers with each one's
  `log_path` and `manifest_path`, and the skipped ones with their reasons. No schema change.
- `QueueRepository.resume_links()`: the queue rows the refusal and the warning read (state, `execution_number`,
  `resumes`, `resumes_execution`), in one query.
- `QueueRepository.kept_parked()`: leaves out a parked entry whose own execution no longer exists.
- `Store.delete_executions(numbers)`: calls both, then deletes each deleted execution's log (`_delete_log`) and
  manifest (a new `_delete_manifest`, `.json` only, never a symbolic link) after the transaction commits. It returns
  the manifests that stayed: `_delete_manifest` returns whether the file is gone, and a manifest it skips (a symbolic
  link, or not `.json`) counts as kept, since the importer could still find it.

### `services/`

- `services/history_delete.py`: `executions_in_use(store) -> dict[int, str]`, each execution a queued or running
  entry uses, with that entry; `resumes_ended(store, numbers) -> dict[int, list[str]]`, the entries each deletion
  would leave unresumable; and `delete_executions(store, numbers) -> DeleteReport` (deleted, refused with reasons,
  missing, the resumes ended, and `manifests_kept`).
- "A resume would accept this entry, and its chain reads these executions" has one definition, shared with
  `queue_resume`: the state and already-resumed checks, and `_resolve_chain`'s walk up to its succeeded-run and
  runs-left checks, move into a helper both call. `_linked_execution`'s refusal says "was pruned or deleted".
- The check and the delete run under two locks, taken in the order a resume takes them:
  - `ServerContext.submission_lock`, taken by the route. A resume already holds it around the whole of
    `resume_entry`, from `_resolve_chain` to the insert. `_state_lock` alone would leave a gap: `_resolve_chain` runs
    before `enqueue` takes that lock, so a delete could land between the two, and the resume would queue with a
    `resumes_execution` that no longer exists.
  - The worker's `_state_lock`, through `QueueWorker.delete_executions(numbers)`, as `enqueue` runs a submission. It
    keeps a claim or a job start from landing between the check and the delete.
- `HistoryReader` is unchanged: the TUI still reads the history from the store, in `BROWSE` mode, and writes through
  the API.

### `server/`

| Method and path | Purpose |
|-----------------|---------|
| `POST /v1/executions/delete` | Delete one or several (owner decision: this one endpoint, no `DELETE` method): body `{"executions": ["E0012", ...]}`, 1 to `PAGE_SIZE` (200) IDs, 422 `invalid_input` otherwise or for an ID that does not parse; 200 with `deleted`, `refused` (ID and reason), `missing`, `resumes_ended`, and `manifests_kept` (ID and path) |
| `GET /v1/executions` | Gains `name`: matches the job name or file name as `/filter name` does (the store's `name_contains`) |
| `GET /v1/executions/{execution_id}` | Gains `resumes_ended`: the entries a deletion would leave unresumable, for the dialog and `dtc history delete` |

- A refused or missing ID is not an error status: the batch answers 200 with each ID's outcome, so a front end
  reports them all at once.
- Every ID gets its own audit row, `delete_execution`, with the outcome `ok`, `invalid_state`, or `not_found`.
  `audited(...)` records one row per request, so the route calls `store.audit.record` once per ID instead. Actions
  are plain strings (`state/audit.py` keeps no list of them).
- It is registered always, with no `--allow-write`: that flag guards writes to job files, and an execution is the
  server's own record.
- A front end that deletes more than 200 pages the IDs itself, one request per page.

### `cli/`

- `cli/history_app.py`: a `dtc history` group with `delete`, over the synchronous client, like `cli/queue_app.py`.
  IDs, or `--status` and `--name`, or `--all`, resolved to IDs with `GET /v1/executions` (paged); `--yes`.
- Exit codes: 0 when every execution asked for was deleted, `EXIT_INVALID_INPUT` (2) when any was refused or missing,
  after deleting the rest; the output names each, and any manifest that could not be deleted (exit 0 still).

### `tui/`

- `tui/screens.py`: `DeleteDialog(ModalScreen)`, with one execution's summary, its place (`1 of 12`), its resume
  warning, and the buttons above. The stale docstrings of `tui/screens.py` and `tui/text/jobs.py`, which name a
  confirmation that no longer exists, are fixed.
- `tui/panes/history.py`: `Space` marks a row, `d` deletes the marked rows or the selected one, a mark column, and
  `remove_rows(ids)` after a deletion, which also moves the cursor and clears the marks.
- `tui/commands.py`: `/delete execution <IDs...>`, `/delete filtered` (refused with no filter), and `/delete all`,
  completion for all three, and the `d` and `Space`
  keys in `/help`.
- `tui/controller.py`: collects the targets (the filtered set is read page by page from the store), leaves out the
  running and in-use ones and reads each one's resume warning (from the store, for the dialog; the server checks
  again), runs the dialog, and sends the deletions as the user confirms them: one `POST /v1/executions/delete` with
  one ID per **Delete**, and batches of up to 200 for **Delete all**.
- `tui/queue_client.py`: `delete_executions`.
- `tui/text/`: the dialog's summary line, and the Messages lines for deleted, refused, and left-out executions.

### Tests

- `tests/state/`: `delete` removes the row and its runs, keeps the counter, and skips running and in-use ones;
  logs and manifests go, symbolic links and non-`.json` files stay; a missing file is no error; `kept_parked` drops a
  parked entry whose execution was deleted.
- `tests/services/`: the refusal for queued and running entries; a manifest that cannot be deleted is reported and
  the row still goes; the resume warning for each resumable state, a
  resume already made, a chain with no succeeded run or no runs left (no warning), and a chain of resumes through an
  ancestor; a resume refused after its execution is deleted; a resume and a delete of the same execution racing,
  the delete landing after `_resolve_chain` and before the insert, which the submission lock keeps out. The existing
  `assertRaisesRegex(..., "was pruned")` in `tests/services/test_queue_resume.py` still matches the new text; a test
  for "was pruned or deleted" is added beside it.
- `tests/server/`: the endpoint's per-ID outcomes, the 422s (no IDs, over 200, an unparsable ID), one audit row per
  ID, `GET /v1/executions?name=`, and `resumes_ended` on the detail.
- `tests/cli/`: `dtc history delete` with IDs, filters, `--all`, `--yes`, the prompt, no terminal, a mixed or empty
  selection refused, the server down, and exit codes.
- `tests/tui/`: the dialog's four choices, marks, `/delete filtered` beyond the first page and refused with no filter,
  `/delete all`, the server down, the left-out and refused
  messages, the cursor and Execution widget after a deletion.
- `dtc import-history` after a deletion does not bring it back.

### Documentation, when it lands

- `docs/user-guide.md`: deleting executions, under "Execution history and the run lock"; the endpoint and the `name`
  filter under "Server: HTTP API and gRPC monitoring"; `dtc history delete`.
- `docs/architecture.md`: a Milestone 6 section: the delete path (TUI or CLI, the API, the submission lock and then the worker's `_state_lock`, the store),
  the refusals, and the resume warning.
- The phase changelog, and this document's status and an "As built" section.

## Acceptance criteria

- `d` on a history row, confirmed, deletes that execution: its row, runs, log, and manifest are gone, its outputs
  stay, and the history, the cursor, and the Execution widget update at once.
- Marked rows, `/delete execution` with several IDs, `/delete filtered`, and `/delete all` step through the dialog; **Delete**,
  **Skip**, **Delete all**, and **Cancel** each do what the dialog says, and Cancel keeps what was already deleted.
- A running execution, and one a queued or running entry uses, are never deleted, by the TUI, the CLI, or the API,
  and the refusal names the reason and the entry.
- Deleting the execution a parked, interrupted, failed, or cancelled entry would resume from is warned about first,
  names the entry after, and a later resume of it is refused with "was pruned or deleted"; a parked entry left so is
  pruned by retention like any finished entry.
- A queued resume never loses its resume point to a deletion racing it.
- Every deletion through the API, refused ones included, has an audit row `delete_execution`.
- `/delete filtered` with no filter, and `dtc history delete` with no selection, are refused; the whole history is
  deleted only by `/delete all` or `dtc history delete --all`.
- `dtc history delete` deletes by ID, filter (`--name` matching as `/filter name` does), or `--all`, asks unless
  `--yes`, and exits 2 naming any it could not delete.
- A manifest that could not be deleted is named, with the warning that `dtc import-history` would bring it back.
- With the server down, the TUI and `dtc history delete` delete nothing and say the server cannot be reached.
- `dtc import-history` does not bring a deleted execution back, and no execution number is given twice.
- `make check` passes.
