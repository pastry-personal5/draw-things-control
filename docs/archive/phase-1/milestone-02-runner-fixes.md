# Milestone 02: Runner Fixes

**Phase:** [Phase 1: Basic Functionalities](README.md)
**Status:** done
**Order:** done before [Milestone 01](milestone-01-job-definition-batch.md),
which runs many generations in a row through this runner

## Goal

Make `DrawThingsProcessRunner` and `GenerationService` report exit codes
correctly, shut down reliably on every platform, and never lose output, so
that jobs can depend on them. The bugs come from the code review of commit
`b99603b`.

## Scope

In scope, one fix per finding:

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `generation_service.py` | Ctrl-C and SIGTERM return the child's raw code: -15 becomes 241, and a child that exits 0 after SIGTERM makes an interrupted run look successful | Map by cause: a timeout returns 124; a run stopped by the wrapper's own signal returns `128 + signal number` (130 for SIGINT, 143 for SIGTERM), whatever the child returned; a child killed by an outside signal returns `128 + signal number` |
| 2 | `draw_things_runner.py` | The shutdown loop never reaps the child and has no upper bound after SIGKILL; on Linux a zombie leader keeps `killpg(pgid, 0)` succeeding, so it hangs forever | Reap with `process.poll()` / `process.wait(timeout)` in the shutdown loop, and bound the wait after SIGKILL (for example 5 seconds), then log a warning and return |
| 3 | `draw_things_runner.py` | The last `_drain_output` runs before the reader threads are joined, so the child's last lines are dropped | Join the reader threads (with a timeout) first, then drain the queue |
| 4 | `draw_things_runner.py` | Strict UTF-8 decoding: one bad byte kills a reader thread, closes the pipe, and the child dies of SIGPIPE | Open the pipes with `errors="replace"` |
| 5 | `draw_things_runner.py` | Signal handlers are installed after `Popen`, outside `try`/`finally`; a Ctrl-C in that window leaves the child running unsupervised | Install the handlers before starting the process, and start it inside the `try` so cleanup always runs |
| 6 | `draw_things_runner.py` | Piped stdout/stderr hide the terminal from the child, so `generate` without `--output`, or with `--terminal-image`, shows and saves nothing | When `--output` is omitted or `--terminal-image` is set, let the child inherit the terminal (no pipes, no output capture); otherwise keep piping |

Out of scope:

- The cleanup findings from the same review (the duplicated `--config-json`
  parsing, the `locals()` option passing, retained message objects, and
  README paths). They can be done alongside, but are not required.
- New features.

## Planned changes

| File | Change |
|------|--------|
| `draw_things_runner.py` | Fixes 2–6 |
| `generation_service.py` | Fix 1; `GenerationOutcome` keeps `timed_out` and `termination_signal` so the CLI can explain the exit code |
| `tests/test_runner.py` | Tests for each fix, using small Python child processes (no `draw-things-cli`) |
| `tests/test_service.py` | Exit-code mapping for timeout, wrapper signal with child exit 0 and -15, and outside signal |
| `README.md` | Document the exit codes (0, 1, 2, 124, 128 + signal) |

## Acceptance criteria

- [x] Ctrl-C during a run exits with 130, and SIGTERM with 143, even when the
      child handles the signal and exits with 0.
- [x] A timeout exits with 124.
- [x] A child that ignores SIGTERM and is killed after the grace period does
      not hang the runner on Linux or macOS; `test_runner_timeout_returns_promptly`
      passes on both.
- [x] A child's last stderr lines, written just before it exits after SIGTERM,
      appear in the log.
- [x] A child writing invalid UTF-8 keeps running, and the bytes are logged as
      replacement characters.
- [x] A signal arriving immediately after launch still stops the child's
      process group.
- [x] `generate` without `--output` lets `draw-things-cli` preview in the
      terminal.
- [x] `make check` passes.

## Verification

- `make check` passes. New tests cover the exit-code mapping, the last lines
  after SIGTERM, invalid UTF-8 output, a shutdown requested before launch,
  and a child that ignores SIGTERM being killed within bounds.
- Run on macOS only. The Linux zombie hang (fix 2) is fixed by reaping the
  leader and bounding the wait after SIGKILL, but has not been run on Linux.
- Fix 6 is covered by a test that `generate` without `--output` does not
  capture the child's output; an inline preview with the real CLI was not
  tried.
- The review's cleanup findings (out of scope) were not done.

## Open questions

None.
