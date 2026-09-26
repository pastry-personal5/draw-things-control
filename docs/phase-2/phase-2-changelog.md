# Phase 2 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 2](README.md). Newest first.

## 2026-09-26

- **Change**: Pruning history also deletes the `.log` file of each pruned execution, so `history_retention_days` now bounds the logs too.
  - Only a regular file named `*.log` that a pruned row points to is deleted. Manifests and outputs stay, and a failed delete is a warning, not an error.
  - Alternatives: a separate log-only age setting, and deleting by file age in the output directory. Both were rejected: the first adds a second knob for one policy, and the second could delete logs the history still lists or files it never wrote.

- **Owner decision** [M10]: The TUI command `/run` is renamed `/apply`. It still reads the job again, confirms, and runs it, and takes a job ID or a file name. `/run` is no longer a command.

- **Change** [M10]: The Job Definition widget no longer polls.
  - It watches `data/jobs/`, and the input images and base configurations that valid jobs read, with `watchdog` (FSEvents on macOS). `tui/job_watch.py` is new, and `watchdog` is a dependency.
  - A change is read after a 0.3-second pause, since one save can report several events. The 5-second check remains only while a directory cannot be watched or an invalid job is listed, whose inputs are not known.
  - The Execution History pane still checks the run lock every 5 seconds while another process runs a job.

- **Owner decision** [M10]: From an interview after a code review of Milestone 10.
  - All ten findings are fixed.
  - The Job Definition widget reads a valid job again when its input image or its base configuration changes, not only its own file. Re-validating every file at every check, and leaving it to `/get jobs`, were offered.
  - A name that is both a file's name and another file's job ID is an error that names both ("Show errors to users"). Letting the file name win, and letting the ID win, were offered.
  - The latest sort choice wins: a choice made before the saved sort has loaded is kept, and saves are written in order. Reading the saved sort before accepting keys was offered.
- **Change** [M10]: Fixes from the same review.
  - The job catalog catches every error, as the history reader does. An uncreatable state directory is reported instead of closing the TUI and any running job.
  - An ID or run number of more digits than SQLite can hold is refused before `int()`, which raises on thousands of digits.
  - A job ID is looked up in the state store when the list has not been read yet, as right after the TUI opens. `Store.job_definition` is used for it, and `Store.job_numbers`, which nothing used, is removed.
  - `assign_job_ids` writes nothing when the listing changes nothing, so the 5-second check does not take the write lock. The widget redraws only when its rows or its sort changed, so a view scrolled with the mouse stays put.
  - `Store.import_execution` returns the execution number with the row, and the recorder reads only the number. The catalog forgets deleted files.

- **Change** [M10]: Milestone 10 is done.
  - The TUI's right column has a Job Definition widget above the Execution History widget. It shows the job files with their job IDs (`J0001`), sorts by any column with `s`, `r`, or `/sort jobs`, and keeps the sort. Enter describes a job, `a` asks to run it, and changed files show within 5 seconds.
  - Every execution has an execution ID (`E0012`), reserved before the job starts. It is shown in the TUI, `run-job`'s log line, the manifest, and `import-history`'s report. A job that cannot get one does not start.
  - `/describe execution ID` replaces `/execution ID`, and the History widget is `Execution History`.
  - Messages adds blank lines between blocks.
  - State store schema version 3 is migrated on any open.
- **Design decision** [M10]: Where the build differs from the plan.
  - There is no size notice to show below 80x34, as the plan's acceptance criteria said; the TUI has never had one. 80x34 is the documented minimum, as 80x30 was.
  - `/filter` reports `Execution History: ...`, after the widget's new title.
  - Messages trims a message's own trailing newlines and adds its blank lines itself. The blank line after the last prompt of `/get prompts` now comes from the rule "after a message of several lines", so the owner decision on the prompt layout still holds, and two blank lines never follow each other.
  - Reading the kept sort never creates `state/dtc.db`. Only giving job IDs (in `data/jobs/` only) and keeping a sort do.
  - `/get jobs` reads every file again; the 5-second check reads only the files whose time or size changed.
  - Like Execution History, the widget grows a line when it scrolls sideways, so 8 rows still show. At the minimum width, the Mode and Runs columns are reached by scrolling.

- **Owner decision** [M10]: From a second review of the plan. Tab completes execution IDs after the commands that take one, from the executions the Execution History widget has loaded; typing them without completion was offered. An import of a manifest that records an execution ID no longer in the store gets the next free number and names both (`E0040 (its manifest says E0003)`). Keeping the manifest's number, and saying nothing, were offered.
- **Design decision** [M10]: Fixes from the same review.
  - The store's row number stays internal (the reads and the reveal click use it), and is never shown or typed.
  - Reserving the execution number and writing its row are two steps. A row that cannot be written after the reservation is a later state-store failure: the job runs on.
  - Manifests gain `execution_id`; older ones import as before.
  - In the Job Definition widget:
    - `/sort jobs KEY` without a direction takes the natural one: newest first for `changed`, ascending for the others;
    - ties fall back to the ID, and invalid files sort after valid ones;
    - the selection stays on the same job after a sort or a check;
    - a file changed in an earlier year shows its date with the year.

- **Owner decision** [M10]: Job IDs and execution IDs are shown with four digits: `J0001`, `E0012`. After `9999` they continue as `J10000` and `E10000`. This supersedes the three digits (`J001`, `E012`) in the earlier [M10] entries. Typing is unchanged: the letter is case-ignored and the leading zeros are optional, so `j1` is `J0001` and `e12` is `E0012`.

- **Owner decision** [M10]: From a review of the plan.
  - Job IDs are given only to the files in the project's `data/jobs/`; another `--data-dir` lists its files without IDs. An ID for a file name in any data directory, and one for each full path, were offered.
  - Both kinds of ID last as long as `state/` does, and the user guide says so. Rebuilding execution IDs from manifests was offered.
  - The minimum terminal size rises from 80x30 to 80x34, so the Execution widget keeps about 9 lines under the new widget. Keeping 80x30, and shrinking the job list on a short terminal, were offered.
  - No extras: showing a job's ID in an execution, marking the running job's row, and sorting `/get jobs` like the widget were offered and left out.
- **Design decision** [M10]: Fixes to the plan from the same review.
  - `jobs/` cannot reach the state store, so `JobService.run` takes a `reserve_execution_id` callback from the front end. It calls it after the checks that can refuse a job and before the manifest and the log are created, and puts the ID in `JobStarted`, the manifest, and the log. The plan had the recorder write the row at `JobStarted`, which comes after the manifest and the log, so a job could not have refused to start without leaving them behind. A job that fails after taking its number, before run 1, leaves a harmless gap.
  - The Job Definition widget's 5-second check reads and validates again only a file whose changed time or size differs.
  - `a` while a job runs says so and runs nothing. `/get jobs` and `/describe job` show the job ID. The out-of-scope list now names a *job* ID column in Execution History, since that widget gains an execution ID column. It also says the CLI shows execution IDs but still takes paths.

- **Owner decision** [M10]: The `J` and `E` of an ID are case-ignored when typed. Both kinds of ID follow one rule, which also lets a job ID be typed without its leading zeros (`j1` is `J001`), as an execution ID already could. Both are always shown in upper case.

- **Owner decision** [M10]: Milestone 10 also gives every execution a permanent ID, `E` and three digits, and is renamed "Job Definition widget, job IDs, and execution IDs". From the interview:
  - The ID is a new number of its own in the state store, not the row number, which was offered. Migration 3 numbers the existing executions by start time; row order was offered. The number only goes up: a pruned one is never reused, and an imported execution gets the next free number when it is imported.
  - It is typed only in the `E` form, in any letter case (`E012`, `e12`); a bare number is refused. Accepting bare numbers too was offered.
  - It is shown in the TUI, the CLI's output, manifests, and logs.
  - A job that cannot get an execution ID does not start. Leaving the ID out of the manifest and the log was offered.
- **Design decision** [M10]: A job refuses to start only when it cannot get its ID, and with exit code 1, the code for an unusable state store; a dry run needs no ID. Once the job has its ID, a later state-store failure keeps today's behavior: the job runs on. Stopping a running job over its record was not offered, and would lose the work in progress.

