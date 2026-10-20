# Milestone 15: Held-out gate and tunables search

**Phase:** [Phase 4](README.md)
**Status:** in-progress
**Depends on:** [Milestone 14](milestone-14-timing-knobs.md): the knob files, the ring, the provisional checkpoints, pins, backups, and `fc timing knobs`. Milestone 14 freezes a group's calibration after its 32nd run; this milestone lets later runs change it again, but only through a gate.

The current implementation partitions the ring by execution, compares workload-adjusted step-rate errors on held-out executions, judges the live and candidate calibrations independently, records the latest gate for `check`, tracks edits to tunables as pins, and strictly validates and journals approved baselines with the knob files. `fc timing knobs tune` currently runs that same guarded updater on demand. Full remaining-time replay, coverage gating, and tunables selection described below remain to be implemented before this milestone is done.

## Goal

Let the calibration keep learning after the provisional checkpoints, and let the tunables be searched, with every change validated on executions the candidate never saw. Provisional values are measured against the factory baseline; a validated change is measured against the last approved calibration.

## Approach

**Files.** Adds `.machine/validated.json`: per backend and pipeline, the last held-out-approved calibration, plus the searched tunables of the last approval, each with `approved_at` and the held-out counts that approved it. It carries a `schema` and is validated, backed up, journaled and recovered like Milestone 14's other files (the transaction's prior file set includes it, so `rollback [N]` restores it with the rest and `reset [backend]` drops that backend's section). Until the first approval the validated baseline is the factory defaults. The file is machine-only: the owner's pins live in the knob files and are overlaid on any baseline or candidate, so a restore never discards a pin. `tuning.yaml` (written by the search) and `validated.json` join `written.json`'s digests, so a machine write of either is never read as a hand edit.

**The validated update** runs in Milestone 14's updater, off the event path at low priority, after a complete run once a group is past its 32-run checkpoint:

1. **Trigger.** Per backend and pipeline, an attempt starts only when the group is past its 32nd complete run and at least three independent executions have arrived since the previous attempt's holdout (since the 32nd run, for the first attempt). A run that does not reach that count only enters the ring. This bounds how often one stream of data is looked at, because testing after every run would approve noise by repetition.
2. **Partition** chronologically by execution id. The holdout is the newest `max(3, ceil(0.2 × independent executions))` executions of the group, capped at 8, and **every one is later than the execution of the group's 32nd run** (so later than the last provisional fit; no new `written.json` field is needed, and a group already past 32 when this lands qualifies). Never use any of their runs, marks, or retained-history duplicates to fit a candidate, choose its penalty, or set its spread. Require at least three earlier independent executions for training. Otherwise record `insufficient_evidence` and leave the effective knobs alone.
3. Fit a candidate calibration from the older ring and retained-history runs: ridge penalized toward the last validated calibration (or factory defaults) instead of toward zero, so accepted knowledge that aged out of the ring persists through the prior. Cap the prior by `prior_weight_cap`, decay it with `half_life_runs`, and discount it on a changed executable build. Choose the penalty by leave-one-execution-out on the training partition only. Hold pinned coefficients fixed and subtract their terms before fitting. Clamp each coefficient's movement per update (0.1 in the log). Do not fit a term that lacks independent variation or an identifiable baseline.
4. **Gate.** Replay the validated baseline, the candidate, and (when it differs from the baseline) the live calibration the files now hold, on the same reserved executions, at M11's checkpoints, with none trained on them; apply the same current owner pins to all. For each comparison take one paired difference per held-out execution (the mean over its runs and checkpoints), and bootstrap those differences with a generator seeded from the held-out execution ids, so the same data always gives the same verdict. A paired comparison of fewer than five executions cannot be called clear in either direction: with three, a bootstrap has ten distinct resamples. It is `inconclusive` unless the sign test on the executions is unanimous and the effect is larger than the stated margin. The verdict for the candidate against the baseline, on the 90% bootstrap interval of its median remaining-time error difference:
   - **promote** if the upper bound is no worse than the margin (0.01 of the error, an absolute percentage point), its 80% coverage is no further from 80% than the baseline's beyond the same kind of interval, and its values are finite, within bounds, and respect justified monotonic constraints;
   - **reject** if the lower bound exceeds zero (a clear regression) or its values are invalid: nothing is written and `updates.log` says why;
   - otherwise **inconclusive**: the current values stay, labeled as they were, and a later attempt retries on later executions.
   The live calibration is judged the same way against the baseline. A clear regression of the live calibration restores the validated baseline (the factory before the first approval) into the files with the owner's pins overlaid; it never restores to something that was not approved. This is how a bad provisional calibration is undone. The log records counts, error and coverage for each version, the interval, and the reason. A 10-fold replay over all ring runs is useful for offline diagnosis but cannot approve a candidate trained on those same runs.
