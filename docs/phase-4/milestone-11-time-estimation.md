# Milestone 11: Job completion time estimation

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 10](milestone-10-stage-timing.md)'s stage timeline and per-run timing rows, and the existing cooldown policy.

## Goal

Replace the step-counter heuristics with one stage-aware forecast of the running job: the active run's seconds left, the job's seconds left, and a time-weighted share done, each with an honest range. The server computes the forecast once, and the TUI, `fc queue wait`, the HTTP API, gRPC, and MCP show it. Its accuracy is measured by replaying recorded runs against the current estimator, not asserted.

## Evidence

**What the TUI does now** (`src/framecontrol/tui/estimate.py`). Before the first step reading, a run is estimated from the last successful run of any job. At the first reading it uses that run's time per step, and from the second on the mean pace since the first reading. A job's later runs are priced at the mean full time of its finished runs, and waits come from the cooldown policy. Every front end that wants progress would have to repeat this, because the server publishes only the last `n/N` it saw.

**Where [Milestone 10's data](milestone-10-stage-timing.md#evidence) shows that failing:**

1. H3 phases are read as one counter, so an early `1/1` or `50/50` marks the run as finishing, and Draw Things' `40 / 40`, which begins its last step, is read as the end. The estimator uses no knowledge of what stage is running.
2. Run 1 is priced from the latest run of any job. Run times span 654 to 2400 s across H3 settings, and the per-step time grows faster than the clip length, so an unrelated run misleads.
3. Later runs are priced at the mean of earlier ones, although identical settings varied by up to 31% from one run to the next.
4. A constant pace since the first reading is biased when the step time moves. In 7 of 8 Draw Things runs it rose 11% to 24% from the first quarter of the steps to the last, so at a quarter of the way the steps still to come are underestimated by roughly 15%.
5. The time after the last step is known only from a previous run's tail, so run 1 assumes none. That is the last step and 11 s of decode for Draw Things, and 66 to 142 s of decode and FFmpeg for H3. Framecontrol's own finishing is 1 to 2 s, or 38 to 46 s with H3 color correction, and is known only through an earlier run's full time.
6. A single time is shown with no sense of its uncertainty.

## Approach

With the data one host produces in a retention window (a few runs a day), a small, regularized model with explicit uncertainty fits better than a flexible one. All of it is closed-form with `numpy`, which is already a dependency.

- **A run is a sum of stages.** The stage sequence is known from the backend and pipeline, so a stage still to come contributes a prediction and the current stage contributes what is left of it. A stage the model has not seen widens the range instead of shrinking the sum.
- **Iterative stages dominate** (denoise is 87% to 94% of an H3 run, sampling about 98% of a Draw Things run). Per-step time is modeled as `log t = f(workload) + u + noise`. `f` is a ridge-regularized least-squares fit on the logs of the retained history, over log pixels, log frames, pipeline, log of the reference count, and the knobs that vary in the data; the penalty is chosen at fit time by leave-one-execution-out error over a fixed grid and reported in the basis. `u` is this run's speed factor. Its prior spread is taken from the residuals between runs of one signature once at least three such pairs exist, and is 0.10 in the log until then (the eight same-settings `r2av` runs spanned about ±8%). A knob that never varies in the data is not extrapolated.
- **Live update.** After each step of the active stage, `u` is updated by the normal-normal conjugate rule on the log ratio of measured to predicted step time, so the weight of history falls as steps accumulate. A step over three times the median is treated as a pause: it counts toward elapsed time, not toward the rate. A per-backend within-stage trend, the slope of log step time against the step's place in the stage, is fitted from history and applied across the step index, so a rising step time is not extrapolated flat. The data shows why it must be fitted rather than assumed: Draw Things rose 11% to 24% in 7 of 8 runs, while H3's last third ran at 0.72 to 1.19 times its first with no consistent sign, so its slope is expected to fit near zero and is used only where it is distinguishable from zero.
- **Other stages** (setup, decode and FFmpeg, Framecontrol finishing) use the same-signature median when there are 3 or more samples, else a log-linear fit on frames and pixels, with the residual spread as their variance.
- **Fallback tiers**, the first that qualifies: the same signature with 3 or more samples; a fit within one backend and pipeline; a fit across pipelines with an indicator; and, with no usable history, no number until the iterative stage has 2 readings, then a live-only forecast with the pooled spread. A forecast always carries its basis (tier and sample count), and it is never invented.
- **The job.** Remaining time is the active run's, plus each later run's prediction (its signature, with the speed factor shrunk toward this execution's measured one by the between-run correlation, 0.5 until the data can fit it), plus Framecontrol's finishing per run, plus cooldown waits computed by the existing policy from the predicted run times. Run noise adds in quadrature and the shared speed factor adds coherently.
- **The range** is the 80% posterior predictive interval. The bar's share is time-weighted, `elapsed / (elapsed + median remaining)`, and never moves back within a run: it holds still when the forecast grows. The text may change freely.
- **Where it runs.** The queue worker already observes each run's events to keep its status. The forecast sits beside that, updates on every stage reading, and publishes the predicted end as an absolute time with its range, so a front end counts down from it with no model of its own. A client attached mid-run reads the latest forecast from the snapshot, and the completed spans from the stages endpoint.

Rejected: a constant pace (the present bias); the CLI's own percentage (its built-in prediction, not a measurement); an exponentially weighted pace (an arbitrary constant, no uncertainty); gradient boosting, neural, or full MCMC models (too few samples, and the conjugate update is exact for this model); any dependency beyond `numpy`; and running the model in each front end (duplicated logic, and a golden test just to keep the copies equal).

## Scope

In scope:

- The forecast and its fit, in a `jobs/timing/` model that imports only `core` and `numpy`, a `services` loader that reads the history from the store, and the worker-side publisher.
- The server's surfaces: a `ForecastUpdated` event, the HTTP queue entry and watch, gRPC `QueueEntrySnapshot`, and the MCP queue entry. The step fields `current_step` and `current_step_total` are removed from all of them.
- The display: the TUI's status line, bars, and Execution pane stage list, and `fc queue wait`.
- The replay harness and `fc timing replay`, with a frozen copy of the current estimator in `tests/` as its baseline.
- Removing the old estimate: `tui/estimate.py`, the step state `LiveRun` keeps for it, `RunOutput.progress` and `percent`, their source in `ProcessMessage`, and the shared `PROGRESS_PATTERN`.

Out of scope, by owner decision on 2026-10-15: a predicted start and end for queued entries. The forecast covers the running job only; queue-wide prediction can follow as its own milestone. Also out of scope: changing generation, recommending knobs, notifications, predicting failure, and a thermal or GPU model.

## Planned changes

1. **Baseline first.** Port the current estimator unchanged into the replay harness and record its error on the retained runs, including whether the phase hazard is real, before any estimator changes.
2. **Model and forecast.** `jobs/timing/` gains the fit, the posterior update, and a `Forecast` (stage, units done and total, and for the run and the job the share done, the predicted end, and the 80% range, plus the basis). It is a pure function of the stage events and the fitted model.
3. **Loader and publisher.** `services/` fits from `run_timing` and `run_stages` when a job starts and caches the fit by the newest complete run and the row count, since pruning changes the count. The worker feeds each stage event to the forecast and publishes the result. Any failure (an empty store, a corrupt row, an unknown phase) yields "no forecast", never an exception in the worker or a front end.
4. **Server and API.** The snapshot's `current_step` and `current_step_total` are replaced by a `forecast` object (`stage`, `stage_done`, `stage_total`, and for `run` and `job` a `progress`, `end_at`, `low`, and `high`, plus `basis`), which is null when there is no forecast. The same object travels in `ForecastUpdated`, the gRPC `QueueEntrySnapshot` (field numbers 8 and 9 are reserved), and MCP. There is no optional-field tolerance, and a client and a server are expected to be the same version.
5. **TUI and CLI.** The front ends render the forecast and hold no model: `tui/estimate.py` is deleted, and the old step state, `RunOutput.progress` and `percent`, and `PROGRESS_PATTERN` go with it (the reseed in `tui/feed.py` reads the snapshot's forecast instead). The status line reads, for example, `denoise 12/30, ends ~16:42, 16:31 to 16:58`. The range is shown when its half-width is over 3% of the time left, and `estimating` stays when there is no forecast. The Execution pane lists each stage with its units, time, and share. `fc queue wait` prints the same stage and end. New terms go in [ux-terms.md](../ux-terms.md).
6. **Replay.** `fc timing replay [--backend] [--estimator naive|last-signature|new]` feeds the retained runs through an estimator as a live stream and prints the metrics below. The frozen current estimator is in `tests/`, not in the command.
7. **Documentation.** The user guide (what the bars and the range mean), architecture, the HTTP and MCP guide, the phase changelog, and `docs/research/time-estimation.md` with the baseline, the replay results, and the host validation.

## Evaluation

- **Checkpoints:** 5%, 10%, 25%, 50%, 75%, and 90% of each run's true duration.
- **Metrics:** the median and 90th-percentile absolute percentage error of the remaining time, its signed bias, the coverage of the 80% range, the time to the first number, and the number of times the bar moves back.
- **Cross-validation:** leave one execution out. Runs of one execution share a machine state, so a fit never includes the execution it is scored on.
- **Baselines:** the frozen current estimator, a constant-pace estimator, and "the last run of the same signature".
- **Two data sets, kept apart.** The first is synthetic: a seeded generator writes runs for both backends from the spreads recorded in [Milestone 10's evidence](milestone-10-stage-timing.md#evidence) (a log-linear step time in pixels and frames, a per-run speed factor, a rising step time for Draw Things and a flat one for H3, occasional long pauses, constant setup and tail stages), at least 2000 runs over at least 20 signatures. It runs in `make check` and tests the algorithm against a known truth. The second is the real history the owner's database holds when the milestone is validated. It is small (14 days by default) and changes under the test, so it is measured once, on the owner host, and reported with each cell's run count and a bootstrap interval; a cell with fewer than 8 runs is listed and not judged. Coverage is judged pooled over the checkpoints from 10% on.

## Acceptance criteria

CI, on the synthetic data set and fixed scenarios:

- On runs that have at least 3 other same-signature runs, the median absolute error of the remaining time is at most 15% at 10% elapsed, 5% at 50%, and 10% at 90%. The 80% range covers the truth between 70% and 90% of the time at each checkpoint from 10% on.
- At every checkpoint where the frozen current estimator gives a number, the new median error is lower, and it is never more than 2 points worse for any backend, pipeline, or checkpoint. The bar never moves back within a run.
- The first number appears no later than the current estimator's, and, for a signature with 3 or more samples, before the first step reading.
- Scenario tests with synthetic readings: an early H3 `tokenizer 1/1` or `text encoder 50/50` does not read as finishing; Draw Things' `40 / 40` does not read as the end; a pause counts toward elapsed time and not toward the rate; each fallback tier is chosen, and reported in the basis, for the history that qualifies for it.
- The fit of 5000 rows takes at most 200 ms, and a forecast update at most 5 ms per event. Failure injection (an empty store, a corrupt row, an unknown phase, a run with no history) never raises into the worker or a front end.
- One recorded event stream yields the same forecast fields from the HTTP snapshot, the gRPC snapshot, and the MCP entry, and the TUI and `fc queue wait` render a given forecast to the expected text. `tests/test_architecture.py` fails if `tui/`, `cli/`, or `mcp_server/` imports the model, so no front end can grow a second forecast.
- Layering holds, no dependency is added, and `make check` passes.

Owner host validation, recorded in `docs/research/time-estimation.md` and not blocking completion: the same accuracy targets measured on the real retained runs (error, coverage, and the comparison with the frozen estimator, with run counts and intervals), and one real Draw Things job and one real H3 job on the new forecast compared with the actual end, as Milestone 06 does.

## Decisions for the owner

- **The server computes the forecast once** (owner decision, 2026-10-16). Front ends show the published end and range, and count down from the end. Rejected: each front end running the model on the stage events, which duplicates the logic and needs a golden test to keep the copies equal, although the TUI already needs `fc serve`.
- **Real accuracy is measured once, not gated** (follows from the 2026-10-16 decisions in [Milestone 10](milestone-10-stage-timing.md#decisions-for-the-owner)). Timing rows are pruned with their executions and the logs behind the evidence are not kept, so no real data set stays fixed enough to gate `make check`. The synthetic data set gates the algorithm, and the owner-host measurement checks it against reality.
- **The targets are provisional.** They come from the data's own spread: eight `r2av` runs of one size, frame count, and step count (E0040 run 1, E0041 to E0047) took 2055 to 2396 s, about ±8%, and the live rate removes most of the denoise share of that after a few steps. If the first measurement on real runs shows a target unreachable, it changes only through a changelog entry that records the measurement, not silently.
- **A range, not only a time.** The spread between identical runs makes a single time look more certain than it is. Rejected: hiding the range (the old behavior), and always showing it (noise on a steady run).
- **`current_step` is removed, not repurposed** (owner decision, 2026-10-16: backward compatibility may be broken; this supersedes the 2026-10-15 decision to keep the names with a new meaning). The old value was wrong for H3, and an older client reading a stage's counter as a global one would be wrong in a new way, so the fields go and `forecast.stage`, `stage_done`, and `stage_total` replace them.
- **The current estimator's heuristics are removed from the product** in this milestone, with the generic progress parsing they read. A frozen copy, adapted to the replay stream, stays in `tests/` as the baseline.