- **Owner decision** [M10]: Milestone 10, "Job Definition widget and job IDs", is added to Phase 2. From the interview:
  - A job file gets a permanent ID, `J` and three digits, kept in the state store; a job file with an `id` key was offered, as was a registry file in `data/jobs/`. The ID belongs to the file name: editing keeps it, a rename gets a new one, and a deleted file's ID comes back with the same name. Following renames by content was offered. IDs are numbered in order and never reused, continuing as `J1000` after `J999`; filling gaps was offered.
  - `/run` and `/describe job` take an ID in any letter case, and Tab completes IDs. An ID column or an ID filter in the Execution History widget was offered and left out.
  - A `Job Definition` widget above the Execution History widget shows 8 job rows. Its columns are the ID, the file name without its extension, the changed time, the mode, and the runs. An invalid file is listed in dim red; hiding it was offered.
  - Sorting is by any column, both ways: `s` and `r` in the widget, or `/sort jobs KEY [asc|desc]`. The choice is kept across sessions, and the default is changed time, newest first. Enter describes the selected job, `a` asks to run it, and the list is read again every 5 seconds. Tab goes command line, Job Definition, Execution History, Execution.
  - `/describe execution ID` replaces `/execution ID`, which is removed and names its new form; keeping an alias was offered. The History widget is renamed `Execution History`.
  - Messages adds a blank line before each command echo, around each message longer than one line, and before each run and the job's result. It never adds two in a row, nor one at the top.
- **Design decision** [M10]: Two files that differ only in their extension (`walk.yaml`, `walk.yml`) show their extension in the widget, so the rows stay apart. The IDs and the sort are the only state-store writes that browsing makes, in two new tables.

- **Change** [M08, M09]: Fixes from a third code review, all made at the owner's request.
  - A `--config-json` value is written one to one: every list is in brackets and every mapping in braces, and an empty list is `[]` and an empty mapping `{}`. A mapping key that is not a plain name is quoted. This supersedes the earlier forms, where a one-item list read like its item and null, `[]`, and `{}` all read `(none)`. Because of those forms, "only what changed" could hide a change such as `[]` becoming null.
  - `JobPreview` holds each run's redacted command once, in `commands`, which is required. `command_previews` is derived from it with `shlex.join`, so `preview` no longer runs a dry-run `execute` per run.
  - `plan_header` and `plan_steps` return text without `# `, and `plan_lines` adds it for the CLI. The TUI keeps the `PlanStep`s themselves, so `PlannedCommand` is gone.
  - The last run a `/run` message is compared with is kept on the job's `LiveRun`, which each job starts afresh, instead of on the screen. A run without a command is never kept.
  - Each run's argument rows are built once and carried to the next run for the comparison. `details_text` and `plan_text` take the valid job from their caller, without unreachable `No job` branches.

- **Owner decision**: The Draw Things configurations move from `dt-config/` to `data/params/`, and the job files from `data/` to `data/jobs/`. `dt-config/` is removed. From the interview:
  - The tools read fixed paths: `data/params/` for `config_file`, and `data/jobs/` as the default `--data-dir`. There is no fallback to the old places. Global configuration keys for both directories were offered.
  - The rule never to edit, delete, or deduplicate the configurations now covers `data/params/*.json` and `data/params/*.yaml`; this move was the one-time exception. Extending it to `data/jobs/`, or dropping it, was offered.
  - Tracked files moved with `git mv`, and the rest as files. `.gitignore` ignores the `-default` files under `data/params/`.
  - The code names follow: `PARAMS_DIRECTORY` and `params_directory` replace `DT_CONFIG_DIRECTORY` and `dt_config_directory`. The current documents, scripts, and the example job's comments use the new paths; past changelog entries and done milestones stay as written.

- **Owner decision** [M08, M09]: From an interview after a second code review.
  - All ten findings are fixed.
  - A `--config-json` text value is always quoted: `"wan.ckpt"`, `""`, `"true"`. This supersedes the earlier words for text, `(empty)` and unquoted text, which could not be told from `true`, `false`, or `(none)`. Quoting only ambiguous text, or keeping JSON for nested values, was offered.
  - A width and height set by `desired_input_width` or `desired_input_height` are marked `desired input size`, not `job override`. No note was offered.
  - From run 2 on, `/run`, `/execution`, and `/describe job` show only the arguments that changed since the run before; `/get param` still shows one run in full. Keeping every table in full, or collapsing only in `/run` and `/describe job`, was offered.
- **Change** [M08, M09]: Fixes from the second review.
  - Nested values are written with braces and brackets (`size={w=2 h=3}`, `[1, 2]`), so a mapping inside a mapping is no longer flattened.
  - `/describe job` and `run-job --dry-run` read the plan from the same steps, `plan_header` and `plan_steps` in `jobs/job_report.py`. The TUI takes each run's redacted command as arguments from the new `JobPreview.commands` instead of splitting its preview line, and names the executable it was given.
  - The Status widget reads the execution ID again at every job event, so it drops the ID when the state store stops recording the job.
  - The `/describe job` test covers cooldown lines in the plan.
  - `job_file_stem` in `tui/text.py` is renamed `job_display_name` and uses `is_yaml_file`. `plan_text` takes only the job details. `execution_notes` computes an execution's notes once.

- **Owner decision** [M09]: `/execution` and `/describe job` get the same treatment as a `/run` run. They show prompts laid out as `/get prompts` lays them out, and arguments as the `/get param` table with the overrides marked. They no longer show a command line or bare JSON.
- **Design decision** [M09]: How the two commands use the table.
  - `/describe job` builds its plan from the same preview as `run-job --dry-run`. The header drops the `# `, and each run's command becomes its heading and a table, parsed from the redacted command preview. A new header line, `Executable:`, names the executable, since the table leaves it out. The prompts appear once, under the prompt pairs, not again for each run. `run-job --dry-run` in the CLI is unchanged.
  - `/execution` shows each run's prompts after its steps, and its table last, with the overrides taken from the stored `config_override`.

- **Owner decision** [M09]: When a run of `/run` starts, Messages shows the run's positive and negative prompts and its argument table with the job's overrides marked, instead of the `draw-things-cli` command line with its bare `--config-json`. No JSON is shown.
- **Design decision** [M09]: The run's table is the `/get param` table, from one function, `arguments_table`, so the two cannot differ. A `--config-json` value is written in words in both:
  - an empty text as `(empty)`, and null or an empty list or mapping as `(none)`;
  - a mapping as `key=value` pairs, and a list's items separated by commas, or by `; ` between mappings, such as LoRAs.

  The earlier `""` and `[]` of `/get param` are superseded. The full table is shown for every run; showing only what changed since the previous run was left for later.
- **Change**: `dt-config/image-to-video-wan-2-2.example.json` is written again from `dt-config/image-to-video-wan-2-2.example.yaml`, at the owner's request, so the test that compares the two passes. `dt-config/image-to-video-wan-2-2-default.json` was already gone.

- **Owner decision** [M08]: Status line 1 shows the execution's ID and the job file's name without `.yaml` (or `.yml`), for example `running  execution 12: walk  run 2/3`. This follows the `Execution 12: walk` form of the history and `/get`. The ID appears once the state store has recorded the job, from `JobStarted` on, and stays after the job ends. It is left out while the job starts, and when it is not recorded (`did not start`).

- **Owner decision** [M08, M09]: From an interview after the review fixes.
  - At the first step report, a past run whose step count differs from the counter's total gives its whole time less the time elapsed. Rescaling by a saved `strength`, or showing `estimating` until the live rate, was offered.
  - The manifest importer reads a size or frame count written as text of digits (`"832"`), by the one rule every source now shares. Rejecting text in manifests only was offered.
  - The prompt layout has no indent and a blank line after every prompt, the last one included. Dropping the last blank line, or keeping the two-space indent, was offered.

- **Owner decision** [M09]: `/get prompts`, `/get positive`, and `/get negative` put a blank line before each `positive:` or `negative:` label, start the prompt on the line after it, and put a blank line after the prompt, so a prompt reads and selects as a block. They also copy the prompts to the operating system's clipboard. `/get positive` and `/get negative` copy only the prompt text, and `/get prompts` copies it with the `positive:` and `negative:` labels.
- **Design decision** [M09]: The clipboard copy uses macOS `pbcopy` with UTF-8 input, which the TUI can confirm. Without `pbcopy`, it falls back to Textual's `copy_to_clipboard` (OSC 52) and says it asked the terminal, since not every terminal supports that and the TUI cannot confirm it. Several prompts (one per pair) are separated by a blank line. A missing prompt, shown as `(none)`, is not copied.

- **Change** [M08, M09]: Fixes from a code review of Milestones 08 and 09.
  - `/get param` compares a flag with its `--config-json` value as numbers, so `--cfg 5.0` no longer shows as replacing `guidanceScale` `5`.
  - At the first step report, the past run's time per step is used only when the past run's step count matches this run's counter total. An image-to-image run, a refiner, or a video's segments can count other than `--steps`, which made the estimate about half the real time; otherwise the past run's time less the time elapsed stands, as before the first report.
  - A run that has taken longer than its past-run time per step, or from run 2 on, longer than the job's average full run, shows `estimating` instead of `in 0 s`, as the [M08] design decision says.
  - A saved command with a number too large for a float no longer breaks the TUI; `command_settings` never raises, as documented.
  - The Execution widget looks for output files and reads run commands once, when it reads the execution off the UI thread, instead of on every key and resize.
  - The preflight and the measuring find `ffprobe` the same way (beside the `ffmpeg` on `PATH`, or else on `PATH`), so a job the preflight lets start can measure its outputs.
