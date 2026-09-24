# Milestone 04: Cooldown Between Runs

**Phase:** [Phase 1: Basic Functionalities](README.md)
**Status:** done
**Depends on:** [Milestone 01: Job definition and chained batch runs](milestone-01-job-definition-batch.md)

## Goal

Let a job pause for a fixed time between runs. Video runs in particular keep
the GPU fully busy for many minutes, and a long chain of them back to back
heats the machine until it throttles, which slows every later run. A cooldown
between runs lets the machine recover, so a long unattended job runs at a
steadier speed.

## Scope

In scope:

- A new optional global configuration key, `cooldown_seconds`: the default
  cooldown for every job on this machine
- A new optional root-level job key, `cooldown_seconds`, which overrides the
  global value for that job, in every mode (`i2i`, `t2v`, `i2v`)
- Waiting that long after each successful run, before the next run starts
- Stopping the wait at once on `SIGINT`, `SIGTERM`, or `SIGHUP`, with the same
  outcome as a signal between runs today
- Reporting the cooldown and where it came from in `validate-job`,
  `run-job --dry-run`, `run-job`, the job log, and the job manifest

Out of scope:

- Waiting based on temperature or macOS thermal state (for example,
  `pmset -g therm`), or scaling the wait with the last run's length; the
  wait is a fixed time
- A different cooldown per mode in the global configuration
- A different cooldown per run or per prompt pair
- A cooldown before run 1 or after the last run
- The one-off `generate` command

## Where the value comes from

The cooldown for a job is the first of these that is set:

| Source | Key | Reported as |
|--------|-----|-------------|
| The job file | `cooldown_seconds` (root level) | `job` |
| The global configuration | `cooldown_seconds` | `global_config` |
| The app | built in: 0 (no wait) | `default` |

The global value applies to every mode, so a job that should never wait
sets `cooldown_seconds: 0` to turn the global value off.

### Global configuration key

```yaml
version: 1
input_directory: ~/draw-things/input
output_directory: ~/draw-things/output
write_job_records: false
cooldown_seconds: 900                  # optional; 0 to 3600; default 0 (no wait)
```

- `cooldown_seconds` is optional. When it is absent, the default is 0.
- `config/global-config.example.yaml` sets it to 900 (15 minutes), with a
  comment explaining it. An existing `config/global-config.yaml` has no such
  key, so it keeps running with no cooldown until the user adds one; the app
  never edits that file.
- Invalid values fail every command that reads the global configuration,
  with exit code 2, naming the file and the key, as for the other keys.

### Job file key

```yaml
mode: i2v
batch_count: 7
cooldown_seconds: 120                  # optional; 0 to 3600; overrides the global cooldown_seconds
```

- `cooldown_seconds` is optional, and allowed in every mode.
- When set, it replaces the global value for this job, including 0, which
  means no wait.

### Values

In both files, the value is a number of seconds from 0 to 3600: an integer
or a float, not a boolean or a string. The limit of one hour leaves room
above the 15-minute example and catches a value typed in the wrong unit (for
example, minutes multiplied by 60 twice). Out of range, the error is:

```
data/sunset.yaml: 'cooldown_seconds' must be a number of seconds from 0 to 3600
```

The cooldown does not count toward `run_timeout_seconds`, which still
limits each run on its own.

## When the cooldown happens

The job waits after run *k* finishes, before run *k*+1 starts, when all of
these hold:

- Run *k* succeeded, including extracting its last frame for a video job. A
  failed, timed-out, or interrupted run stops the job as before, with no
  wait.
- Run *k* is not the last run. The job finishes as soon as the last run
  does.
- No signal has arrived since run *k* finished.
- The job's cooldown is greater than 0.

So a job with `batch_count: 7` waits 6 times. With `batch_count: 1`, it never
waits.

The wait is measured with a monotonic clock, so a system clock change does
not shorten or lengthen it. The next run's output name is chosen when that
run starts, after the wait, so the timestamp in the name is still the time
the run started.

