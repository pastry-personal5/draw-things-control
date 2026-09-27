# Milestone 07: Automatic Cooldown

**Phase:** [Phase 2: Terminal UI for Humans](README.md)
**Status:** done
**Depends on:** none (it changes the cooldown between a job's runs)

## Goal

Let the wait between a job's runs follow how hard the machine just worked. In
the new `auto` mode, a run that took A minutes is followed by a wait of A/2
minutes (the ratio can be set), kept between a minimum and a maximum. The
fixed wait of today stays as the `manual` mode, and `off` turns the wait off.
All three are written as one `cooldown` mapping, in the global configuration
and, to override it, in a job file. With no `cooldown` anywhere, `auto`
applies with its defaults.

## Scope

In scope:

- A `cooldown` mapping with `mode: auto` (a share of the last run's time,
  within `minimum_seconds` and `maximum_seconds`), `mode: manual` (a fixed
  `seconds`), or `mode: off`, in `config/global-config.yaml` and in job files
- `auto` with its defaults when neither file sets `cooldown`
- The old `cooldown_seconds` key rejected in both files, with a message
  naming its replacement
- The job service working out each wait from the run that just finished
- `validate-job`, `run-job --dry-run`, the job log, the manifest, the state
  store, and the TUI showing which mode applies and what each wait was
- `config/global-config.example.yaml` and `data/example-job.yaml` in the new
  form

Out of scope:

- Editing `config/global-config.yaml`. It is a user file: the owner's copy
  has `cooldown_seconds: 1200` and fails validation until the owner rewrites
  it as `cooldown: {mode: manual, seconds: 1200}` or chooses another mode.
  The error says how.
- Waits based on anything but the last run's time (temperature, the time of
  earlier runs, or `draw-things-cli` output), and estimating run times from
  execution history.
- A cooldown after a failed or interrupted run: the job stops there, as
  today, so there is no next run to wait for.
- A state store migration. The execution row's `cooldown_seconds` column is
  kept, and the mode goes in its `settings` JSON.
- Rewriting execution history or old manifests: they keep
  `cooldown_seconds`, which means `manual`.

## Planned changes

### The `cooldown` mapping

The same mapping in both files:

```yaml
# Half of the last run's time, kept between the two bounds (the default).
cooldown:
  mode: auto
  ratio: 0.5               # optional, default 0.5
  minimum_seconds: 300     # optional, default 0
  maximum_seconds: 1800    # optional, default 3600
```

```yaml
# A fixed wait after each successful run but the last.
cooldown:
  mode: manual
  seconds: 900
```

```yaml
# No wait.
cooldown:
  mode: off
```

| Key | Modes | Meaning |
|-----|-------|---------|
| `mode` | all | `auto`, `manual`, or `off`; required |
| `ratio` | `auto` | The share of the last run's time to wait, above 0 and up to 1 (default 0.5) |
| `minimum_seconds` | `auto` | The shortest wait, 0 to 3600 (default 0) |
| `maximum_seconds` | `auto` | The longest wait, 0 to 3600 (default 3600); not below `minimum_seconds` |
| `seconds` | `manual` | The wait, 0 to 3600; required. `0` is allowed and means no wait, like `off` |

- The auto wait after run k is
  `min(maximum_seconds, max(minimum_seconds, ceil(T × ratio)))`, where T is
  run k's time in seconds: the time its `draw-things-cli` ran, as the
  manifest's `seconds` already records it. Extracting the last frame and
  tagging colors are not counted. The share is rounded up to a whole second,
  so a 1201-second run waits 601 seconds at the default ratio.
- A wait of 0 seconds (`off`, manual `seconds: 0`, or an auto result of 0) is
  no wait: no cooldown events and no countdown, as a `cooldown_seconds: 0`
  job behaves today.
- `mode: off` takes no other key.
- Validation is strict. A missing or unknown `mode`, a key of another mode
  (`seconds` with `auto`, `ratio` with `manual`, anything with `off`), an
  unknown key, a value out of range, a non-number (booleans are not
  numbers), or `minimum_seconds` above `maximum_seconds` is an error naming
  the key, for example
  `'cooldown.minimum_seconds' must be a number of seconds from 0 to 3600`.
  In a job this is a validation error (exit code 2); in the global
  configuration it stops every command that reads it (exit code 2).