- **Design decision** [M08, M09]: Code shared after the same review.
  - `DrawThingsGenerateArguments.command` is written from one ordered flag table, `GENERATE_FLAGS`. The parser's `VALUE_FLAGS` and the redacted `SECRET_FLAGS` come from it, so the builder, the parser, and redaction cannot drift apart.
  - A new `core/numbers.py` reads numbers from outside for every source: a command, `--config-json`, a manifest, or `ffprobe`. A size or frame count is a positive whole number, from JSON or text of digits, wherever it comes from. The manifest importer now reads `"832"` as 832; it used to drop text.
  - `/reveal`, a click, and Enter reveal an output through one helper, `reveal_run` in `tui/history.py`.

- **Owner decision** [M09]: Milestone 09, "Get and describe commands", is added to Phase 2. `/jobs` becomes `/get jobs`, `/job JOB` becomes `/describe job JOB`, and `/history` becomes `/get history`; the old names are removed, and typing one names its new form (keeping them as aliases, or moving every command to verb-noun form, was offered). New: `/get prompts`, `/get positive`, and `/get negative ID [RUN]` show an execution's prompts, and `/get param` (or `/get parameters`) `ID [RUN]` shows a run's `draw-things-cli` arguments without the prompts, as a text table. From the interview: `ID` is the History execution ID (a job file name, or both, was offered); without `RUN`, each prompt pair is shown once with its runs (per run, or every pair only, was offered); the table lists the saved command's arguments with the overridden values (the job's settings instead, or both, was offered).
- **Design decision** [M09]: The parameters table has two parts, the flags and then each `--config-json` key, since a flag can replace a configuration value and both are in the command. Its note column marks the job's overrides and every replaced value on both rows. Base-configuration values that a job override replaced inside `--config-json` cannot be shown, because the command holds only the merged result; the note says `job override` there. The table is plain aligned text in Messages, so it wraps and scrolls with the rest of the log.
- **Change** [M09]: Milestone 09 is done.

- **Owner decision** [M08]: A design change after the milestone was built, which supersedes the [M08] decision that the run estimate comes only from the live step counter. A run is estimated as early as possible: before `draw-things-cli` reports a step, from a past run's time less the time elapsed; at the first report, from the past run's time per step times the steps left; and from the second report on, from the live rate. The past run is the job's own last successful run, or before it has one, the latest successful run of any job in the state store. From the interview: the latest successful run of any job, whatever its settings (the latest with the same settings, or the same job file first, was offered); rescaling the past run at the first report (keeping its time until the rate, or `estimating` until the rate, was offered); and the job estimate starts from the past run on run 1 (keeping `estimating` until the rate was offered).
- **Design decision** [M08]: Before a run's live rate exists, from run 2 on, the job estimate still counts that run as the job's average full run time less its elapsed time, not as the past run's `seconds`, since `seconds` leave out the decode, last-frame extraction, and measuring that follow `draw-things-cli`. A run that has taken longer than the past run shows `estimating` rather than `in 0 s`. The job's worker reads the past run with `Store.latest_succeeded_run` in its own store connection before the job starts, and posts it ahead of every job event, so the first run has an estimate from its first moment.

- **Change** [M08]: Milestone 08 is done.
  - A Status widget above the draw-things-cli pane shows the phase, a job bar and a run bar with `ends ~16:42 (in 23 min)`, the step counter and elapsed time, and the last successful run's time. During a cooldown it shows a `Wait` bar, and after the job, its result and total time. It says so when another process holds the run lock.
  - An Execution widget under the history follows the cursor. It shows the model, refiner, measured size, CFG, and shift, and each successful run's measured frames, steps, and time, with a file-name link that reveals the output in Finder. It can be used from the keyboard. Enter on a History row now moves to it; `/execution` still writes the full detail. `/execution` also shows the refiner, CFG, shift, and each run's steps and measured output.
  - The minimum terminal size is 80x30. Each successful run's output is measured (`ffprobe` for videos, the header for PNGs), and `RunFinished`, the manifest, and the state store (schema version 2, migrated on any open) keep the result. A video job without `ffprobe` fails before run 1 with exit code 2.
- **Design decision** [M08]: At 80 columns the History table scrolls sideways, and its scrollbar took one of the 5 rows the owner asked for. The table grows by a line while that scrollbar shows. Hiding the scrollbar was the alternative; it was rejected because the start time and run count would then be unreachable on a narrow terminal.

- **Owner decision** [M08]: From a second review of the plan.
  - From now on, opening the state database may run any schema upgrade, whatever opened it, including the TUI just browsing history. This supersedes Milestone 05's "browsing writes nothing" for upgrades only; browsing still never prunes. Leaving a version 1 database to be upgraded by the next job was offered.
  - Video jobs require `ffprobe` as they require `ffmpeg`: a job without it fails before run 1 with exit code 2. Warning and recording `-` was offered.
  - During a cooldown, Status line 3 is a `Wait` bar with its percentage and end time; a text countdown was offered.
  - Enter on a History row moves to the Execution widget instead of writing the detail to Messages; `/execution ID` still writes it. Keeping Enter as it was was offered.
- **Design decision** [M08]: Fixes from the same review.
  - The job estimate times the runs still to come by each finished run's full time (`RunStarted` to `RunFinished`), which includes loading, the decode, last-frame extraction, tagging, and measuring. It still bases the waits on `seconds`, as the cooldown does. Using `seconds` for both would have made every remaining run look short.
  - From run 2 on, before a run's step rate exists, the job estimate counts the current run as that average time less its elapsed time. Otherwise the job line would drop to `estimating` at the start of every run.
  - The reveal action carries only the execution and run numbers, never stored text.
  - The plan now lists its dependency on Milestone 07.

- **Owner decision** [M08]: The width, height, and frame count in the execution detail widget are the **actual** values of the output file, recorded after each run, because Draw Things may round a requested size to a multiple of 64 or crop it, and a video model may change the frame count. The requested values from the arguments or the configuration are neither recorded nor shown. This supersedes the earlier [M08] entry that read the size, and the frames, from the saved command. From the interview: the actual frame count replaces the requested one (keeping the requested frames was offered); image outputs record their size too (videos only was offered); runs from before this milestone show `-` (measuring the file when shown, or a one-time backfill command, was offered). Model, refiner, CFG, shift, and steps are still read from the saved command.
- **Design decision** [M08]: A new `jobs/media_info.py` measures outputs.
  - Videos go through `ffprobe`, which comes with the `ffmpeg` video jobs already require. The size is the stream's displayed `width` and `height`, after any crop, not the padded coded size. The frame count is `nb_frames`, falling back to `-count_packets`, which counts without decoding.
  - PNGs are read from their `IHDR` header, with no external tool.
  - Measuring happens after a successful run, outside its `seconds`. A failure is a warning, never a failed run.
  - The values travel in new `RunFinished` fields, the manifest's `RunRecord`, and three `runs` columns (`output_width`, `output_height`, `output_frames`) added by state store schema migration 2. The `output_` prefix keeps them apart from a job's requested `width`, `height`, and `frame_count`.

- **Owner decision** [M08]: The owner has seen `draw-things-cli` show one step counter through a Wan 2.2 run with a refiner: 1/40, 2/40, 3/40, and on, without restarting when the refiner takes over. This supersedes the earlier [M08] decision to capture a real run's output before building, and the plan's stage rules. The run estimate follows one counter per run. A counter that goes backward, or whose total changes, restarts the rate measurement instead of starting a stage. Line 4 no longer shows a stage number.

- **Owner decision** [M08]: The execution detail widget also shows the width and height, compactly as `832x448`, on one line with CFG and shift. Both are read from the saved command like the other settings: `--width` and `--height`, which win over `width` and `height` in `--config-json`.

- **Owner decision** [M08]: From a review of the plan.
  - With no job in the session, the Status widget is empty: no `idle` text. This supersedes the earlier answer of `idle` only.
  - The milestone starts by capturing one real run's output into `docs/research/draw-things-cli-progress-sample.log`, and the stage rules are checked against it before any code is written. Building without a sample, or the owner pasting one, was offered.
  - While a run is finishing after its last step, the job estimate adds the previous run's tail (the time from its last step to its end); on run 1 it adds nothing. Counting 0 throughout was offered.
  - On narrow lines the bar shrinks, down to nothing, and the end text is always written in full. Dropping `(in …)` first was offered.