5. **Write** through Milestone 14's transaction: the prior file set includes `.machine/validated.json` when the approved baseline changes, and a promotion writes the knob files and `validated.json` together or not at all.

A source `learned` is added beside `KNOBS_FACTORY`, `KNOBS_PROVISIONAL`, and `KNOBS_PINNED` in `knobs.py` for a value a validated update produced; `pinned` still wins where a pin touches the figure. The basis field needs no new proto field, and the CLI and TUI text (`from provisional calibration` today) gains `from validated calibration`.

**The tunables search** is attempted in the same background updater after every 20 new complete runs counted across all groups since the last attempt (kept in `written.json`; an attempt, accepted or not, resets it), only when at least one group has 40 ring runs from six independent executions, three for training and three reserved for holdout. A group below that is left out of the scoring. Because `tuning.yaml` is shared, the search scores every group that qualifies, rather than optimizing the group that happened to finish the last run. It tries each unpinned searched tunable one grid step either way (the neighbouring grid values, with the pinned and safety-floor ones skipped), refitting calibration on training evidence for each candidate where the tunable affects fitting. **Selection** uses training executions only, by leave-one-execution-out over them. The one selected candidate is then replayed against the current tunables on the later reserved executions with the gate's paired bootstrap and verdict rules (execution-level pairing, at least five held-out executions in total to call it clear, a seeded generator); it is accepted, at most one change per search, only when the verdict is promote with an improvement (its upper bound below zero), coverage is no worse beyond the interval, and no scored group with three or more held-out executions has a clear regression. The search never uses holdout results to choose among grid choices. An inconclusive search leaves every value alone. An accepted change writes `tuning.yaml`, the refit calibrations, and `validated.json` in one transaction; later gate attempts use the new tunables, and a clear gate regression restores the tunables of the last approval along with the calibration. The search is capped at 10 s of CPU at low priority and abandons its work, changing nothing, if the process stops or the queue starts generating. The grids are provisional constants in `factory.py` (named `SEARCH_GRIDS`, separate from `knobs.SEARCHED`, which only says which keys `tuning.yaml` lists under `tunables:`), every point inside `BOUNDS`, pinned by tests:

