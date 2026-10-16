# Milestone 12: Timing knobs as external files

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 11](milestone-11-time-estimation.md) landing: its `fit.py`, `forecast.py`, `forecast_publisher.py`, `timing_history.py`, and replay harness, which this milestone reads and then refactors. Also [Milestone 10](milestone-10-stage-timing.md) (done): the `run_stages` and `run_timing` rows, whose runs are what the knobs are learned from.

## Goal

Move every number the completion-time forecast depends on out of the code and into files under `data/timing-data-analysis/`, keep them current automatically from the runs the host completes, and use factory defaults wherever a file or a value is missing. The numbers are of two kinds. **Tunables** are the estimator's decisions (the pause factor, how fast old evidence fades, how widely an unseen setting widens the range). **Calibration** is what the host measured (how step time grows with width, height, frames, and each run knob, per backend and pipeline; stage durations; trends; spreads). Both depend on the backend, on each run's knobs, and on the output's width and height, and both are written and rewritten by Framecontrol, with the owner's hand edits kept.

## Evidence

**What Milestone 11 plans, and where it falls short for this.**

- Its thresholds are "provisional constants in `fit.py`" and in `features.py`, so a change is a code change and a release, and nothing records what was tried.
- It fits on the retained window of 14 days by default, and pruning deletes the rows with their execution. What one host learned about its own H3 and Draw Things settings is forgotten every two weeks, and the real evidence behind [Milestone 10](milestone-10-stage-timing.md#evidence) is already nearly gone ([Milestone 11's decisions](milestone-11-time-estimation.md#decisions-for-the-owner)).
- The step time grows with size, frames, and the run's knobs, and Milestone 11 fits only log pixels, log frames, a reference count, and the knobs that vary in the retained window. A knob the window never varied is not extrapolated, so the forecast drops to `live_only` the first time a known knob takes another value, although the same host may have timed it a month before.
- With no qualifying history it gives no number until the iterative stage has two readings. A first run on a fresh host, or after a pruned fortnight, shows `estimating` for the first several minutes.

**What a run records, and so what the knobs can depend on** (`timing_workload` in the backends):

| Backend | Workload keys |
|---------|---------------|
| H3 | `pipeline`, `width`, `height`, `pixels`, `frames`, `steps`, `seconds`, `references` (by kind) and `reference_count`, `render_width`, `render_height`, `denoise_reuse`, `dit_layers`, `core_reuse`, `token_reduction`, `ssd_streaming`, `int8_row_fc2`, `reference_rope`, `reference_image_size`, plus `color_corrects` or nondefault `compatibility` where applicable |
| Draw Things | `width`, `height`, `pixels`, `frames`, `steps`, `mode`, `guidance_scale`, `strength`, `shift`, `color_corrects` where applicable, and the model in the environment |

The M10 evidence already shows the dependence this milestone is for: per-step time at 832×448 was about 21 s for 5 s clips, 45 s for 8 s, and 65 s for 10 s, so it grows faster than the clip length, and H3's setup, tail, and Framecontrol finishing change with color correction (1 to 2 s against 38 to 46 s).

[E0053's live Draw Things measurement](../research/time-estimation.md#evidence) adds a narrow, model-specific calibration point: two 384×768, 33-frame, 25-step `i2v` runs averaged 13.33 and 13.95 s per step, with about 8 s of setup, 5 s of child finishing, and 8 s of Framecontrol finishing with color correction. They are one execution, so they cannot establish a size or frame-count slope or the between-execution spread. Its stored workload says 17 frames and 40 steps because the old timing hook missed per-run CLI overrides; the output and child counter say 33 and 25. The fit and replay loader now reconcile this old row. The future ring import must use those reconciled features too.

## Approach

**Files** (all under `data/timing-data-analysis/`, which `.gitignore` already treats as personal data; `ProjectPaths` gains `timing_data`, so no module holds a path of its own):

| File | Holds | Written by |
|------|-------|------------|
| `tuning.yaml` | The tunables, shared by both backends | the owner and the tunables search |
| `draw-things-cli.yaml`, `h3.yaml` | One backend's calibration, with a section per pipeline | the updater and the owner |
| `.machine/ring.jsonl` | The newest 300 complete runs, as numbers only | the updater |
| `.machine/written.json` | What the updater last wrote to each file (a digest and the flattened values), and the keys the owner pinned | the updater |
| `.machine/updates.log`, `.machine/backups/`, `.machine/lock` | The outcome of every update, the newest 20 timestamped copies of each file, and the lock | the updater |

A file is complete when written and carries a `schema` version, so a later schema is a forward-only migration like the state store's. `knobs.example.yaml` is the factory content exported, checked in by the `!data/**/*example*` rule, and a test keeps it equal to the factory code.

**Factory defaults** are code: `jobs/timing/factory.py` builds a `Tunables` and a `Calibration` for each backend and pipeline, with every number commented with the line of the M10 evidence it comes from. The evidence fixes the stage durations, the H3 frames exponent (about 1.5 to 1.7 from the three clip lengths), the Draw Things step time and its 11% to 24% rise, and the Framecontrol finishing times. It does not vary the size, so the factory size exponent is the frames exponent, on the argument that step time follows the token count (pixels times frames), and it carries a wide prior spread to say so. The factory also records the settings its numbers were measured at (its baseline). Where no file exists, or a file lacks a key or holds an invalid value, the factory value is used for exactly that key, and the first update writes the files. A malformed file, an unknown key, or a value out of its bounds is reported once in the log and by `fc timing knobs check` and never raises into the worker or a front end.
The Draw Things factory must keep the earlier 40-step point and E0053's 25-step point tied to their model, size, frame count, and color policy; neither point justifies one backend-wide step time.
An E0053-specific factory point has been landed ahead of this milestone in `jobs/timing/factory.py` and is read by the current estimator only when no history tier qualifies. It prices that exact workload and executable build at 13.6710 s per later step, with a 0.99758 whole-stage factor, a 0.205 stage slope, and a broad 0.25 log spread; it does not create or load the planned knob files. This milestone will carry that measured point into its full calibration format and keep its exact-match guard until independent workloads establish scaling coefficients.
An [E0051/E0054 H3 factory point](../research/time-estimation.md#evidence) has also landed for the exact overridden `r2av` workload: 15 requested seconds became 362 effective frames, with 30 steps, three image references, and one executable build. It uses 138.6683 s per later step, a 0.99334 whole-stage factor, the observed denoise and FFmpeg stages, a first-gap ratio of 0.798, a 0.1685 step slope, and the same broad 0.25 log spread. The full calibration model must carry the effective frames and per-run override, not train this point as 360 frames or as a profile default.

**The model** (`jobs/timing/calibrate.py`, pure, `core` and `numpy` only). Per backend and pipeline, the step time of the iterative stage is `exp(b0 + b_pixels log pixels + b_frames log frames + b_aspect log aspect + b_render log render pixels + b_refs log(1 + references) + the effect of each run knob + the size offset)`:

- **Width and height.** A continuous power law over pixels and aspect applies to any size. Each exact `WxH` also gets an offset, the mean residual of its runs shrunk toward zero by `n / (n + k)` (`k` is the tunable `size_offset_shrinkage`), so a size used often sharpens and a new size follows the curve. H3's render size enters on its own. Aspect enters only where the data varies it, as a knob that never varies is not extrapolated.
- **Run knobs.** Each knob has one learned effect (a number as its log slope, a flag or a value as an indicator), and the file records the values each knob has been seen at. A value never seen and different from the factory baseline widens the range by `unseen_knob_inflation` and puts `unseen` in the forecast's basis, as an unknown phase does.
- **Other stages** (setup, decode, FFmpeg, Framecontrol finishing, including the color-correction one) carry their own log-linear fit on frames and pixels and the residual spread. The file also holds the within-stage trend (kept only where its interval excludes zero, as in M11), the first-gap ratio, the run and execution spreads, and a bounded top 40 of per-signature summaries (count, median stage times, last seen) so tier 1 of M11 survives pruning.

**Evidence kept.** Raw timing rows still follow `history_retention_days`. The ring is a second, bounded, numbers-only copy: each entry has the backend, pipeline, workload, environment (model name, executable size and modification time, load), and the stages with their durations and units, the marks thinned to at most 120. A key whitelist enforces that no prompt, path, or log text enters it. It is what the gate and the tunables search replay when the database has been pruned.

**The update** (`jobs/timing/calibrate.py` for the fit and `services/timing_knobs.py`, `services/knob_updater.py` for the files and the worker side). After each complete run, off the event loop and one update at a time (a run that finishes during one is coalesced into the next):

1. Take the lock, load the files, and detect pins (below).
2. Append the run to the ring, dropping a duplicate (execution, run) and the oldest beyond 300.
3. Fit a candidate calibration per backend and pipeline: ridge over the ring and the retained history, penalized toward the **current** calibration instead of toward zero, so what aged out of the ring persists through the prior. The prior's weight in pseudo-runs is at most `prior_weight_cap`, decays with `half_life_runs`, and is multiplied by `build_change_discount` for a backend whose executable changed (M11 only flags `build_changed`). The penalty is chosen by leave-one-execution-out error over M11's fixed grid. A pinned coefficient is held at its value and its term subtracted before the fit. A coefficient moves at most a clamped step per update (0.1 in the log), so one odd run cannot jerk a knob.
4. **Gate.** Replay the ring grouped into 10 folds by execution through M11's replay harness, scoring the current and the candidate knobs by M11's metrics. Accept only if the pooled median absolute error of the remaining time is not worse than the current's by more than its bootstrap standard error, the 80% range covers between 70% and 90% (or no further from 80% than the current), and every value is finite, within its bounds, and non-decreasing in pixels and frames. A rejection keeps the current files and writes the reason to `updates.log`.
5. Write each changed file atomically (a temporary file in the same directory, then rename), keep a timestamped backup, prune to the newest 20, and update `written.json`.

**The tunables search** runs in the same worker after every 20th new complete run, and only with at least 40 ring runs from at least 6 executions. It tries each unpinned searchable tunable one grid step either way, scores each by the same fold replay, and accepts one change per search, the best, if its error is lower by more than the bootstrap standard error and the coverage condition holds. The searchable tunables and their grids are provisional constants in `factory.py`, pinned by tests:

| Tunable | Factory | Searched | Meaning |
|---------|---------|----------|---------|
| `pause_factor` | 3.0 | yes (2 to 5) | A step this many times the median is a pause |
| `size_offset_shrinkage` | 3 | yes (1 to 10) | Runs a size needs before its offset is half trusted |
| `prior_weight_cap` | 12 | yes (4 to 40) | Most pseudo-runs the previous calibration weighs |
| `half_life_runs` | 60 | yes (20 to 200) | How fast old evidence fades |
| `unseen_knob_inflation` | 1.5 | yes (1.1 to 3) | Range widening for a knob value never seen |
| `rho` | 0.5 | yes, while still used (0.2 to 0.8) | Share of the speed factor common to one execution, until spreads are measured |
| `slope_level` | 0.95 | yes (0.9 to 0.99) | Interval a within-stage slope must exclude zero at |
| `factory_spread` | 0.25 | no | The wider run spread used from factory defaults |
| Tier counts (3 runs from 2 executions; 6 from 3), the 80% range, the 2 s publish interval, the 200 ms fit and 5 ms event budgets, the ring size, the backup count | as M11 | no | Safety floors and budgets. A hand edit changes them, the search never does |

**Hand edits** use digest detection, as the owner chose: a file is rewritten whole, and `written.json` keeps what was written. On the next load every value that differs from it is the owner's and is **pinned**: the updater keeps it, fits the remaining terms around it, and never searches it. Deleting a key reverts it to automatic. The `evidence` block and `updated_at` are read-only and an edit to them is ignored with a warning. PyYAML cannot keep comments through a rewrite, so a `notes:` block is preserved verbatim and the backups hold the earlier text.

**The forecast.** With no qualifying history, a forecast appears at once from the factory or the loaded calibration, with basis tier `factory` (the file's numbers only when no signature or fit qualifies), a range widened by `factory_spread`, and the live update taking over as steps arrive. The basis gains `knobs` (`factory`, `learned`, or `pinned` when a pin touched the figure), `knobs_updated_at`, and `unseen` knob names. This supersedes [Milestone 11's](milestone-11-time-estimation.md#approach) "no number until the iterative stage has 2 readings", which remains true only for a signature the factory and the files cannot price. The published object, the floor rule, and the publishing rule are otherwise as in Milestone 11.

Rejected: sufficient statistics (`XᵀX`, `Xᵀy` kept per backend: exact, but opaque, a second numeric format to keep in step with the YAML, and the gate still needs a ring); a YAML-only incremental update with no ring (nothing to replay); a per-key `pinned` flag in one file (PyYAML drops the owner's comments on every machine rewrite); a calibration sweep of deliberate runs (GPU hours for knowledge passive learning supplies).

## Scope

In scope:

- The knob model and factory in `jobs/timing/`: `knobs.py` (`Tunables`, `Calibration`, their validation and bounds), `factory.py`, `calibrate.py` (the candidate fit, size offsets, shrinkage, clamping), and `tune.py` (the search). They import only `core` and `numpy`.
- In `services/`: `timing_knobs.py` (load, validation, digests and pins, atomic writes, the lock, backups, the ring) and `knob_updater.py` (the worker-side hook, the gate, coalescing).
- `ProjectPaths.timing_data`, the `fc timing knobs` commands, and `knobs` in the forecast basis on the HTTP, gRPC, and MCP surfaces and in the TUI's and the CLI's forecast text.
- The refactoring of Milestone 11's landed code (planned change 2).
- The synthetic data set's second mode (truth set away from the factory) and the tests below.

Out of scope: queue-wide prediction; recommending knobs for a job; a calibration sweep of deliberate runs; thermal, GPU, or host-load modeling (the ring records the start load, so a later milestone can test it); a second host (one machine only, by owner decision); copying logs or per-run rows beyond the numbers the ring holds; changing generation.

## Planned changes

1. **Knob types and factory first.** `Tunables`, `Calibration`, and `factory.py`, with the research note's factory derivation written before any number is trusted. Nothing reads them yet.
2. **Refactor Milestone 11's landed code onto the seam.** Milestone 11 is left as written and is built with its constants in the code. When it has landed, a behavior-preserving step moves them behind the types: `PAUSE_FACTOR` and `SLOPE_MIN_GAPS` in `features.py`, the penalty grid, tier counts, prior spreads, `rho`, the pause rule, and the publishing constants in `fit.py`, `forecast.py`, and `forecast_publisher.py`, become fields of `Tunables`; `FittedModel` and `Forecast` take a `Tunables` and a `Calibration` as parameters (the factory pair by default), and the tier-1 summary, the backend fit, and the stage tables read from the `Calibration` as well as from the history. `timing_history.py` passes the pair in. Done first and alone: with the factory pair every M11 test passes unchanged except the constructor arguments, and the synthetic replay's published fields are byte-identical to before the refactor, which proves the move changed nothing. Only after that do the `factory` tier and the persisted calibration change behavior. Milestone 11's document is not edited, and its superseded statements are recorded in the changelog.
3. **The model.** `calibrate.py` with the size power law and per-size offsets, the knob effects with their observed values, the other stages' fits, shrinkage toward a prior, clamping, and pinned terms. Pure and tested on the synthetic data first.
4. **The file store.** `timing_knobs.py`: strict per-key validation with fallback, schema version, atomic write, lock, backups, the ring with its key whitelist, and digest and pin detection. A crash at any point leaves either the old file or the new one.
5. **The updater and the gate.** `knob_updater.py`: the worker-side hook on a complete run, one background update at a time, the fold replay, the accept rule, `updates.log`. The first update on a host with no files creates them from the factory folded with the retained history, as a one-time backfill.
6. **The tunables search.** `tune.py` and its cadence, guards, and one-step rule.
7. **Surfaces.** The `factory` tier and `knobs` basis fields through `forecast_publisher.py`, the HTTP snapshot, gRPC, and MCP, and the text the TUI and `fc queue add --wait` show (for example `estimating from factory defaults`). A change to the proto adds fields to the nested basis message and renumbers nothing.
8. **Commands.** `fc timing knobs show` (each effective value with its source: factory, learned, or pinned), `check`, `update` (also the way to run it with no server, taking the same lock), `tune`, `rollback [N]` (restores a backup and records it in `written.json`, so the restored values are not read as hand edits), `reset [backend]` (deletes a file so the factory applies, keeping the backups), and `export-defaults`.
9. **Documentation.** The user guide (what the knob files are and how to edit and pin), architecture (the new modules, the layering rule, `ProjectPaths.timing_data`), the phase changelog, the phase README, and `docs/research/time-estimation.md` (the factory derivation, each factory number's source, and the observed learning curve).

## Evaluation

- **Synthetic data, second mode.** The existing seeded generator (at least 2000 runs, at least 20 signatures, both backends) writes a truth whose step times, stage durations, and size and knob effects are set 30% away from the factory values, and varies what the model does not assume (a rebuilt executable, a heavy-tailed run factor, drift, a pause, a falling step time). Each test starts from no files, replays the runs in time order as the updater would see them, and measures at the checkpoints of M11.
- **Metrics:** M11's (median and 90th-percentile absolute percentage error of the remaining time, bias, 80% coverage, the time to the first number, the share moving back), taken at 0, 10, 25, 50, 75, and 90% of each run, by completed runs of the pipeline (0, 10, 40) and for a size seen 0, 1, and 5 times.
- **Baselines:** the factory knobs with no files, and the calibration with the size offsets and knob effects switched off, so each addition is measured.
- **Real data** is the owner's own host, measured once like M11's, and not gated.

## Acceptance criteria

CI, on the synthetic data and fixed scenarios (provisional, like M11's, and changed only through a changelog entry that records the measurement):

- With no files, every backend and pipeline gets a forecast at `RunStarted` with basis `factory`, and its 80% range covers the truth between 70% and 90% of the time at the first number. The factory error is reported, not gated.
- After 10 complete runs of a pipeline from at least 3 executions, the median absolute error of the run's remaining time at 0% elapsed is at most 20%, and after 40 runs at most 12%, and lower at 40 than at 10. Coverage stays between 70% and 90% at both.
- A size seen 5 times has a lower median error at 0% than the power law alone; a size never seen is priced by the power law with coverage still between 70% and 90%. A knob value never seen widens the range and is named in `unseen`; once seen it is learned, and the same value no longer widens it.
- A run 10 times slower than its prediction moves no coefficient by more than the clamp. A candidate worse than the current knobs by more than its noise, or that leaves the coverage band, is rejected, the files and their digests stay, and `updates.log` says why.
- Writes are atomic (a failure injected between the temporary file and the rename leaves the old file), concurrent updates serialize on the lock, rollback restores a backup, and only the newest 20 backups remain. A corrupt file, an unknown key, an out-of-bounds value, a file from a later schema, and a missing directory give the factory value for that key and never an exception in the worker or a front end.
- A value the test edits by hand is pinned across updates and the search and its terms are fitted around it; deleting the key unpins it; an edit to `evidence` is ignored with a warning.
- The tunables search runs only after 20 new runs and only with 40 runs from 6 executions, moves one tunable one grid step, never moves a pinned one or a safety floor, and accepts only on an improvement over its noise. A tunable's value always stays within its bounds.
- The ring holds at most 300 runs, holds no run twice, and a test asserts every key of an entry is on the whitelist (no prompt, path, or log text). Pruning the database does not remove what the files learned, and the forecast after pruning still uses them.
- After the refactoring step (planned change 2) and before anything else changes, every Milestone 11 test passes with the factory pair and the synthetic replay's published forecasts are identical to those before it.
- Loading the files takes at most 20 ms, an update (fit and gate) at most 3 s of CPU on a 300-run ring, and a forecast update at most 5 ms per event, as in M11. The update never delays the worker's event handling.
- `tests/test_architecture.py` fails if `tui/`, `cli/`, or `mcp_server/` imports `jobs/timing/knobs.py`, `factory.py`, `calibrate.py`, or `tune.py`, or `services/timing_knobs.py` or `knob_updater.py`; `cli/timing_app.py` imports only the service. No dependency is added, and `make check` passes.

Owner host validation, recorded in `docs/research/time-estimation.md` and not blocking completion: the learning curve on the real runs the host completes after the knobs go live (the first number's error with factory defaults, then after 10 and 40 runs), and one real H3 job and one real Draw Things job forecast from the files and compared with the actual end.

## Decisions for the owner

- **The knobs are both tunables and calibration** (owner decision, 2026-10-16). Tunables are decisions and calibration is measurement, and both are files the updater writes.
- **The files are the durable memory** (owner decision, 2026-10-16), with a bounded numbers-only ring beside them. This supersedes, for those numbers only, [Milestone 10's](milestone-10-stage-timing.md#decisions-for-the-owner) "timing data follows history retention" and its rejection of a separate store: raw rows, stages, and marks still follow `history_retention_days`, and the ring carries nothing a prompt, path, or log could.
- **One file per backend with the pipelines inside, plus one tuning file** (owner decision, 2026-10-16).
- **Automatic, replay-gated, with backup** (owner decision, 2026-10-16): an update is written only if a fold replay of the ring shows no regression, each write keeps a backup, and `rollback` restores one.
- **The tunables are searched too** (owner decision, 2026-10-16, against the recommendation to search calibration only). The risk is overfitting a few dozen runs, so the search runs rarely, needs at least 40 runs from 6 executions, moves one tunable one grid step, accepts only a gain over its noise, and never touches a safety floor. If a first measurement shows it chasing noise, the guards tighten through a changelog entry that records the measurement.
- **Hand edits are detected by digest** (owner decision, 2026-10-16). Rejected: layered override files, which keep comments and make pinning explicit, and a per-key flag in one file. The cost is that PyYAML drops comments on a machine rewrite, held down by the `notes` block and the backups.
- **Width and height as a power law plus per-size offsets** (owner decision, 2026-10-16).
- **A factory forecast at the first moment** (owner decision, 2026-10-16), with a wide range and the basis `factory`, superseding [Milestone 11's](milestone-11-time-estimation.md#approach) "no number until the iterative stage has 2 readings" where the factory or the files can price the run.
- **One machine** (owner decision, 2026-10-16). The knobs describe this host and nothing keys them by host. A second Mac would train and be forecast by the first's numbers.
- **The persistence engine is a ring with prior shrinkage** (design decision, 2026-10-16), over sufficient statistics and a YAML-only update.
- **Milestone 11 is left untouched** (owner decision, 2026-10-16). Its document and build stay as planned, and this milestone's planned change 2 refactors its landed code onto the `Tunables` and `Calibration` seam, behavior-preserving and verified by identical replay output before any behavior changes.