- `mode` values are strings; YAML reads an unquoted `off` as the boolean
  `false`, so `mode: off` is accepted in that form as well and means `off`.
  No other boolean is accepted.
- The 3600-second limit is today's `MAX_COOLDOWN_SECONDS`, unchanged.

### Precedence and the default

- A job's `cooldown` replaces the global one as a whole; keys are not merged
  across the two files.
- With neither set, `auto` applies with its defaults (ratio 0.5, 0 to 3600
  seconds), source `default`. This changes behavior for anyone without a
  cooldown today, who had no wait: every run but the last is now followed by
  half its time, up to an hour. `mode: off` restores the old behavior.
- The source names stay `job`, `global_config`, and `default`.

### The old key

- In the global configuration, `cooldown_seconds` is an error:
  `'cooldown_seconds' was replaced by 'cooldown'; write cooldown: {mode: manual, seconds: 1200} for the same wait, or use mode auto or off (see config/global-config.example.yaml)`,
  with the file's own value in place of 1200 (`{mode: off}` when it is 0).
- In a job file, the same message, in the form jobs already use for renamed
  keys (`'batch_count' was renamed to run_count`).
- There is no fallback that reads the old key.

### Code (`core/global_config.py`, `jobs/`)

- `core/global_config.py` gains a frozen `CooldownPolicy` (`mode`, `ratio`,
  `seconds`, `minimum_seconds`, `maximum_seconds`), its parser
  `parse_cooldown(value, key)` shared by both files, the default policy, and
  `CooldownPolicy.wait_after(run_seconds) -> CooldownWait`: the seconds and
  whether a bound set them. `GlobalConfig.cooldown_seconds` becomes
  `GlobalConfig.cooldown: CooldownPolicy | None`.
- `jobs/job_definition.py`: `JOB_KEYS` swaps `cooldown_seconds` for
  `cooldown`; `JobDefinition.cooldown_seconds` becomes
  `JobDefinition.cooldown: CooldownPolicy`, with `cooldown_source` unchanged.
- `jobs/job_service.py`: after a successful run that is not the last, the
  wait is `job.cooldown.wait_after(record.seconds)`. `_cool_down` takes that
  wait and the run's time instead of reading one fixed value. Signals,
  cancellation, and the injected `cooldown` callable are unchanged.

### Events, manifest, and state store

- `JobStarted` carries `cooldown: CooldownPolicy` in place of
  `cooldown_seconds`, and keeps `cooldown_source`.
- `CooldownStarted` gains `mode`, `run_seconds` (the time of the run it
  follows), and `bound` (`minimum`, `maximum`, or none), so a front end can
  say why the wait is that long. `seconds` stays the wait itself, so the TUI
  countdown needs no change.
- The manifest keeps `cooldown_seconds` (the manual `seconds`, 0 for `off`,
  or `null` for `auto`) and `cooldown_source`, and gains `cooldown`: the
  mapping as resolved, defaults filled in. Each run's
  `cooldown_after_seconds` records the wait it was given, as today.
- The recorder writes the execution's `cooldown_seconds` column the same way
  and the resolved mapping under `settings.cooldown`. `import-history` copies
  a manifest's `cooldown` when it has one. No schema change.

### What people see

| Where | `manual` (unchanged) | `auto` | `off` |
|-------|----------------------|--------|-------|
| `validate-job`, TUI `/job` and `/apply` confirmation | `900 s between runs, from global_config (2 waits, 30 min total)` | `auto: half of each run's time, 5 min to 30 min, from global_config (up to 2 waits, 1 h total at most)` | `off (job)` |
| `run-job --dry-run` header | `cooldown 900 s (global_config)` | `cooldown auto, half of each run, 5 min to 30 min (global_config)` | `no cooldown (job)` |
| `run-job --dry-run`, between runs | `# Cooldown 900 s` | `# Cooldown auto: half of run 1's time, 5 min to 30 min` | nothing |
| Job log, before a wait | `Cooldown: waiting 15 min before run 2/3 (until 14:05:00)` | `Cooldown: waiting 12 min, half of run 1's 24 min, before run 2/3 (until 14:05:00)` | nothing |
| TUI Messages, before a wait | `Cooldown 15 min before run 2, until 14:05:00` | `Cooldown 12 min (half of run 1's 24 min) before run 2, until 14:05:00` | nothing |
| TUI `/execution` detail | `cooldown: 900 s (global_config)` | `cooldown: auto, half, 5 min to 30 min (global_config)` | `cooldown: off (job)` |

