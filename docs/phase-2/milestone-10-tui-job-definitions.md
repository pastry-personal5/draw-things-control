# Milestone 10: Job Definition Widget, Job IDs, and Execution IDs

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** [Milestone 05: Command layout and execution history](milestone-05-tui-run-history.md), [Milestone 09: Get and describe commands](milestone-09-tui-get-describe.md)

## Goal

See the job definitions in `data/jobs/` at a glance, and pick one by a short
ID instead of its file name. Name every execution by a short ID as well.
Give the TUI's widgets clearer titles, and make Messages easier to read.

## Scope

In scope (owner decisions):

- A **Job Definition** widget at the top of the right column, above the
  Execution History widget. It lists the job files in the data directory,
  8 rows high, and can be sorted.
- A permanent **job ID** for each job file in `data/jobs/`: `J` followed by
  four digits (`J0001`), kept in the state store.
- A permanent **execution ID** for each execution: `E` followed by four
  digits (`E0012`), kept in the state store, and shown everywhere an
  execution is named: in the TUI, the CLI's output, manifests, and logs.
- `/apply` and `/describe job` take a job ID as well as a file name.
  `/describe execution`, `/get`, and `/reveal` take an execution ID.
- `/describe execution ID` replaces `/execution ID`.
- The History widget is renamed **Execution History**.
- Messages adds blank lines between blocks of output.
- The minimum terminal size becomes 80x34.

Out of scope:

- A job ID column, or a job ID filter, in the Execution History widget. An
  execution keeps its job file path, as it does now.
- Job IDs anywhere but the TUI. The CLI still takes paths
  (`validate-job`, `run-job`); it only shows execution IDs.
- Job IDs for a data directory other than the project's `data/jobs/`.
- Rebuilding IDs after `state/` is deleted.
- Editing, creating, or deleting job files from the TUI (Phase 3,
  Milestone 03, covers job file management).
- Keeping `/execution` as an alias.

## Decisions

From the interviews (owner decisions), with the design decisions marked.

### Typing and showing an ID

- The letter is case-ignored and the leading zeros are optional:
  `J0001`, `j0001`, `j001`, and `j1` are the same job, and `E0012`,
  `e0012`, `e012`, and `e12` the same execution.
- An ID is always shown in upper case with at least four digits: `J0001`,
  `E0012`. After `9999` the numbering continues as `J10000` and `E10000`.
- An execution ID is typed in the `E` form only. A bare number (`12`) is
  refused with `use E0012`, so an execution ID and a run number
  (`/get param E0012 2`) never mix up. Run numbers stay bare.
- Both kinds live only in the state store, `state/state.db`. The user guide
  says so, and that deleting `state/` starts both numberings over; nothing
  rebuilds them.
- *Design decision:* the store's own row number for an execution stays
  internal. The history's reads and the click that reveals a file keep
  using it, but it is never shown or typed.
- Tab completes execution IDs after `/describe execution`,
  `/get prompts|positive|negative|param`, and `/reveal`, from the executions
  the Execution History widget has loaded, newest first.

### Job IDs

- Job IDs are given only to the files in the project's `data/jobs/`. With
  `dtc tui --data-dir` pointing elsewhere, the widget lists that
  directory's files with `-` for the ID, and `/apply` and `/describe job`
  take file names only there.
- An ID belongs to a **file name** in `data/jobs/`, for good. Editing the
  file keeps its ID. A renamed file is a new name and gets a new ID. When a
  file is deleted, its ID is retired, and it comes back if a file with the
  same name returns. An ID is never given to another name.
- Each new file name gets the number after the highest ever given, and a
  retired number is never reused.
- A file gets its ID when the TUI first lists it. Job files are never
  written to.
- `/apply` and `/describe job` take an ID or a file name (with or without its
  suffix, as now). Tab completion after them offers IDs as well as file
  names.
- `/get jobs` and `/describe job` show the ID.

### Execution IDs

- An execution ID is a **number of its own**, not the row number.
  Migration 3 numbers the executions already in the store by start time:
  the oldest becomes `E0001`, with ties by row order.
- The number only goes up. A pruned execution's number is never given
  again, and an execution imported by `import-history` gets the next free
  number when it is imported, whatever its start time. When its manifest
  records an ID of its own (the execution was pruned, or `state/` was
  deleted), the import names both: `E0040 (its manifest says E0003)`.
  A manifest written before this milestone has no `execution_id` and
  imports as before.