- **Design decision** [M08]: Fixes from the same review.
  - Durations use `duration_text` of whole seconds (`7 min 12 s`); `seconds_text` would have shown `432.1 s`.
  - History's Escape focuses the command line directly, since the next widget in the Tab order is now the Execution widget.
  - The screen's resize rule counts the Status widget's 7 lines.
  - A stop request freezes the bars and shows `stopping` in place of the end times. A job that did not start shows only `did not start`. During a cooldown, line 4 says which run is next.
  - A counter whose total changes also starts a new stage.
  - A job already running in another process is checked once when the TUI opens, rather than only at the first 5-second poll.
  - Long model names are cut in the middle, so the refiner's ` from 10%` still shows.
  - The selected run is kept when the same execution is read again.
  - A filter that hides a new execution leaves the cursor where it is.

- **Owner decision** [M08]: From a third interview on the plan.
  - "Last run took" keeps the last successful run when a run fails; showing the failed run's time was offered.
  - After a job ends, the Status widget shows the total job time with cooldowns included. No total, or the total of pure run times, was offered. The detail widget still shows no totals.
  - The history table shows 5 rows. 7 rows, from 10 lines with the border, or 10 rows was offered.
  - When this TUI starts a job, the history cursor moves to the new execution unless History or the detail widget has focus. Always moving it, or never, was offered.
  - End times read `ends ~16:42 (in 23 min)`. The clock alone, or with seconds, was offered.
  - The link shows the file name, cut with `…` to fit; the full path was offered.
  - With no job in the session, the Status widget shows only `idle`; the last execution from history was offered.
  - The detail widget can be used from the keyboard. Tab reaches it after History, Up and Down select a run, Enter reveals it, and Escape returns to the command line. This supersedes the earlier plan, which left the keyboard out and relied on `/reveal`.

- **Owner decision** [M08]: The Status widget's "last run took" line shows the pure run time, without cooldown: the `seconds` of the current job's last successfully finished run, as the execution detail widget shows it. The line appears only once there is such a run; before that it is empty instead of showing `-`.

- **Owner decision** [M08]: The execution detail widget lists only runs that finished successfully. Failed, timed-out, interrupted, and running runs are left out, so the status column goes; `/execution` still lists every run. Each run's time is its pure run time without the cooldown: the stored `seconds`, which the job service measures around the `draw-things-cli` process alone. The widget shows no total job time and no total cooldown time.

- **Owner decision** [M08]: The execution detail widget also shows the model, the refiner (with the point where it starts), CFG, and shift.
- **Owner decision** [M08]: This supersedes the earlier [M08] entry that recorded steps and frames when each run starts. Every setting in the widget is read from each run's saved command instead. The state store already keeps that command, credentials redacted, and it includes `--config-json`: the base configuration merged with the job's overrides, as passed to `draw-things-cli`. The plan was first written on the belief that the store kept neither value; all 12 runs in the owner's database hold both. The state store schema, `RunStarted`, and the manifest do not change, and executions from before this milestone show their settings too.
- **Design decision** [M08]: `command_settings`, in `core/draw_things_arguments.py`, reads the settings back from a command. It sits beside the flags it reads so the Phase 3 server can use it. Flags win over `--config-json` keys, as `draw-things-cli`'s configuration precedence says. A setting in neither shows `-`: the model's recommended value is not in the command, so it cannot be shown truthfully. Model, refiner, CFG, and shift come from the first run with a readable command, since the configuration is job-wide. Steps and frames come from each run's own command.

- **Owner decision** [M08]: Milestone 08, "Status widget and execution detail widget", is added to Phase 2 and planned. A Status widget above the draw-things-cli pane shows the status, an overall progress bar, a run progress bar, progress details, the time the last run took, and the estimated wall-clock time at which the current run and the current job finish. The lower part of the right column becomes an execution detail widget. It shows each run's frames, steps, and time taken, and a link that reveals the video in Finder when clicked.
- **Owner decision** [M08]: From an interview on the plan.
  - Layout: the minimum terminal size is raised from 80x24 to 80x30. Shrinking the draw-things-cli pane, or collapsing the Status widget on short terminals, was offered. The history table is about 10 lines, and the detail widget takes the rest (half and half, or a fixed detail height, was offered). The draw-things-cli pane keeps its run line.
  - Run estimate: from the live step counter only (falling back to history before the first step, or history only, was offered). A counter that drops starts a new stage, and a run past its last step shows `finishing`.
  - Job estimate: from this job's finished runs plus the waits of its cooldown policy (also using past executions was offered). The overall bar is the estimated share of time, cooldowns included (runs with the current run's fraction, or whole runs only, was offered).
  - "Last run took" means the previous run of the current job (the last finished run of any job was offered).
  - A job run by another process gets only a notice (showing what the store holds was offered).
  - Steps and frames are recorded when each run starts, in a schema version 2 migration (working them out later from the configuration, or recording now with a fallback for older rows, was offered).
  - Clicking the link reveals the file in Finder (opening it in the default player, or both, was offered).
  - The detail widget follows the history cursor (updating only on Enter or `/execution` was offered).

- **Change** [M07]: Milestone 07 is done. The wait between a job's runs is a `cooldown` mapping, in `config/global-config.yaml` and, replacing it as a whole, in a job file: `mode: auto` waits `ratio` (default 0.5) of the run just finished, rounded up to a whole second and kept between `minimum_seconds` (default 0) and `maximum_seconds` (default 3600); `mode: manual` waits a fixed `seconds`; `mode: off` never waits. `validate-job`, `run-job --dry-run`, the job log, the TUI confirmation and messages, and `/execution` show the mode and why each wait is that long. The manifest and the execution's `settings` keep the resolved mapping as `cooldown`; `cooldown_seconds` holds only a manual wait. `CooldownStarted` carries `mode`, `ratio`, `run_seconds`, and `bound`; `ratio` was added to the planned fields so the event alone can say "half" or "40%".
- **Change** [M07]: With no `cooldown` in either file, `auto` now applies with its defaults: every run but the last is followed by half its time, up to an hour, where before there was no wait. `cooldown: {mode: off}` restores the old behavior. The old `cooldown_seconds` key fails validation (exit code 2) in both files with a message giving the mapping for the same wait; `config/global-config.yaml` must be rewritten by hand.
- **Owner decision**: Ruff formats as well as lints, and Black is removed; this supersedes the earlier choice of Black as the formatter. The unlimited line length stays: `line-length = 65535` in `[tool.ruff]` applies to the linter and the formatter alike. `make format` runs `ruff format`, and `make check` runs `ruff format --check`.
- **Owner decision** [M07]: From a second interview on the plan. This supersedes the earlier [M07] design decision that kept the ratio fixed. The run time is the time `draw-things-cli` ran, without last-frame extraction or color tagging (the whole run step, or the time since the last wait ended, was offered). `auto` takes an optional `ratio`, default 0.5, above 0 and up to 1 (a range up to 4 or 10 was offered). A third mode, `off`, turns the wait off; manual `seconds: 0` still works (keeping two modes, or also a scalar `cooldown: off`, was offered). An auto wait is rounded up to a whole second (whole minutes, or no rounding, was offered). With no `cooldown` in the global configuration or the job, `auto` applies with its defaults, so a job that had no wait now waits half of each run, up to an hour (no wait, as today, or a required key was offered). `validate-job`, `/run`, and `--dry-run` show the rule, the bounds, the number of waits, and the most they can add up to (the rule alone, or an estimate from past executions, was offered). A wait set by the minimum or the maximum says so in the log and the TUI.
- **Design decision** [M07]: YAML reads an unquoted `off` as the boolean `false`, so `mode: false` is accepted as `off`, and no other boolean is; asking people to quote `'off'` would make the obvious spelling an error.
- **Owner decision** [M07]: Milestone 07, "Automatic cooldown", is added to Phase 2 and planned. The global configuration gets a root `cooldown` key with a `mode`: `manual` takes a fixed wait in seconds, and `auto` waits half of the last run's time (a run of A minutes is followed by A/2 minutes), with a minimum a person can set.
- **Owner decision** [M07]: From an interview on the plan. Job files take the same `cooldown` mapping and replace the global one as a whole (keeping `cooldown_seconds` in jobs, or dropping the job-level override, was offered). The old `cooldown_seconds` key is an error in both files, with a message naming the new form; reading it as `manual` was offered. The owner rewrites `config/global-config.yaml` by hand. `auto` takes an optional `maximum_seconds`, default 3600, today's cooldown limit (no default cap, or no maximum at all, was offered).
- **Design decision** [M07]: The key under `manual` is `seconds`, and under `auto` `minimum_seconds` and `maximum_seconds`, since `cooldown.cooldown_seconds` would repeat the parent's name. The run time is the `seconds` the manifest already records for a run: the time `draw-things-cli` ran, without the last-frame extraction. The ratio stays one half and is not a key yet. The state store needs no migration: the mode goes in the execution's `settings` JSON, and the `cooldown_seconds` column holds only a manual wait.
- **Change** [M06]: Fixes from the code review. YAML numbers read as JSON would read them: `1e-3` is a number, not the string `"1e-3"`, and a leading zero (`010`, octal) or a colon (`1:30`, base 60) is rejected with its line instead of being read as 8 or 90. The keys of a mapping merged inline with `<<` are checked for duplicates and non-strings like any other. A value PyYAML cannot build (`!!float abc`, `!!timestamp 2026-99-99`) is a validation error with its line, and nesting too deep to read is a validation error instead of a crash. `generate --config-file` picks YAML or JSON by the name given, as `validate-config` does, not by a symlink's target.