- A ratio other than 0.5 is written as a percentage: `40% of each run's time`,
  `40% of run 1's 24 min`.
- A wait set by a bound says so:
  `5 min (the minimum; half of run 1's 3 min is less)` and
  `30 min (the maximum; half of run 1's 80 min is more)`.

### Files changed by hand in this milestone

- `config/global-config.example.yaml`: the `cooldown_seconds: 900` block
  becomes a `cooldown` mapping with `auto` active and `manual` and `off`
  shown in comments.
- `data/example-job.yaml`: the commented `cooldown_seconds: 300` line
  becomes a commented `cooldown` mapping.
- `data/duo.yaml` sets no cooldown and does not change; it follows the global
  configuration.

## Documentation

In the same change:

- `docs/user-guide.md`: the global configuration table, the job key table,
  and the Cooldown section describe the `cooldown` mapping, the three modes,
  the formula, the bounds, the default, the precedence, and the error for
  the old key.
- `docs/architecture.md`: `CooldownPolicy` in `core/global_config.py`, and
  the new event fields.
- `AGENTS.md` and the phase README: Milestone 07's status.
- Phase 2 changelog: an entry when the milestone lands, with a **Change**
  entry for the new default.

## Acceptance criteria

- With global `cooldown: {mode: auto, minimum_seconds: 300}`, a run that took
  1200 s is followed by a 600 s wait, one that took 200 s by a 300 s wait,
  one that took 1201 s by a 601 s wait, and one that took 10000 s by a
  3600 s wait. With `ratio: 0.25`, a 1200 s run is followed by 300 s.
- With `mode: manual`, jobs wait exactly as they do today with the same
  number in `cooldown_seconds`.
- With `mode: off`, or manual `seconds: 0`, no job waits.
- With no `cooldown` in either file, jobs wait as `auto` with its defaults.
- A job's `cooldown` replaces the global one.
- `cooldown_seconds` in the global configuration or a job file fails with
  exit code 2 and the message above.
- Each invalid mapping in the validation list fails with exit code 2, naming
  the key.
- No cooldown follows the last run or a failed run, in any mode.
- `validate-job`, `run-job --dry-run`, the log, the manifest, the history,
  and the TUI show the mode and each wait as in the table above.
- Ctrl-C and `/cancel` end an auto wait at once, as they do a manual one.
- `config/global-config.yaml` is not edited by the app, tests, or tools.
- `make check` passes.

## Tests

- `tests/core/test_global_config.py`: the three modes, the defaults, the
  default policy when the key is missing, every rejected mapping, `off` read
  as a boolean, the old key's message, and `wait_after` at, below, and above
  each bound, with rounding up and a non-default ratio.
- `tests/jobs/test_job_definition.py`: the job mapping, the replacement of the
  global one, `off`, and the old key's message.
- `tests/jobs/test_job_service.py`: auto waits computed from each run's time
  with the injected cooldown, no wait after the last or a failed run, the
  manifest's `cooldown`, and `CooldownStarted`'s new fields.
- `tests/cli/test_job_output.py`: the `validate-job` and `--dry-run` lines for
  each mode.
- `tests/state/`: `settings.cooldown` recorded and imported; old rows and
  manifests still read.
- `tests/tui/`: the confirmation, the cooldown messages, and the history
  detail for `auto` and `off`.
- Every test that sets `cooldown_seconds` today switches to the mapping.
  Tests that set no cooldown now get `auto`: a fake run of a fraction of a
  second rounds up to a 1-second wait through the injected cooldown, so
  tests that count cooldown events set `mode: off` where they need none.
