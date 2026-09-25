# Phase 2 Changelog

Owner decisions, design decisions, and notable changes for
[Phase 2](README.md). Newest first.

## 2026-09-25

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

- **Design decision** [M02]: A job run stores its exact YAML text and the
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
