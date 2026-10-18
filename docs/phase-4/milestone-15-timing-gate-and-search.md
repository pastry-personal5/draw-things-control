# Milestone 15: Held-out gate and tunables search

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 14](milestone-14-timing-knobs.md): the knob files, the ring, the provisional checkpoints, pins, backups, and `fc timing knobs`. Milestone 14 freezes a group's calibration after its 32nd run; this milestone lets later runs change it again, but only through a gate.

## Goal

Let the calibration keep learning after the provisional checkpoints, and let the tunables be searched, with every change validated on executions the candidate never saw. Provisional values are measured against the factory baseline; a validated change is measured against the last approved calibration.

## Approach

**Files.** Adds `.machine/validated.json`: the last held-out-approved calibration and tunables, restored after a regression. Until the first approval the validated baseline is the factory defaults.

**The validated update** runs in Milestone 14's updater, off the event path at low priority, after a complete run once a group is past its 32-run checkpoint:

1. Partition evidence chronologically by execution. Reserve the newest three independent executions of the affected backend and pipeline, **all later than the last provisional fit**, as holdout. Never use any of their runs, marks, or retained-history duplicates to fit a candidate, choose its penalty, or set its spread. Require at least three earlier independent executions for training. Otherwise record `insufficient_evidence` and leave the effective knobs alone.
2. Fit a candidate calibration from the older ring and retained-history runs: ridge penalized toward the last validated calibration (or factory defaults) instead of toward zero, so accepted knowledge that aged out of the ring persists through the prior. Cap the prior by `prior_weight_cap`, decay it with `half_life_runs`, and discount it on a changed executable build. Choose the penalty by leave-one-execution-out on the training partition only. Hold pinned coefficients fixed and subtract their terms before fitting. Clamp each coefficient's movement per update (0.1 in the log). Do not fit a term that lacks independent variation or an identifiable baseline.
3. **Gate.** Replay the validated baseline and candidate on the same reserved executions, at M11's checkpoints, with neither version trained on them; apply the same current owner pins to both. Bootstrap paired differences by execution, not by step or run. Promote the candidate to validated only if its median remaining-time error is no worse beyond the uncertainty of that paired comparison, its 80% coverage is no further from 80% than the baseline's beyond that uncertainty, and its values are finite, bounded, and respect justified monotonic constraints. An inconclusive comparison keeps the current values, still labeled as they were, and retries as later executions arrive; a clear regression restores the validated baseline with the owner's pins overlaid. The log records counts, error and coverage for both versions, uncertainty, and reason. A 10-fold replay over all ring runs is useful for offline diagnosis but cannot approve a candidate trained on those same runs.
4. Write through Milestone 14's transaction: the prior file set includes `.machine/validated.json` when the approved baseline changes.

A source `learned` is added to the forecast basis for a value a validated update produced.

**The tunables search** is attempted in the same background updater after every 20 new complete runs, only with at least 40 ring runs and six independent executions overall, three for training and three reserved for holdout. Because `tuning.yaml` is shared, the search scores all backend and pipeline groups with evidence, rather than optimizing the group that happened to finish the last run. It tries each unpinned searchable tunable one grid step either way, refitting calibration on training evidence for each candidate where the tunable affects fitting. It chooses one candidate using training executions, replays that candidate on later reserved executions, and accepts at most one change per search only when the paired improvement clears its uncertainty, coverage is no worse beyond uncertainty, and no group with enough held-out evidence has a clear regression. The search never uses holdout results to choose among grid choices. Inconclusive searches leave every value alone; the cadence counts new runs since the last attempt, not since the last acceptance. The grids are provisional constants in `factory.py`, pinned by tests:

| Tunable | Factory | Searched |
|---------|---------|----------|
| `pause_factor` | 3.0 | yes (2 to 5) |
| `size_offset_shrinkage` | 3 | yes (1 to 10) |
| `prior_weight_cap` | 12 | yes (4 to 40) |
| `half_life_runs` | 60 | yes (20 to 200) |
| `unseen_knob_inflation` | 1.5 | yes (1.1 to 3) |
| `rho` | 0.5 | yes, while still used (0.2 to 0.8) |
| `slope_level` | 0.95 | yes (0.9 to 0.99) |
| `factory_spread`, tier counts, the 80% range, the publish interval, the fit and event budgets, the ring size, the backup count | as M14 | no: safety floors and budgets |

## Scope

In scope: `tune.py` (the search) and the gate in `jobs/timing/calibrate.py` and `services/knob_updater.py`; `.machine/validated.json`; `fc timing knobs tune`, and the gate result in `check`, `show`, and `updates.log`; the `learned` source in the basis on every surface; the user guide, architecture, changelog, and research note.

Out of scope: everything Milestone 14 excludes.

## Planned changes

1. **The gate.** The held-out partition, the paired bootstrap, the accept and restore rules, `validated.json`, and its transaction. Tested on the synthetic data first.
2. **The tunables search.** `tune.py`, its cadence, guards, and one-step rule. Search choices are made on training evidence before the holdout is opened.
3. **Surfaces and commands.** `learned` in the basis and in `show`; `fc timing knobs tune`; the last gate result in `check`.
4. **Documentation.**

## Evaluation

The synthetic second mode of Milestone 14, continued past the 32-run checkpoint, with M14's metrics and baselines plus the provisional-only calibration, so the gate's gain is measured. Report how often the gate accepts, rejects, and stays inconclusive, and whether the search moved a tunable and what it gained on held-out executions. No accuracy threshold gates CI or completion.

## Acceptance criteria

- Past a group's 32nd run, a validated candidate with a clear held-out regression or invalid values is rejected; an inconclusive comparison makes no validated change. The ring retains the run, the files and digests stay, and `updates.log` explains why. A clear regression restores the validated baseline.
- Holdout executions are later than the last provisional fit, are never used to fit, choose a penalty, or set a spread, and are the same for baseline and candidate; a test asserts the partition.
- The search is attempted only after 20 new runs and only with 40 runs from 6 executions, moves one tunable one grid step, never moves a pinned one or a safety floor, and validates the chosen step on later executions excluded from selection. A tunable's value always stays within its bounds.
- A pinned coefficient or tunable is held through the gate and the search.
- Benchmark a 300-run fit and held-out replay against a 3 s CPU target; report the measured value rather than weakening the replay. The updater still never delays the worker's event path.
- `tests/test_architecture.py` also forbids `tui/`, `cli/`, and `mcp_server/` from importing `jobs/timing/tune.py`. No dependency is added, and `make check` passes.

Owner host validation, recorded in `docs/research/time-estimation.md` and not blocking completion: the learning curve after validated updates on the real runs the host completes.

## Decisions for the owner

- **Automatic updates with a held-out gate** (owner decision, 2026-10-16): carried from [Milestone 14](milestone-14-timing-knobs.md#decisions-for-the-owner), which keeps the provisional checkpoints.
- **The tunables are searched too** (owner decision, 2026-10-16, against the recommendation to search calibration only), and it stays on by default (owner decision, 2026-10-18). The risk is overfitting a few dozen runs, so the search runs rarely, needs at least 40 runs from 6 executions, moves one tunable one grid step, accepts only a gain over its noise, and never touches a safety floor. If a first measurement shows it chasing noise, the guards tighten through a changelog entry that records the measurement.
- **Split from Milestone 14** (owner decision, 2026-10-18), so the riskier statistics get their own evidence after the files and provisional learning are in use.