- It is shown in the `E` form everywhere an execution is named:
  - in the TUI: the Execution History widget's ID column, the Execution
    widget's title, Status line 1 (`running  E0012: walk  run 2/3`), and
    every message (`Execution E0012: walk`);
  - in the CLI's output: `run-job`, `import-history`, and any other command
    that names an execution;
  - in the job manifest (an `execution_id` field) and the job log (a line
    when the job starts).
- A job that cannot get an execution ID **does not start**. When the state
  store cannot give one, `run-job` and `/apply` fail before run 1, with exit
  code 1 as for any unusable state store, and write no manifest or log. A
  dry run needs no ID.
- *Design decision:* the ID is taken after the checks that can refuse a job
  (the run lock, the tools, the input) and before the manifest and the log
  are created. A job that still fails after that, before run 1, leaves its
  number unused; numbers are never reused, so a gap is harmless.
- *Design decision:* once a job has its ID, a later state-store failure
  keeps today's behavior: the job runs on, and the record stays as far as
  it got. Stopping the job would lose the work in progress.
- *Design decision:* `jobs/` cannot reach the state store (`state` builds
  on `jobs`, not the other way). So `JobService.run` takes a
  `reserve_execution_id` callback from the front end. It calls it at the
  moment above, puts the ID in `JobStarted`, the manifest, and the log, and
  stops with the callback's error when it fails. The recorder then writes
  the execution's row under that ID. Reserving the number and writing the
  row are two steps: if the row cannot be written after the number was
  reserved, the manifest and the log still name the ID, and the job runs on
  as for any later state-store failure.

### Job Definition widget

- Title: `Job Definition`. It sits at the top of the right column, above
  Execution History, with **8 job rows** showing (11 terminal lines with its
  header and border). The Execution widget below gives up the space, and the
  minimum terminal size rises from 80x30 to 80x34, so the Execution widget
  keeps about 9 lines.
- Columns: the **ID**, the **file name without its extension**, the file's
  **changed time** (its modification time, as `09-26 14:05`, or
  `2025-09-26` for a file last changed in an earlier year), the job's
  **mode**, and its **runs**. *Design decision:* two files that differ only
  in their extension (`walk.yaml`, `walk.yml`) show their extension, so they
  stay apart.
- An invalid job file is listed with its ID, in dim red, and its mode and
  runs read `invalid`. `/describe job` shows its error, and running it is
  refused, as now.
- **Sorting** is by any column, ascending or descending. In the widget, `s`
  moves to the next column and `r` reverses the order. From the command
  line, `/sort jobs KEY [asc|desc]` does the same, where `KEY` is `id`,
  `name`, `changed`, `mode`, or `runs`. The choice is kept in the state
  store across sessions. The default is `changed`, newest first.
- *Design decision:* `/sort jobs KEY` without a direction takes the natural
  one: newest first for `changed`, ascending for the others. Ties fall back
  to the ID, and invalid files sort after valid ones by `mode` and `runs`.
  After a sort or a check, the selection stays on the same job.
- **Keys**: Enter writes `/describe job` for the selected row to Messages.
  `a` asks to run the selected job, with the same confirmation as `/apply`;
  while a job runs, it says `A job is already running`, as `/apply` does.
  Escape returns to the command line.
- **Tab order**: command line, Job Definition, Execution History, Execution.
- The directory is watched with `watchdog` (`tui/job_watch.py`), so new,
  changed, renamed, and deleted files show without `/get jobs`. It also
  watches the input images and base configurations that valid jobs read. It
  is checked every 5 seconds only while it cannot be watched or an invalid job
  is listed. *Design decision:* only a file whose changed time or
  size differs is read and validated again. `/get jobs` still writes the
  list to Messages.

### Commands and titles

- `/describe execution ID` does what `/execution ID` did. `/execution` is
  removed, and typing it says `/execution is now /describe execution; type
  /help`, as `/jobs`, `/job`, and `/history` did in Milestone 09.
- The History widget's title becomes `Execution History`.

### Blank lines in Messages

A blank line is added:

- before each command echo (`> /command`), so each command's output reads
  as one block;
- before and after a message longer than one line (a table, an execution's
  detail, prompts);