## 2026-09-25

- **Change** [M06]: Milestone 06 is done. A job's `config_file` must name a `.yaml` or `.yml` file in `dt-config/`; a `.json` name fails validation (exit code 2) and names the YAML file with the same stem when there is one. `generate --config-file` with a YAML file passes it as one compact `--config-json`, with any `--config-json` merged on top, and no `--config-file`; a JSON file is passed as before. `validate-config` reads both. YAML errors name the file and the line (invalid YAML, duplicate or non-string keys) or the key path (dates, binary data, sets, `.inf`, `.nan`, self-referring aliases). `dt-config/image-to-video-wan-2-2.example.yaml` and `-default.yaml` were written from their JSON files, which are unchanged; `data/example-job.yaml` and `data/duo.yaml` name them, and `.gitignore` ignores the `-default.yaml` like its JSON.
- **Design decision** [M06]: Duplicate and non-string keys are rejected while the YAML is constructed, where the line is known; values JSON cannot hold are rejected in one walk after loading, which names the key path instead. Rejecting them in the constructor would give a line but no path, and would miss `.inf` and `.nan`, which PyYAML builds as ordinary floats. Keys brought in by a merge (`<<`) may still be overridden, as YAML intends.
- **Owner decision** [M06]: The base configurations in `dt-config/` are written as YAML, because YAML is for people to edit. For `draw-things-cli`, the app creates an in-memory JSON from the YAML and uses that. Milestone 06, "YAML Draw Things configurations", is added to Phase 2 and planned.
- **Owner decision** [M06]: From an interview on the plan: a job's `config_file` must name a YAML file, and naming a `.json` file is a validation error (accepting both, or accepting JSON with a warning, was offered). The current `dt-config/*.json` files are converted once, by hand, in this milestone into YAML files next to them; a `dtc convert-config` command and leaving the conversion to the owner were offered. Jobs keep passing the merged configuration inline with `--config-json`, rather than through a generated file. `generate --config-file` with a YAML file passes it inline the same way, merged with any `--config-json`, so the app writes no JSON file at all; a temporary file deleted after the run was planned first and dropped for this. The converted YAML files keep the JSON's key order with one header comment (grouping and commenting the keys was offered). The owner's `data/duo.yaml` is pointed at its YAML configuration. No `/jobs` hint about JSON names is added; the validation error is enough.
- **Design decision** [M06]: The JSON files in `dt-config/` stay untouched and still work with `generate --config-file` and `validate-config`. YAML is read strictly: duplicate keys, non-string keys, dates, and non-finite numbers are rejected, since PyYAML would otherwise accept them silently or produce invalid JSON.
- **Owner decision** [M05]: Every TUI widget except the command line and the history pane is on a black background: the draw-things-cli pane (with its run line and output), Messages, and the status line.
- **Change** [M05]: Fixes from the code review.
  - `/execution` and `/reveal` accept only ASCII digits up to SQLite's largest integer, so `/execution 99999999999999999999` or `/execution ²` is a usage error instead of a crash.
  - The history title and subtitle are set as text, not markup, so `/filter name "[/]"` no longer closes the app.
  - Every history read goes through one `HistoryReader`, which keeps one store for the screen's life and turns any error into a message. Before, an error other than `sqlite3.Error` (a bad JSON column, say) ended the worker and with it the app.
  - Only the newest history read is shown, and a filter change clears the rows at once, so a stale page can no longer mix old and new rows.
  - The run confirmation no longer takes Enter, since Enter also submits `/run`; the stop and quit dialogs still do.
  - Polling now follows the run lock rather than a visible `running` row, so another process's job appears whether it started before or after the TUI, and whatever the filter.
  - A finished run updates only its own row, found through the new `ExecutionRecorder.execution_id`, and `Store.executions_by_id` reads such rows.
  - The output pane no longer copies its 2000-line buffer for every line.
  - Tab completes job file names that need quoting, in the quoting style already typed, and the command word in any case.
- **Change** [M05]: Milestone 05 is done. `dtc tui` is one screen with the draw-things-cli pane, Messages, the history pane, a command line between two rules, and a status line, driven by `/` commands (`/help`, `/jobs`, `/job`, `/run`, `/stop`, `/history`, `/execution`, `/filter`, `/reveal`, `/clear`, `/quit`) with Tab completion, Up/Down recall, and Escape to clear. Ctrl-C clears the line, or quits on a second press within 2 seconds. The job list, detail, live, and help screens, and the `q`, `x`, `l`, `s`, `r`, `?`, `j`, and `k` keys, are gone. The history pane reads the state store in pages of 200, keeps its rows and selection on a refresh, and polls every 5 seconds while another process runs a job. `Store.list_executions` gains `name_contains`, and `Store.succeeded_runs` is new; neither changes the schema. The user guide and architecture are updated.
- **Owner decision** [M05]: The TUI becomes one screen with five widgets: the draw-things-cli pane (15 lines, top left), Messages (command output and a readable job log, below it), the execution history (the full height at the right), a command line between two full-width white rules, and a one-line status line under it. It replaces the job list, detail, live, and help screens of M03 and M04. The history screens planned for M05 become this pane, and M05 is renamed "Command layout and execution history". A separate Milestone 06 for the layout was offered and not chosen.
- **Owner decision** [M05]: The TUI is driven by typed commands that begin with `/`, and each command's name says what it acts on: `/help`, `/jobs`, `/job`, `/run`, `/stop`, `/history`, `/execution`, `/filter status|name|off`, `/reveal`, `/clear`, and `/quit`. A line without `/` runs nothing. The single-key shortcuts `q`, `x`, `l`, `s`, `r`, and `?` (and `j`/`k` in the job list) are removed. This supersedes those keys in the [M03] and [M04] entries; the confirmation dialogs keep `y`/Enter and `n`/Escape. Three alternatives were rejected: keeping today's command names with a slash added, verb-object names (`/list-jobs`), and making the slash optional.
- **Owner decision** [M05]: Ctrl-C quits only on a second press within 2 seconds, and while a job runs the second press opens the "Stop the job and quit?" dialog. This supersedes the [M04] behavior, where one Ctrl-C quit or asked. Having the second press stop the job without a dialog was rejected. Signals sent from outside still behave as in M04.
- **Owner decision** [M05]: The history pane takes a third of the width, at least 36 columns; 40% and half were offered. The command line rules use the theme's foreground color (white in the dark theme) rather than a fixed white. Up/Down recall the commands typed in this session, kept in memory only; saving them in `state/` was rejected because it would be a new file the TUI writes. Escape clears the command line.
- **Owner decision** [M05]: The TUI uses a dark theme by default. The command line shows `> ` and a steady (not blinking) white block cursor.
- **Design decision** [M05]: From a review of the plan:
  - Ctrl-C first clears a non-empty command line and does nothing while a dialog is open, and its "press again" hint is shown in the status line, where it cannot scroll away.
  - `/filter name` matches the job name or the job file name as a substring in any letter case, since `/run` takes file names while history shows job names.
  - Clearing the filters is `/filter off`, so `/clear` means only "clear the messages".
  - Messages is capped at 5000 lines.
  - A failing `open` in `/reveal` is reported, because a failed Textual worker would close the app.
  - A history refresh keeps the loaded rows and the selection.
  - The draw-things-cli pane may shrink, to no fewer than 7 lines, so Messages keeps 6 lines at 80x24.
  - Textual's command palette is turned off.