| Tunable | Factory | Grid (neighbouring points are the steps) |
|---------|---------|----------|
| `pause_factor` | 3.0 | 2, 2.5, 3, 4, 5 |
| `size_offset_shrinkage` | 3 | 1, 2, 3, 5, 10 |
| `prior_weight_cap` | 12 | 4, 8, 12, 20, 40 |
| `half_life_runs` | 60 | 20, 40, 60, 100, 200 |
| `unseen_knob_inflation` | 1.5 | 1.1, 1.25, 1.5, 2, 3 |
| `rho` | 0.5 | 0.2, 0.35, 0.5, 0.65, 0.8; searched only while the model still uses it (until spreads are measured), and skipped otherwise |
| `slope_level` | 0.95 | 0.80, 0.95: the only values `SLOPE_LEVELS` supports, so its one step is a switch (the earlier 0.9 to 0.99 range was outside the bounds) |
| `factory_spread`, tier counts, the 80% range, the publish interval, the fit and event budgets, the ring size, the backup count | as M14 | not searched: safety floors and budgets (`factory_spread` is listed under `tunables:` in `tuning.yaml` but is a floor for the factory's own spread, which learning replaces) |

## Scope

In scope: `jobs/timing/tune.py` (the search; pure, `core` and `numpy` only, added to `FORECAST_MODULES` in `tests/test_architecture.py`), the held-out replay and paired bootstrap in a new pure `jobs/timing/gate.py` (so `calibrate.py` stays a fit), the orchestration in `services/knob_updater.py`; `.machine/validated.json` and its handling in `timing_knob_files.py`, `reset`, and `rollback`; `fc timing knobs tune` (runs one search now, under the same lock and eligibility rules as `update`, and says what it did), and the last gate and search results in `check`, `show`, and `updates.log`; the `learned` source in the basis on every surface; the user guide, architecture, changelog, and research note.

Out of scope: everything Milestone 14 excludes.

## Planned changes

1. **The gate.** `gate.py`: the partition, the seeded paired bootstrap and the three verdicts, then `validated.json`, its transaction, and the restore rule in the updater. Tested on the synthetic data first, including a candidate trained to be worse.
2. **The tunables search.** `tune.py`, `SEARCH_GRIDS`, its cadence, guards, and one-step rule. Search choices are made on training evidence before the holdout is opened.
3. **Surfaces and commands.** `learned` in the basis and in `show` (with the baseline each value was approved against); `fc timing knobs tune`; the last gate and search results in `check`; `reset` and `rollback` covering `validated.json`.
4. **Documentation.**

## Evaluation

The synthetic second mode of Milestone 14, continued past the 32-run checkpoint, with M14's metrics and baselines plus the provisional-only calibration, so the gate's gain is measured. Include a stream where a provisional calibration is worse than the factory, so the restore path is measured, and one with no true change, so the false-accept rate of the gate and the search is measured. Report how often the gate accepts, rejects, and stays inconclusive, and whether the search moved a tunable and what it gained on held-out executions. No accuracy threshold gates CI or completion.

## Acceptance criteria

- Past a group's 32nd run, a validated candidate with a clear held-out regression or invalid values is rejected; an inconclusive comparison makes no validated change. The ring retains the run, the files and digests stay, and `updates.log` explains why. A clear regression restores the validated baseline.
- Holdout executions are later than the group's 32nd run, are never used to fit, choose a penalty, or set a spread, and are the same for baseline, candidate, and live calibration; a test asserts the partition. Fewer than three training or holdout executions gives `insufficient_evidence`, fewer than five held-out executions never gives a clear verdict, and the same data gives the same verdict twice.
- A promotion writes the knob files and `validated.json` as one transaction; a crash at every write boundary recovers or rolls back as Milestone 14's does, and `rollback` and `reset` restore or drop `validated.json` with the rest. A machine write of `tuning.yaml` or `validated.json` is never pinned as a hand edit.
- The search is attempted only after 20 new runs and only with 40 runs from 6 executions, moves one tunable one grid step, never moves a pinned one or a safety floor, and validates the chosen step on later executions excluded from selection. A tunable's value always stays within its bounds, and a test asserts every grid point is inside `BOUNDS`, `slope_level` included.
- A pinned coefficient or tunable is held through the gate and the search.
- Benchmark a 300-run fit and held-out replay against a 3 s CPU target, and a full search against 10 s; report the measured values rather than weakening the replay. The updater still never delays the worker's event path, and a search stops when a run starts.
- `tests/test_architecture.py` also forbids `tui/`, `cli/`, and `mcp_server/` from importing `jobs/timing/tune.py` and `jobs/timing/gate.py`. No dependency is added, and `make check` passes.

Owner host validation, recorded in `docs/research/time-estimation.md` and not blocking completion: the learning curve after validated updates on the real runs the host completes.

## Decisions for the owner

- **Automatic updates with a held-out gate** (owner decision, 2026-10-16): carried from [Milestone 14](milestone-14-timing-knobs.md#decisions-for-the-owner), which keeps the provisional checkpoints.
- **The tunables are searched too** (owner decision, 2026-10-16, against the recommendation to search calibration only), and it stays on by default (owner decision, 2026-10-18). The risk is overfitting a few dozen runs, so the search runs rarely, needs at least 40 runs from 6 executions, moves one tunable one grid step, accepts only a gain over its noise, and never touches a safety floor. If a first measurement shows it chasing noise, the guards tighten through a changelog entry that records the measurement.
- **Split from Milestone 14** (owner decision, 2026-10-18), so the riskier statistics get their own evidence after the files and provisional learning are in use.