- before each `Run N/M started`, and before the job's result
  (`Job succeeded: ...`), so a job's log splits by run.

Two blank lines never follow each other, and no blank line is added at the
top of Messages or right after `/clear`.

## Changes

- `state/store.py`: migration 3.
  - `executions` gains `execution_number` (unique), numbered by start time
    for the rows already there.
  - `counters` holds the highest execution and job numbers ever given, so a
    pruned or retired number is never reused.
  - `job_definitions` holds each job number, its file name, when it was
    first seen, and whether the file is present.
  - `settings` holds the Job Definition widget's sort.
  - Methods: reserve the next execution number; assign job IDs to a
    directory listing; look up either kind of ID; read and write the sort.
    Each assignment is read and written in one transaction, so two
    processes that list the same new file at once give it one ID. The reads
    return `execution_number` with each execution.
- `jobs/job_service.py`: `run` takes `reserve_execution_id`, calls it after
  the checks and before the manifest and the log, and puts the ID in
  `JobStarted`, the manifest's `execution_id`, and the log's first line.
- `jobs/job_events.py`, `jobs/job_manifest.py`: the `execution_id` fields;
  a manifest without one still reads.
- `state/recorder.py`: records the execution under its reserved number.
- `state/history_import.py`: the next free number for each imported
  execution, reported with the manifest's own ID when it has one.
- `cli/app.py`: `run-job` passes a reserving callback, fails with exit code 1
  when it cannot, and names the ID in its output; `import-history` names
  the IDs it gives.
- `tui/history.py`: `parse_execution_id` and `parse_job_id` read the typed
  forms; one function writes each kind.
- `tui/job_files.py`: `read_rows` returns each row's ID, changed time, mode,
  and runs, reusing a row whose file has not changed; `find_job` accepts an
  ID.
- `tui/panes.py`: `JobDefinitionPane`, a `DataTable` with its keys, the
  sort, and the 5-second check in a worker. The Execution History title and
  ID column.
- `tui/screens.py`: the widget in the layout and the Tab order; `/sort`,
  `/describe execution`, and the `/execution` message; execution IDs in
  every command that takes one; blank lines through `say`.
- `tui/widgets.py`: `MessageLog` tracks whether its last line is blank and
  whether a block ended, so it adds a blank line only where one is needed.
- `tui/commands.py`: the new forms, completion of job IDs, execution IDs,
  and sort keys, and usage.
- `tui/styles.tcss` and the size check: the widget's height and border, and
  the minimum size of 80x34.
- `tui/text.py`: the IDs in every message, `/get jobs`, and `/describe job`.
- Help, the user guide, and the architecture updated.

Browsing writes to the state store only to assign job IDs and keep the
sort. This supersedes, for those two tables only, Milestone 05's "browsing
writes nothing" (Milestone 08 already made schema upgrades an exception).

## Acceptance criteria

- Each job file in `data/jobs/` has an ID that stays the same across
  sessions, edits, and a delete and restore under the same name. A new name
  gets the next number, and no job ID is ever reused. Another `--data-dir`
  lists its files without IDs.
- `/apply J0001`, `/apply j1`, `/describe job J0001`, and the file name forms all
  find the same job. An unknown ID says so, and runs nothing.
- The Job Definition widget shows 8 rows, sorts by each column both ways
  with `s`, `r`, and `/sort jobs`, keeps the sort after a restart, and
  shows a file added, changed, or deleted within about 5 seconds, reading
  only the files that changed.
- Enter on a row describes the job. `a` asks to run it, and only `y` runs;
  while a job runs, `a` runs nothing.
- Every execution has an `E` ID that stays the same across sessions and is
  never given to another execution, after pruning and imports too. The
  migration numbers the existing ones by start time.
- `E0012`, `e012`, and `e12` find the same execution; `12` is refused and
  names the `E` form. The `E` form shows in the TUI, the CLI's output, the
  manifest, and the log, and Tab completes it. An import of a manifest that
  names another ID reports both.
- A job that cannot get an execution ID does not start, writes no manifest
  or log, and exits with code 1. A dry run still works without the state
  store.
- `/describe execution ID` shows what `/execution ID` showed; `/execution`
  runs nothing and names the new form.
- Messages never shows two blank lines in a row, nor one at its top.
- The TUI's layout holds at 80x34, its documented minimum size.
- `make check` passes.