- **Change** [M04]: Fixes from the code review. The unmount backstop now records the stop before cancelling, so an app that ends while the worker is still taking the lock or opening the store stops the job before run 1 instead of letting it run on. After the app has gone, `SIGHUP`, `SIGTERM`, and `SIGINT` stay handled (and ignored, since the job is already stopping) until the worker ends, then the previous handlers are restored; a second signal during that wait no longer kills `dtc` and leaves `draw-things-cli` running. A signal that arrives while a job is already stopping no longer changes `dtc tui`'s exit code, which stays 128+N for the signal that stopped the job. The run table updates its cells in place, so it keeps its scroll position; a line of output updates only the output pane or the progress, not the whole view. The list's status column widens to fit `running`.
- **Change** [M04]: Milestone 04 is done. `x` on the job list or detail view reads the job again, asks for confirmation, and runs it on a worker thread under `RunLock("tui")`, recorded in the state store as `run-job` records it. The live view (`l` from the list, Escape back) shows the runs, the active run's elapsed time, step progress and redacted command, the cooldown countdown, the last 2000 output lines (progress-bar lines update the progress instead), and the result. `s` stops the job as Ctrl-C does in the CLI (exit code 130). `q` and Ctrl-C during a run ask to stop it first; `SIGHUP`, `SIGTERM`, and `SIGINT` stop the job and make `dtc tui` exit with 128+N. `dtc tui` gains `--shutdown-grace`, and `create_job_service` takes `handle_signals` (`False` for the TUI).
- **Design decision** [M04]: The backstop that stops a running job when the app ends unexpectedly is in the app's `on_unmount`, not only in a `finally` after `App.run()` as planned. `App.run()` uses `asyncio.run`, which waits up to 300 seconds for the default executor's threads, the job worker among them, before it returns, so a cancel after `run()` would come too late. The `finally` in `tui_command` stays, for an app that fails before it mounts.
- **Design decision** [M04]: A stop requested after confirmation but before `JobService.run` has begun (the lock and store are still being opened) finds no job to cancel, so the worker's observer cancels on `JobStarted` when a stop is pending, and the job ends interrupted before run 1.
- **Change** [M04]: Plan reviewed against the runner and Textual 8.2.8. Every job event, `RunOutput` included, arrives on the worker thread (the runner drains its readers on the `run()` thread). Progress-bar lines update the progress display instead of filling the 2000-line pane. Textual runs thread workers on asyncio's default executor, which Python waits for at exit, so an app that ends unexpectedly would leave `dtc` running the job in the background: `dtc tui` now cancels any running job in a `finally` after the app returns. A job counts as running until its worker has released the lock (`JobWorkerEnded`), not until `JobFinished`. `dtc tui` exits with 128+N after a signal. Running a job writes what `run-job` writes; browsing still writes nothing. The plan adds a Tests section, including a subprocess test for `SIGTERM`.
- **Change** [M04]: Milestone 04 plan revised after checking it against the code as M03 left it. `create_job_service()` builds `JobService` with `handle_signals=True`, which raises off the main thread, so the TUI's service is built with `handle_signals=False` (one per app, not one per job, since `run()` resets its state and refuses concurrent calls). `Store` keeps one connection per thread, so the job worker opens, sweeps, uses, and closes its own. Textual installs no `SIGHUP`, `SIGTERM`, or `SIGINT` handler, so the earlier claim that the runner's cleanup stops the child when the terminal closes was wrong: the child runs in its own session and would survive. The app now registers those signals with the asyncio loop while it runs and stops the job before exiting. Errors before `JobStarted` are shown and record nothing. `dtc tui` gains `--shutdown-grace`.
- **Design decision** [M04]: Job events reach the UI through `App.post_message` into an app-level `LiveRun` model that screens render from. `call_from_thread` (blocks the worker on the UI and raises once the app is gone) and posting to the live screen directly (events lost while the user is on the list) were rejected.
- **Design decision** [M04]: `s`, and quitting during a run, cancel with `SIGINT`, so the outcome matches Ctrl-C in the CLI (exit code 130). `SIGTERM` (exit code 143) was rejected because the plan promised parity with Ctrl-C. Escape leaves a running job's view without stopping it, and `l` reopens it (or the last job's final state); a view that blocks the list until the job ends was rejected.
- **Change** [M03]: Milestone 03 is done. `dtc tui [--data-dir PATH] [--executable PATH] [--global-config PATH]` opens a read-only Textual job browser (`textual` is now a dependency): a job list with validation status, a detail view with the summary, prompt pairs, and dry-run plan (placeholder seed `0` when the job sets none), refresh with `r`, help with `?`, quit with `q` or Ctrl-C. The job-reading and job-text helpers moved from `cli/app.py` and `jobs/job_definition.py` to `jobs/job_report.py`; `report_ignored_config` became `ignored_config_lines`, which returns the lines. `validate-job` and `run-job --dry-run` output is unchanged, pinned by `tests/cli/test_job_output.py`. `JobService.preview` takes an optional `seed`.
- **Change** [M03]: Supersedes the [M02] "known limit, accepted for now" entry from this date. The module-level `_active_lock` in `cli/app.py` is gone. `ChildStartCallback` (`(pid, executable name)`) reaches the runner as an argument: `JobService.run(on_child_start=...)` and `GenerationService.execute(on_start=...)` forward it to the `RunnerFactory` as `on_start`, and `run-job` and `generate` pass the held lock's `record_child`. A runner built outside a held lock reports to nothing, as before; the orphaned-child guard is unchanged.
- **Design decision** [M03]: `dtc tui` checks the global configuration before starting the app and exits 2 on an error, rather than opening the app only to show it; either way no job is listed as invalid because of it. The cooldown and seconds text helpers moved to `jobs/job_report.py` with the rest, and `job_report` imports `JobPreview` only for type checking, since `job_service` uses them too.
- **Owner decision** [M03]: From a review of the plan. `dtc tui` takes `--executable` (same default as `run-job`), since the plan needs it and a local binary would otherwise always show the missing-tool message; a global configuration key and PATH only were rejected. `--data-dir` defaults to `PROJECT_ROOT/data`, so the TUI works from any working directory; this supersedes the `data/` default in the earlier [M03] owner decision from this date. The plan cache is cleared only by refresh; keying it by file modification times (misses the `dt-config` file, the global configuration, and tools) and no cache were rejected. Ctrl-C quits the app, bound explicitly rather than left to Textual's default. The `App` takes its `JobService` and settings as constructor arguments so tests can inject fakes, and the placeholder seed is `0` with a note.
- **Change** [M03]: Plan checked against the code after the `cli/app.py` cleanup (`load_settings`, `open_state`). Two `RunnerFactory` protocols exist (`GenerationService` and `JobService`), so both gain `on_start`, and the CLI factory binds the executable name. `report_ignored_config` logs, so it returns its warnings instead and the detail view shows them.
- **Change** [M03]: Milestone 03 plan revised after checking it against the code. The plan named a `read_job` in core that does not exist: it lives in `cli/app.py`, and `tui` may not import `cli`. M03 now starts by moving the job-reading and job-text helpers to `jobs/job_report.py`, with `validate-job` and `run-job --dry-run` output pinned by a test first. Job rows validate with `decode_input=False`; a missing data directory and an invalid global configuration have defined behavior; the run key for M04 becomes `x` because `r` is refresh.
- **Owner decision** [M03]: Shared job helpers go in `jobs/job_report.py`. Putting them in `state/` (wrong layer) and letting the TUI reimplement them (would drift from the CLI) were rejected.
- **Owner decision** [M03]: The detail view's dry-run plan shows the configured seed, or `random` with a placeholder seed in the commands, so it is stable across opens; a missing `draw-things-cli` or `ffmpeg` shows the message in the plan pane and the rest still renders. `JobService.preview` gains an optional `seed`; `run-job --dry-run` is unchanged. Mirroring `run-job --dry-run` exactly (random seed on every open, whole pane fails without tools) and a plan only on a key press were rejected.
- **Owner decision** [M03]: `--data-dir` defaults to `data/` and `--global-config` to `config/global-config.yaml`; sub-directories are not scanned.
- **Owner decision** [M03]: The module-level `_active_lock` in `cli/app.py` is removed in M03, not deferred to M04: `JobService.run()` takes `on_child_start` and `RunnerFactory` passes it as the runner's `on_start`. This supersedes the "known limit, accepted for now" [M02] entry from this date; the orphaned-child guard is unchanged.

