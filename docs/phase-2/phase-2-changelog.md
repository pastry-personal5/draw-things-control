# Phase 2 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 2](README.md). Newest first.

## 2026-09-25

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