## Interrupting a cooldown

A signal during the cooldown ends the wait at once and stops the job, as a
signal between runs does now ([Milestone 01, Interrupting a
job](milestone-01-job-definition-batch.md#interrupting-a-job)):

1. No later run starts.
2. The job is marked `interrupted` in the manifest, and the log says which
   signal stopped it, that it came during the cooldown, and how long the job
   had waited:
   `Job stopped by SIGINT during the cooldown before run 2/7 (waited 412.3 s of 900 s)`.
   This replaces the usual `Job stopped by SIGINT before run 2/7` line for a
   stop during a cooldown.
3. The app exits with `128 + signal number` (130 for Ctrl-C).

Completed runs and their outputs are unaffected: every run before the
cooldown stays `succeeded`.

### How the wait wakes on a signal

The wait uses a wake-up pipe, so a signal ends it at once, with no polling.
It is `interruptible_wait(seconds, stopped, wake_on_signal=...)` in
`draw_things_runner.py`, next to `install_signal_handlers`; the job service
calls it with `stopped` reading `self._interrupt`.

1. Before the wait, it opens a pipe with `os.pipe()`, makes both
   ends non-blocking, and registers the write end with
   `signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)`, keeping the
   previous wake-up fd.
2. Only then does it check `self._interrupt`, so a signal that lands between
   the check and the wait still leaves a byte in the pipe and is not lost.
   It waits with `select.select([read_end], [], [], remaining)`. When a
   handled signal (`SIGHUP`, `SIGINT`, `SIGTERM`) arrives, Python's C-level
   handler writes a byte to the pipe, so `select` returns; the job's Python
   handler then sets `self._interrupt`, as it does today.
3. After `select` returns, the service drains the pipe and checks
   `self._interrupt`. If it is set, the wait ends. If not (another signal
   woke it), the service recomputes the time left from `time.monotonic()`
   and waits again. When the time is up, the wait ends.
4. In a `finally`, the service restores the previous wake-up fd and closes
   both ends of the pipe, whether the wait finished, was interrupted, or
   raised.

The job's signal handler only sets `self._interrupt`, as it does now. It
does not touch a lock or an event: a `threading.Event` set from the handler
could deadlock, since `Event.wait()` holds a non-reentrant lock that the
handler, running on the same thread, would try to take again.

`signal.set_wakeup_fd` works only on the main thread and only while the job's
signal handlers are installed. When they are not (`handle_signals=False`, or
the job runs off the main thread), the service waits with the same `select`
call and no wake-up fd, since no signal can stop the job then anyway. Nothing
else in the app uses a wake-up fd; the job's runners are created with
`handle_signals=False` and leave signal handling to the job service.

Only a wait that ended early counts as cut short. A signal that arrives
after the full wait stops the job before the next run with the usual
`Job stopped by SIGTERM before run 2/7` line, and the run's
`cooldown_after_seconds` is the full wait.

The "until" time in the start line is local time, like every other
timestamp in the log and manifest.

For tests, the service takes the wait as a constructor argument
(`cooldown: Callable[[float], float]`, returning the seconds waited), so
tests do not sleep; the pipe itself is tested once, by sending the process a
real `SIGINT` during a short wait.

## Reporting

The source is shown with the value everywhere, in the style of the seed:
`job`, `global_config`, or `default`.

- `validate-job` prints a new line after `runs`:
  - `  cooldown: 900 s between runs, from global_config (6 waits, 1 h 30 min total)`
  - `  cooldown: 900 s between runs, from global_config (no waits: 1 run)`
  - `  cooldown: none (default)` when the value is 0, with the source that
    set it (`none (job)` when the job turns a global value off).
- `run-job --dry-run` prints the same summary in its header line, and a
  comment line between runs that will have a cooldown:

  ```
  # Job sunset-walk (i2v): 7 runs, seed 829023514 (config_file), cooldown 900 s (global_config)
  # Run 1/7 (batch 1, pair walk)
  draw-things-cli generate ...
  # Cooldown 900 s
  # Run 2/7 (batch 2, pair wave)
  draw-things-cli generate ...
  ```

  With no cooldown, the header says `no cooldown` and there are no cooldown
  comments.
- `run-job` names the cooldown in the job's opening line, and logs an INFO
  line when each wait starts and another when it ends:

  ```
  Job sunset-walk (i2v): 7 runs, seed 829023514 (from config_file), cooldown 900 s (from global_config); ...
  Cooldown: waiting 900 s before run 2/7 (until 15:59:12)
  Cooldown finished; starting run 2/7
  ```

  A wait ended by a signal logs the WARNING line from "Interrupting a
  cooldown" instead of the finished line.
- The job manifest gets two job-level fields: `cooldown_seconds`, the value
  used (0 for none), and `cooldown_source` (`job`, `global_config`, or
  `default`). Each run record gets `cooldown_after_seconds`: the time the
  job waited after that run, rounded to 0.1 s, or `null` when it did not
  wait (the last run, a run that did not succeed, or no cooldown).

  The manifest is saved when a wait starts, with `cooldown_after_seconds`
  set to 0, so anyone watching it during a long pause can see the job is
  cooling down; and saved again when the wait ends, with the time waited.
  A wait cut short by a signal is recorded the same way, with the shorter
  time, and the job's `status` is `interrupted`:

  ```json
  "cooldown_seconds": 900,
  "cooldown_source": "global_config",
  "status": "interrupted",
  "runs": [
    {"batch": 1, "status": "succeeded", "cooldown_after_seconds": 900.0, "...": "..."},
    {"batch": 2, "status": "succeeded", "cooldown_after_seconds": 412.3, "...": "..."}
  ]
  ```

## Planned changes

| File | Change |
|------|--------|
| `global_config.py` | Add `cooldown_seconds` to `GLOBAL_CONFIG_KEYS`; validate type and range with `is_cooldown`; `GlobalConfig.cooldown_seconds: float | None` (None when absent); `is_number` moves here from `job_definition.py`, which now imports it |
| `job_definition.py` | Add `cooldown_seconds` to `JOB_KEYS`; validate type and range; `JobDefinition.cooldown_seconds: float` and `cooldown_source: str`, resolved from the job, the global configuration, or the default; `seconds_text`, `duration_text`, `cooldown_summary`, and `cooldown_details` format the value for the CLI and the log |
| `draw_things_runner.py` | `interruptible_wait`: the wake-up-pipe wait, next to the other signal helpers |
| `job_service.py` | New constructor argument `cooldown: Callable[[float], float]` (default: `interruptible_wait` ended by the job's signal handler, returning the seconds waited); one `_stop` helper for every signal stop; in `_run_chain`, after a successful run that is not the last, log, save the manifest, wait, record `cooldown_after_seconds`, save again, and log the end; if a signal arrived, log the cooldown stop line, mark the job `interrupted`, and stop before the next run |
| `job_manifest.py` | `JobManifest.cooldown_seconds` and `cooldown_source`; `RunRecord.cooldown_after_seconds` |
| `main.py` | `validate-job` prints the cooldown line; `run-job --dry-run` prints the cooldown in its header and `# Cooldown <n> s` between runs |
| `config/global-config.example.yaml` | `cooldown_seconds: 900` with a comment |
| `data/example-job.yaml` | A commented `cooldown_seconds` example that overrides the global value |
| `README.md` | Document both keys, the precedence, when the wait applies, and how a signal ends it |
| `docs/phase-1/milestone-01-job-definition-batch.md` | One sentence in "Interrupting a job": a signal during a cooldown stops the job the same way, and links here |
| `tests/test_global_config.py` | Absent means 0; integer and float accepted; 0 and 3600 accepted; negative, over 3600, `.nan`, `.inf`, a boolean, and a string rejected |
| `tests/test_job_definition.py` | The same value rules for the job key, in every mode; precedence: job over global, global over default, and a job's 0 over a global 900; the source of each |
| `tests/test_job_service.py` | With a fake wait: the number and length of waits (`batch_count - 1`); no wait after the last run, after a failed or timed-out run, or when the value is 0; a signal during the wait marks the job `interrupted`, starts no further run, and exits with `128 + signal number`; `cooldown_after_seconds` saved as 0 when the wait starts and as the time waited when it ends, including a cut-short wait; the start, end, and stop log lines. With the real wait: a `SIGINT` sent during a 5 s wait ends it in well under a second, and the previous wake-up fd is restored and the pipe closed afterwards |
| `tests/test_job_cli.py` | `validate-job` cooldown line with each source; `--dry-run` header and cooldown comments between runs, none after the last run |

## Acceptance criteria

All met; see Verification.

- [x] With `cooldown_seconds: 900` in the global configuration and no job
      key, a job with `batch_count: 3` waits 900 s after run 1 and after run
      2, and not after run 3, in every mode.
- [x] A job's `cooldown_seconds` overrides the global value, and
      `cooldown_seconds: 0` in the job turns the wait off.
- [x] With neither key set, the job runs exactly as before: same commands,
      no wait, no cooldown log lines.
- [x] A failed or timed-out run stops the job with no wait.
- [x] Ctrl-C during a cooldown ends it at once, starts no further run, marks
      the job `interrupted`, records the time waited on the previous run,
      and exits with 130.
- [x] After a wait, the process's previous wake-up fd is restored and the
      pipe is closed.
- [x] A value out of range, or not a number, in either file fails with exit
      code 2 and names the file and the key.
- [x] `validate-job`, `run-job --dry-run`, the `run-job` log, and the
      manifest show the cooldown and its source.
- [x] `config/global-config.example.yaml` sets `cooldown_seconds: 900`.
- [x] `make check` passes.

## Verification

- `make check` passes: 153 tests (19 new), Ruff and Black clean.
- End to end through the real CLI with a stub `draw-things-cli`, an `i2i`
  job with 3 runs, and `cooldown_seconds: 2` in the global configuration:
  - `validate-job` prints
    `cooldown: 2 s between runs, from global_config (2 waits, 4 s total)`,
    and `run-job --dry-run` shows the cooldown in its header and
    `# Cooldown 2 s` between runs, none after the last.
  - `run-job` took 4.7 s, logged each wait's start and end, and the manifest
    recorded `cooldown_seconds: 2.0`, `cooldown_source: global_config`, and
    `cooldown_after_seconds` of `[2.0, 2.0, null]`.
- The same job with `cooldown_seconds: 60`, sent `SIGINT` during the first
  wait: the process exited with 130 within 0.19 s of the signal, logged
  `Job stopped by SIGINT during the cooldown before run 2/3 (waited 0.2 s of 60 s)`,
  started no further run, and the manifest has status `interrupted` with
  run 1 `succeeded` and `cooldown_after_seconds: 0.2`.
- The unit test for the real wait sends `SIGINT` to the test process during a
  5 s wait; the wait ends in under a second, the previous wake-up fd is
  restored, and no file descriptor is left open.
- Code review fixes: the "until" time is local time; a signal after a full
  wait is not reported as cutting it short; one stop path; one number
  check; the wait moved to `draw_things_runner.interruptible_wait`; tests
  inject the wait through the constructor; seconds print as written
  (`1234.5678 s`, `0.00001 s`), and totals keep tenths (`0.4 s`); the
  manifest's cooldown fields have no default. After the move, `SIGINT`
  during a 60 s wait still stopped the job within 0.19 s, with exit code
  130.
- Not run with the real `draw-things-cli`, or with a real video model.

## Open questions

None. The owner answered them before work started (see the
[changelog](phase-1-changelog.md)).