- **Change** [M02]: Fixes from a second code review. `import-history` judges a phase 1 manifest left `running` by its `started_at` for expiry, so a pruned crashed execution is no longer re-imported with a fresh finish time on every import. `JobService` also treats `struct.error` from a malformed video like the other tagging failures: logged, the video kept as written, the run still succeeds.
- **Change**: Finished videos in a job are now tagged with a `colr` box (BT.709 primaries, sRGB transfer, BT.709 matrix; `nclc` for QuickTime files, `nclx` with limited range for others) by `jobs/video_color.py`, before the last frame is extracted, so the extraction decodes with the stated matrix. The box is written by editing the MP4/QuickTime boxes directly: it goes after the last child of the video sample entry, the enclosing box sizes grow by its length, and chunk offsets are shifted when `moov` precedes the media data. A real run with the installed `draw-things-cli` gave videos whose media data, packet timestamps, and decoded frames are identical to the untagged file. The file is replaced atomically by a copy that differs by that box, which is an exception to "nothing is ever overwritten" that the user asked for. A video that already has a `colr` box is left alone; a fragmented MP4 or a file that cannot be parsed is refused, logged, and kept as written, and the run still succeeds. `JobService` takes an optional `video_tagger`, which the CLI passes. `generate` does not tag its output. Design decision: remuxing with `ffmpeg -c copy` was rejected because it rewrites Draw Things' `pts < dts` timestamps (the duration changed from 0.31 s to 0.25 s); the sRGB transfer was chosen over BT.709's so the video and the PNG frames carry the same label.
- **Change**: Last-frame extraction decodes a video with no color matrix tag as BT.709 limited range instead of ffmpeg's BT.601 default, and honors a stated matrix. The current `draw-things-cli` writes untagged H.264 (an earlier one wrote ProRes tagged `smpte170m`). Against the input image of a real i2v run, BT.601 decoding left a green cast on saturated pixels (mean error 3.35 against 2.56 for BT.709 on the most saturated ones), and each chained run fed the cast back in. A real run with the installed CLI confirmed the untagged output and the new sRGB-labeled last frames. This supersedes the earlier entry's statement that the matrix is left as the file states it, for untagged files only. The video files themselves are still untagged: retagging them with ffmpeg `-c copy` was tried and rejected because Draw Things' files have `pts < dts` timestamps that ffmpeg's mov muxer rewrites (durations changed), so only a direct edit of the `colr` box would be lossless, and it is not done.
- **Change** [M02]: Fixes from the code review of Milestone 2. A failed `COMMIT` is rolled back so later writes on that thread work; `Store` closes its connection when opening fails; the lock file is written before it is truncated, so a refused starter always reads the holder; the recorder stores the manifest path resolved, as the import looks it up; a mid-run `sqlite3.Error` is no longer reported as a state database failure at startup; and the orphaned-child guard records the executable name the run started (line 2 of `run.lock` is now `PID name`) instead of assuming `draw-things-cli`, so a renamed or wrapped executable is recognized.
- **Design decision** [M02]: Known limit, accepted for now: `create_runner` still reaches the held lock through the module-level `_active_lock` in `cli/app.py`, so a runner built outside `held_run_lock` does not report its child and the orphan guard does nothing for it. Passing the reporter through `JobService` would remove the global but changes `RunnerFactory`; revisit it when Milestone 3 adds the TUI as a second caller.
- **Change**: Last frames are now written with color tags. Draw Things' `.mov` files are ProRes with a `smpte170m` matrix but no primaries or transfer, and the extracted PNG carried no color chunks. Extraction now converts to RGB first, at the depth the source needs, and then labels the frame sRGB (`bt709` primaries, `iec61966-2-1` transfer), so ffmpeg writes `sRGB`, `cHRM`, and `gAMA` chunks; pixels are identical to before (tested). The video itself is written by `draw-things-cli` and cannot be tagged from here. Labeling sRGB was chosen over embedding an ICC profile with Pillow, which would have reduced the 16-bit frames to 8 bits, and over `setparams` alone before conversion, which changed pixels. The matrix is left as the file states it (`smpte170m`); a comparison of four chained runs found BT.709 fit slightly better, too little to override the file's own tag.
- **Change** [M02]: Every real `run-job` is recorded in `state/dtc.db` (SQLite, WAL) through `state/recorder.py`, whatever `write_job_records` says; `--dry-run` and `generate` record nothing. `run-job` and `generate` take a `flock` on `state/run.lock` after validation and exit 75 when another run holds it (a busy lock is retried for 250 ms first). `dtc import-history [--directory PATH]` imports phase 1 manifests idempotently. `history_retention_days` (default 14, 0 to 3650) is a new global configuration key; `state/` is git-ignored (anchored as `/state/`, since a bare `state/` would also hide `src/draw_things_control/state/`). `JobStarted` gains `config_file`, `config_override`, and `input_resize`, and `DrawThingsProcessRunner` gains an optional `on_start(pid)`; `RunnerFactory` is unchanged, and `create_runner` passes the held lock's `record_child`. The user guide, architecture, and example configuration are updated.
- **Design decision** [M02]: The store's first schema is version 1. Retention is checked on every open, by `run-job`, `import-history`, and later the TUI and server. A process's own start-up is where a crash sweep runs, so no background thread or scheduler exists in Phase 2.
- **Owner decision** [M02]: A `SIGKILL`ed `dtc` must not let a second run start while its `draw-things-cli` child is still running. The lock file records the child's PID, and a starter that finds that process group alive (and named `draw-things-cli`) exits 75 without killing it. Accepting the limit and documenting it was offered and not chosen. A process-list scan for any `draw-things-cli` was rejected because it would block on a generation the user started by hand. Supersedes the "accepted and documented" part of the earlier [M02] entry on the run lock. This adds an `on_start(pid)` hook to the process runner.
- **Design decision** [M02]: Confirmed by the owner: `run-job` refuses, with exit 1, when the store or lock cannot be opened before a run, and the state directory is fixed at `state/` (no `state_directory` key).
- **Design decision** [M02]: On the layering question the owner asked for clean-architecture principles: dependencies point toward the more stable, inner layer. The events are the published observer contract of `jobs`; `state` is an adapter that implements an observer for them, so `state` -> `jobs` is the correct direction, and `jobs` never imports `state`. Moving the events to `core/` was rejected since `core/` would then hold `JobService` concepts. The rule stays `cli`, `tui`, `server` -> `state` -> `jobs` -> `core`.

- **Design decision** [M02]: A failure to take the lock or open the store before a run starts (unwritable `state/`, unsupported filesystem, newer schema) ends `run-job` with exit code 1 and starts nothing; only failures after the run has started are logged and ignored. Running unrecorded was rejected because it would give the TUI and the Phase 3 server a false history.
- **Design decision** [M02]: A busy lock is retried for 250 ms before it is reported, because read-only screens probe the lock by briefly taking it and must not make a starting run exit 75. A shared-lock probe and `fcntl` POSIX locks (`F_GETLK`) were rejected: the first still conflicts with an exclusive request, the second drops the lock when any descriptor to the file closes. The sweep also closes the `running` rows under a swept execution and sets its `finished_at`, so swept rows are pruned. `state/` files are created with mode 0600. Migrations run in `BEGIN IMMEDIATE`.
- **Design decision** [M02]: `import-history` searches the output directory recursively (manifests are in `<output_directory>/<job name>/`, not the top level; the earlier plan text was wrong) and takes `--directory` for outputs elsewhere.

- **Design decision** [M02]: The state directory is fixed at `state/` in the project; the planned `state_directory` key is dropped. The run lock lives beside the database, so a configurable location would give two processes with different global configurations two locks and let them drive the GPU at once. A fixed lock path outside the configuration was rejected as splitting the lock from the database for no gain.
- **Design decision** [M02]: Crash recovery runs while holding the run lock, before the new run inserts its row. The earlier plan released the lock and then swept, which could mark a just-started run `interrupted`. Read-only screens never write; they display `running` rows as `interrupted` when the lock is free. `recovered_at` separates a swept crash from a user cancel, which both read `interrupted`; a separate `crashed` status was rejected because Milestones 04 and 05 and Phase 3 already use `interrupted` for crashes.
- **Design decision** [M02]: Dependency direction becomes `cli`, `tui`, `server` -> `state` -> `jobs` -> `core`, because the recorder consumes `jobs/job_events.py`. Moving the event types into `core/` was rejected: they describe `JobService` concepts. `jobs` never imports `state`; the CLI wires the recorder in as an observer. AGENTS.md, `development-rules.md`, and `architecture.md` are updated.
- **Design decision** [M02]: Times are also stored as UTC epochs and ordering and retention use them, since local ISO text with an offset misorders across daylight-saving changes. The store requires WAL and refuses a state directory where it is unavailable (the project is on an external volume). The recorder stops for an execution after its first failure. The store also keeps model, log path, config file, resized input, and config override, so history shows which configuration ran.
- **Design decision** [M02]: `import-history` skips manifests already recorded (unique `manifest_path`, which the recorder also fills), uses the pruning rule (`finished_at`, else `started_at`) for expiry, imports a phase 1 manifest left `running` as `interrupted`, leaves columns a manifest cannot fill null, and needs no lock. A killed `dtc` releases the lock while its child may still run; this is accepted and documented.

- **Owner decision**: One start-to-finish invocation of a job is an "execution"; "run" keeps meaning one generation inside it. This replaces "job run" in the Phase 2 and Phase 3 plans: the `job_runs` table is `executions`, the `/outputs/{job_run_id}` endpoint is `/outputs/{execution_id}`, and `runs` rows point at their execution. The name "run history" and the run lock (which guards the whole process) are unchanged. Only planning documents used the old term; no code or stored data is affected. Follows the run/batch decision above.
  Milestone 02 and 05 now say "execution history" (the `history` screen and store keep their names); the milestone 05 file keeps its name so links stay valid.
- **Change** [M01]: `OutputProcessor` now strips the terminal codes `draw-things-cli` uses to redraw its progress bar (`ESC[1A ESC[K`, `\r`, and inline-image sequences) and drops lines left empty, so logs and events carry clean text such as `Sampling... 3 / 20 [█] 15%`. `ProcessMessage` and `RunOutput` gain `percent` (0-100 or `None`), which covers stages with no step counter (`Processing...`, `Generated`) and model downloads. A `[i/n]` file counter is no longer taken as `progress`. Blank child lines are no longer recorded.
- **Owner decision**: "run" is the only word for one generation in a job; "batch" no longer names a run's position. The job key `batch_count` is now `run_count`, and a prompt pair's `batches` list is now `runs`, with no alias for the old keys: validation rejects them with "was renamed to `run_count`" (or `runs`), so job files must be updated. `RunRecord.batch` (the manifest's per-run `batch` field) and `RunStarted.batch` are removed because they always equalled the run number, and log and `--dry-run` lines read `Run 3/7 (pair walk)`. "Batch" stays free for Draw Things' own `batchCount` and `batchSize`, which the job format does not use. Output file names never carried a batch suffix, so they are unchanged. The archived Phase 1 documents and changelog keep the old wording.

- **Change** [M01]: `JobService.run()` takes an `observer` and sends typed
  events (`jobs/job_events.py`); `cancel()` stops a running job from any
  thread and returns whether it did; `combine_observers` joins observers;
  `JobDefinition.source_text` holds the job file's text. `run-job` output,
  logs, manifests, and exit codes are unchanged. `RunnerFactory` may take an
  `on_message` fourth argument, always passed and typed as a `Protocol`
  (`cli/app.py` passes it to `OutputProcessor`),
  and `interruptible_wait` takes a `wake_fd`. One `JobService` runs one job at
  a time; a second concurrent `run()` raises `RuntimeError`.
- **Change**: Last-frame extraction now runs ffmpeg with `-sseof -1`, `-update 1`, and `-q:v 1` (it was `-sseof -3` without `-q:v`), so it seeks to the final second and requests top quality. The Phase 1 documents are archived and keep the old wording.
- **Owner decision** [M01]: `JobService` keeps writing every job and run log
  line; events are additive. Supersedes the earlier design decision that the
  CLI's log lines come from one observer. Alternatives: a `LogObserver` only
  the CLI installs (rejected: TUI and server job log files would lack the job
  lines, and many lines have no event), and a `LogObserver` the service always
  installs (rejected: a wider event surface and a risk of output drift).
- **Owner decision** [M01]: `cancel()` returns `False` and does nothing when no
  job is running, so a stale cancel cannot stop a later job. Latching the
  cancel until the next run was rejected.
- **Design decision** [M01]: `JobDefinition` keeps `source_text`, read once in
  `load_job` and carried by `JobStarted`, so Milestone 02 stores the exact text
  that ran. Re-reading the file in the recorder was rejected because an edit
  in between would store the wrong text.
- **Design decision** [M01]: Child lines reach `RunOutput` through the
  existing `OutputProcessor` callback, with the factory gaining an optional
  `on_message` argument, instead of a new runner `on_output` hook. Events
  also carry the run's parsed `progress`. `JobFinished` and `RunFinished` are
  sent on every exit path, including exceptions, so a `running` history row
  means a crash. Cancelling a cooldown uses a `wake_fd` the service owns and
  passes to `interruptible_wait`; the pipe used to be private to the wait.
  The `install_signals` parameter on `run()` is dropped: the existing
  `handle_signals` constructor argument already covers it.
- **Owner decision**: No source files in the project root. Supersedes the
  "core stays flat" part of the subpackage decision below. All code moves to
  one package, `src/draw_things_control/` (uv `src` layout, `uv_build`
  backend), with `core/`, `jobs/`, and `cli/` now, and `state/`, `tui/`,
  `server/`, `mcp_server/` as phases land. Alternatives: a root-level package
  without `src/`, and several top-level packages.
- **Change**: `main.py` is removed. The CLI is the `dtc` console script
  (`uv run dtc ...`) or `python -m draw_things_control`; `run.sh`,
  `validate.sh`, and the Makefile use it. Tests moved to `tests/core`,
  `tests/jobs`, `tests/cli`, run with `unittest discover -s tests -t .`. New
  Phase 2 and 3 modules are placed as `jobs/job_events.py`, `state/store.py`,
  `state/recorder.py`, `core/run_lock.py`, and `jobs/job_queue.py`. Phase 1
  documents keep their historical `main.py` wording.

- **Change**: Documentation reorganized. The root README is now a short
  overview. Its usage, job, and exit-code content moved to
  `docs/user-guide.md` and `docs/architecture.md`, and the conventions in
  AGENTS.md moved to `docs/development-rules.md`, which AGENTS.md now
  summarizes and links. `docs/README.md` indexes everything. No rule changed.

- **Change**: Phase 2 is planned. The phase document and five milestone
  documents are written; no code yet. AGENTS.md's project layout line is
  amended for the new subpackages (see the owner decision below).

- **Design decision** [M02]: An execution stores its exact YAML text and the
  settings it was resolved with (directories, cooldown and source), not a
  serialized `JobDefinition`. The parsed object holds paths and a resize
  plan and is not meant to be serialized; the text is enough to show, and
  in Phase 3 to re-run, the job as it was.
- **Design decision** [M01]: `RunOutput` events come from an `on_output`
  callback on the process runner, called where it logs each child line. The
  alternative, parsing Loguru output, was rejected as fragile. Front ends
  that own the terminal must not install the stdout and stderr sinks.
- **Design decision** [M02]: Retention deletes database rows only, never
  outputs, manifests, or logs, and never a `running` row. Imported manifests
  older than the cutoff are skipped so they are not imported and pruned at
  once.
- **Design decision** [M05]: History is shown from the state store only, and
  the store is filled by every recorded run. The alternative, reading the
  manifests beside the outputs, was rejected because it needs a directory
  scan and cannot show runs whose output directory has changed. The
  manifests stay as a per-job artifact.
- **Design decision** [M02]: The state store records every `run-job` run,
  even when `write_job_records` is false. That key controls only the
  manifest and log files beside the outputs; history must not depend on it.
  Dry runs record nothing and take no lock.
- **Design decision** [M02]: The run lock is an `fcntl.flock` on
  `state/run.lock`, not the existence of a lock file, so the operating
  system releases it when the holder dies and no stale lock can block the
  GPU. Alternatives rejected: a pidfile (stale after a crash) and a SQLite
  flag row (also stale after a crash).
- **Design decision** [M02]: A busy lock exits with code 75
  (`EX_TEMPFAIL`), so scripts can tell "GPU busy, try later" from a failed
  run. The lock is held through cooldowns, since their purpose is to let the
  machine cool.
- **Design decision** [M01]: Front ends observe a job through a typed event
  callback, not through Loguru output. The CLI's log lines are produced by
  one observer, so its output does not change. A `threading.Event` set from
  a signal handler stays rejected (see the phase-1 changelog, [M04]).
- **Design decision** [M02]: Credentials are redacted before anything is
  stored or emitted, with the existing `GenerationService.redact_command`.

- **Owner decision**: History is kept for 14 days by default. The global key
  `history_retention_days` overrides it (0 keeps history forever). Keeping
  history forever was offered as the default and not chosen.
- **Owner decision**: The TUI uses Textual.
- **Owner decision**: The TUI browses and runs jobs only; it does not edit
  them. This also avoids a comment-preserving YAML dependency.
- **Owner decision**: State (run history, and in Phase 3 the queue) lives in
  SQLite, using the standard library `sqlite3`. Job definitions stay as YAML
  files in `data/`, and manifests and logs stay beside the outputs. The
  database arrives in Phase 2, not Phase 3, so the CLI, the TUI, and the
  server all record runs the same way.
- **Owner decision**: Only one process may drive the GPU. A run lock makes
  the CLI and the TUI refuse to start a run while another runner, including
  the Phase 3 server, holds it. The alternative of making the TUI a client of
  the server was not chosen.
- **Owner decision**: The one-off `generate` command takes the run lock and
  is not recorded.
- **Owner decision**: New front ends live in subpackages (`tui/`, `server/`,
  `mcp_server/`) while the core stays flat. This amends the AGENTS.md
  "flat modules" layout line. Prefixed flat modules were the alternative.
- **Owner decision**: Dependencies for the new front ends are always
  installed with `uv sync`, not offered as optional extras.
