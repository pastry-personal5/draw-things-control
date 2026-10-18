# Time estimation: stage timing evidence and host validation

The measurements behind [Milestone 10](../phase-4/milestone-10-stage-timing.md) (how long each stage of a run
took) and [Milestone 11](../phase-4/milestone-11-time-estimation.md) (the forecast fitted on them), and the owner-host
checks of the recorder. The logs these numbers were read from expire with their executions (14 days by default) and, by
owner decision, no copy is kept: this note and the two milestones are the record. Checked on 2026-10-16 before E0053 ran, the logs of E0028 to E0047 were already gone (deleted with their output folders, not by retention); E0026 and E0027, until 2026-10-19 and 2026-10-20, and E0048 to E0050 still had one. E0053's log and live timing rows were inspected later that day. The tests use synthetic transcripts
written from the line formats below.

## Evidence

Measured on 2026-10-15 by reading the job logs of 22 executions (E0026 to E0047), read-only. 44 runs have a child
transcript: 8 Draw Things `i2v` (E0026 and E0027) and 36 H3 (`i2av`, `r2av`, and mixed chains); one of them (E0038 run 3)
was interrupted. E0028 and E0029 failed within a second, and the logs of E0031 and E0032 no longer existed.

**H3 stages.** Each line is `<phase> n/N` and `n` is the number done. One 243-frame `r2av` run (E0047, 2310 s) went through
`tokenizer`, `video VAE encoder`, `Qwen vision`, `text encoder`, `refine text`, `precompute AdaLN`,
`load transformer core`, `denoise enqueue` with `denoise`, `audio VAE`, `video VAE load`, and `FFmpeg` (counted in frames).
The 35 runs that finished denoising:

| Stage group | Share of the run | Observed |
|-------------|------------------|----------|
| Setup, up to the last line of `load transformer core` | 1% to 3% | 17.5 to 26.6 s |
| Denoise (30 steps), up to the last `denoise 30/30` | 87% to 94% | 18.9 to 74.8 s per step |
| Audio VAE, video VAE load, and FFmpeg after the last step | 6% to 10% | 66 to 142 s |
| Framecontrol finishing after the child exits | under 3% | 1 to 2 s without color correction, 38 to 46 s with it |

Nothing prints between `load transformer core 50/50` and the first `denoise enqueue` reading, and the first gap between
denoise readings is a median 0.74 times a later one. Within a run the last third of the steps ran at 0.72 to 1.19 times the
pace of the first third (24 of 35 runs within 0.9 to 1.1). Between runs of the same settings the pace varies more: eight
243-frame 832×448 `r2av` runs took 63.5 to 74.3 s per step, and three runs of an 8 s `i2av` chain were 16% to 31% slower
than their siblings. The per-step time grows faster than the clip length: 5 s clips took 19 to 24 s per step, 8 s clips
39 to 54 s, and 10 s clips 56 to 75 s.

**Live H3 override evidence (E0051 and E0054, 2026-10-16).** These are two successful, independent `r2av` executions
of the same profile and three image references. Each run overrides its duration to 15 s, or 360 requested frames at
24 fps; H3 aligns this to **362 effective frames**, which `--frames` passed to the child and both outputs contain. The
resolved workload, rather than the profile's base timing or the unaligned 360, is the factory baseline: 832×448,
30 denoise steps, `denoise_reuse=1`, `core_reuse=1`, `dit_layers=50`, three image references, and the recorded false
rendering switches. The two runs used the same H3 executable build and no color correction.

| Measurement | E0051 | E0054 |
|-------------|------:|------:|
| Setup before denoise | 28.86 s | 32.30 s |
| Denoise, 30 steps | 4204.99 s | 4059.67 s |
| Mean step excluding first gap, as the fit reads it | 141.28 s | 136.10 s |
| First gap / later-step median | 0.764 | 0.832 |
| Fitted log step-time slope across the stage | 0.082 | 0.255 |
| FFmpeg for 362 frames | 184.16 s | 195.79 s |
| Child total | 4419.97 s | 4289.76 s |
| Framecontrol finishing | 1.85 s | 2.12 s |

The live denoise marks and the child log both show `denoise enqueue 0/30` through `30/30`; `denoise 30/30` is the
completion echo, not another 30 steps. The factory uses 138.6683 s per later step (the geometric mean of the two rates), a 0.798 first-gap ratio, a 0.99334
stage factor, a 0.1685 within-stage slope, and a broad 0.25 log spread. It matches the exact resolved workload and
executable build. Two executions support a local point and its observed run variation, not a scaling law for other
frame counts, sizes, references, settings, or builds.

**Draw Things stages.** The CLI prints `Starting...`, `Processing...`, `Sampling... n / N`, `Finishing...`, and `Generated`,
each with a bar and a percentage, then `Wrote:` and a `Generation timing:` block. Its `n / N` is printed when step n starts,
so `40 / 40` begins the last step and 39 are done. In the 8 runs the first sampling reading came 9 to 17 s in, the last step
took 50 to 57 s, `Finishing` (decode and write) took 11 s, and runs took 1962 to 2186 s. The step time was not steady: in 7
runs it rose from 42.7 to 45.3 s in the first quarter of the steps to 50.1 to 56.0 s in the last. The bar's percentage is
the CLI's own prediction, not a measurement. Framecontrol's finishing took about 3 s.

**New live Draw Things evidence (E0053, 2026-10-16).** This was a successful two-run `i2v` job on the same host, using
`wan_v2.2_a14b_hne_i2v_i8x.ckpt` at 384×768, with 33 output frames and 25 sampling steps in each run. The two runs share
one execution and settings, so they provide one calibration point with within-execution variation, not evidence of how
time scales with size, frames, or steps. Times below come from the live stage rows; the CLI's own sampling averages were
12.84 and 13.48 s, which exclude the final step as described below.

| Measurement | Run 1 | Run 2 |
|-------------|------:|------:|
| Starting plus processing | 7.8 s | 8.2 s |
| Sampling, 25 steps | 333.3 s | 348.7 s |
| Mean sampled step, including the last | 13.33 s | 13.95 s |
| Mean of first 6 steps → last 6 steps | 12.16 → 14.79 s (+21.6%) | 13.23 → 14.69 s (+11.0%) |
| CLI finishing | 4.7 s | 4.6 s |
| Child total | 345.8 s | 361.5 s |
| Framecontrol finishing, including color correction | 7.8 s | 7.5 s |

The auto cooldown after run 1 was 173 s (half the child time, rounded), making the whole job about 896 s. A provisional
factory point for this *exact* model, size, frame count, and step count is 13.6710 s per later step (the geometric mean of the two rates after the first gap), with a 0.99758 whole-stage factor, about 8 s of child setup,
4.64 s of child finishing, and 7.65 s of Framecontrol finishing with this color policy. The fitted step slopes are 0.240
and 0.170 across the stage, so the factory uses 0.205 with a 0.25 log spread for its step rate and fixed stages. The two
stage means differ by 4.6%; the range stays broad because these runs share one execution. The factory point applies only
when backend, pipeline, model, executable build, width, height, frames, steps, guidance, strength, shift, and color correction agree; otherwise
the existing history tiers or live-only fallback apply. The earlier eight-run Draw Things evidence has a very different
step time and is not pooled into one unconditioned backend constant.

The live `run_timing.workload` for both E0053 runs incorrectly recorded `frames: 17` and `steps: 40` from the base
configuration. The per-run CLI flags supplied 33 frames and 25 steps; both the output check and sampling counter confirm
them. The timing workload hook now applies CLI overrides before recording or describing a run. The fit and replay loader
reconciles this old row when the run's `knobs.frames` agrees with its measured output frames, and takes steps from the
complete sampling stage. The historical rows were not rewritten. This is a recorder error, not evidence that the child
ignored its settings.

## Line formats

What the recorder and the backfill read. A Draw Things job log (an invented example in the format of a real one, not a
transcript):

```text
2026-01-02 03:04:05.000 [app] Run 1/3 (pair default): input=... output=...
2026-01-02 03:04:08.000 [app] Started subprocess (PID 4242)
2026-01-02 03:04:12.000 [stdout +7.0s] Starting... [ ] 0  %
2026-01-02 03:04:18.000 [stdout +13.0s] Processing... [ ] 2  %
2026-01-02 03:04:22.000 [stdout +17.0s] Sampling... 1 / 40 [ ] 2  %
2026-01-02 03:50:00.000 [stdout +2755.0s] Finishing... [█] 98 %
2026-01-02 03:50:12.000 [stdout +2767.0s] Generated [█] 100%
2026-01-02 03:50:12.000 [stdout +2767.0s]   Sampling step time (40 step(s)): avg 60.00 s, median 61.00 s
2026-01-02 03:50:12.100 [app] Subprocess finished with exit code 0
2026-01-02 03:50:27.000 [app] Color drift check (run 1): ...
```

The runner starts three seconds before `Started subprocess` is logged (the clean-exec helper), and the child's stamps count
from the runner's start, so the child's origin is recovered from the lines themselves (a line is logged no earlier than it
is stamped, so the least of each line's logged time less its stamp is the origin, to the stamp's 0.1 s resolution) and not
from `Started subprocess`. The CLI's average and median step are over 40 durations: the first from the last `Processing`
line to the first sampling reading and the rest the 39 gaps between consecutive readings, with the last step (which
`Finishing` ends) not counted; the recorder's marks reproduce them to within 1% in the tests.

**H3, from its source** (`contrib/backends/h3.c`, `h3_cli.c` and `main.c`, `cli_progress`):

```c
fprintf(stderr, "\r%-25s %4d/%-4d", phase, completed, total);
if (completed >= total) fputc('\n', stderr);   /* only a phase's last reading is terminated */
```

- Progress goes to **stderr**, not stdout. A reading is `<phase>` padded to 25 columns, a space, the number done in a
  4-column field, `/`, and the total padded to 4: `denoise enqueue             29/30  `.
- Every redraw begins with `\r`. A reading ends in a newline only when `completed >= total`, or when the next reading is of
  another phase (`\n` is written first, only if the line was left open). A reading is therefore delivered when the next one
  is written, which is the lag: in E0047, `video VAE encoder 8/9` and `9/9` carry one timestamp, and `denoise enqueue
  29/30`, `30/30`, and `denoise 30/30` all arrive at child +2174.2 s.
- A phase reports `0/N` as it begins and `N/N` as it ends, and an identical reading is not written twice. The phases are
  `tokenizer`, `video VAE encoder`, `audio VAE encoder`, `Qwen vision`, `text encoder`, `refine text`, `precompute AdaLN`,
  `load transformer core`, `denoise enqueue`, `denoise`, `preview VAE load`, `audio VAE`, `video VAE load` (36 layers), and
  `FFmpeg`.
- The denoise steps are reported on one of two counters, by the solver (`h3_dit.c`). The GPU Euler loop (lines 2674 to 2781,
  the path `--reuse 1 --core-reuse 1` runs take) prints `denoise enqueue` as each step completes and `denoise N/N` once, at
  the end. The RES solver (lines 2817 to 2973) prints `denoise` for every step and no enqueue. E0049, a GPU Euler run,
  logged 31 `denoise enqueue` readings, 0/30 to 30/30, 57 to 81 s apart, and one `denoise 30/30` with the same stamp as
  `denoise enqueue 30/30`. The step counter is whichever of the two carries the steps, and the recorder keeps both as the
  denoise stage's marks.
- Its other lines that hold an `n/N` (`h3: preview 3/30`, `h3: denoise preview 3/30, video frame 1/24 via kitty`,
  `h3: frame 5/24 via kitty`) begin `h3:`, are written only when a terminal graphics protocol is on, and are not phases.

The runner reads bytes, stamps each segment when its last byte arrives, and releases a reading that already reads whole once
nothing more arrives for 0.2 s. The backfill undoes the lag in logs written before that: a reading that is not its phase's
last was written when the one before it was logged, and the first reading after a phase's last one (its `0/N`) is estimated
and marked.

## What a row holds

A live row's `workload` is what the run was asked to do: the pixels, the frames the configuration asks for (a Draw Things
configuration may leave it unset, which reads as null), the steps, and for H3 the pipeline, references, and rendering knobs.
A row rebuilt from a log holds what the run is known to have done: its measured size and frames, and the steps its output
counted. The two key sets differ, so a fit that mixes fidelities should join `runs.output_*` for the frames of a live row.
`RunOutput.offset` and every stage's start and end are seconds after `RunStarted`.

## What the tests establish

Synthetic transcripts (an H3 `r2av` run, an H3 `i2av` run with a color-correction tail, and a Draw Things run with its
`Generation timing:` block) are written from tables of phases, counters, and times, and the spans they must make are worked
out from the tables, not by the code under test. They establish that the stages cover the child from its start to its exit,
that Framecontrol's stages and the gaps between them add up to the run, that each reading is stamped within 50 ms of the
arrival of its last byte (including a write that splits a number or a multi-byte character, and a reading that is never
terminated), and that a lagged H3 log backfills to spans within 0.5 s of the ones it was written from. They cannot show that
a real child prints what the fixtures print; the H3 fixtures follow `h3.c`'s progress code (padded fields, `0/N` readings,
stderr, newline only after a phase's last reading), and a real Draw Things log gave the other format.

## Host validation

Owner host, like [Milestone 06's](../phase-4/milestone-06-h3-color.md). This does not block the milestone.

Run on 2026-10-18 against the newest live runs of each backend in `state/framecontrol.db`, read through the database
directly (read-only) because the `fc serve` listening on 8765 that day belongs to another checkout with its own state and
does not know these executions. Step 3 (`fc timing import`) was not run: it writes to the store, and every run compared
here already has live rows.

Method: for each run, the stored `run_stages` rows (`start_offset`, `end_offset`, units, marks) against the job log's own
stamps, and for Draw Things the CLI's `Generation timing:` block.

| Run | Backend | Compared with | Result |
|-----|---------|---------------|--------|
| E0084 run 1 (`r2av`, 832x448, 30 steps, 504.4 s, executable 558688 bytes) | h3-metal-cli | its job log | Every printed phase is a named stage; none landed in `other`. Stage edges agree with the log's stamps within 0.2 s: denoise 24.48 to 423.43 s against the log's `0/30` at +24.5 s and `30/30` at +423.4 s, video VAE load ends at 425.13 s against +425.1 s, FFmpeg ends at 504.36 s against +504.2 s. FFmpeg's start is the previous stage's end (425.13 s), since H3 prints its first FFmpeg reading only at the end (`0/124` at +504.0 s). The 30 denoise steps are the 30 `denoise enqueue` readings after `0/30`. |
| E0053 run 1 (`i2v`, 384x768, 33 frames, 25 steps, 345.8 s, executable 160477120 bytes) | draw-things-cli | its job log and the CLI's `Generation timing:` block | The 25 stored sampling marks agree with the log's `Sampling... n / 25` stamps to 0.1 s (log `5 / 25` at +56.3 s, mark 4 at 56.33 s). Starting, processing, sampling and finishing add up to 345.82 s against the child's 345.7 s. Per-step mean 12.82 s and median 12.53 s against the CLI's 12.84 s and 12.42 s (0.2% and 0.9%). The CLI's 343.53 s total is 0.7% under the child's wall time, which is the time before its own clock starts. |

Both executables' versions are the sizes above. The recorder's stage timing is accurate on both backends. This
checks the recorder, not the forecast.

### Forecast accuracy on the retained runs

`uv run fc timing replay`, each run scored with its own execution left out, 31 executions and 32 runs (26 `h3-metal-cli`
`r2av`, 2 `draw-things-cli` `i2v`, and 4 older rows still under the backend id `h3`, which the rename did not migrate and
which therefore form a separate backend for the fit). Only the 26-run cell has enough runs to be judged.

| `h3-metal-cli` `r2av` run | 5% | 10% | 25% | 50% | 75% | 90% |
|---------------------------|----|-----|-----|-----|-----|-----|
| forecast before the reuse fix, median error | 11.7% | 13.0% | 25.5% | 28.0% | 30.4% | 7.7% |
| forecast before the reuse fix, signed bias | +11.4% | +13.0% | +20.3% | +19.9% | +17.4% | +3.8% |
| forecast, median error | 6.1% | 7.4% | 4.5% | 5.0% | 6.5% | 3.8% |
| forecast, signed bias | +3.7% | +5.6% | -1.3% | -0.5% | +3.2% | +2.0% |
| forecast, 80% range coverage | 0.85 | 0.81 | 0.85 | 0.81 | 0.65 | 0.77 |
| last-signature, median error | 4.1% | 4.3% | 5.1% | 7.7% | 15.4% | 38.5% |
| constant-pace, median error | 37.3% | 37.1% | 36.6% | 46.6% | 67.1% | 100.0% |

Job level, same 26 runs (one run per job): identical to the run level from 10% on. The forecast gave a number at once for all
32 runs (median 0.0 s), and the displayed share moved back 0 times, against 42.1 s and 1 and 2 moves for constant-pace. The
two Draw Things runs, both E0053, are listed and not judged (median error 0.4% to 5.0% across checkpoints), and the 4 `h3`
runs are not judged either.

What this shows:

- The first replay (the "before" rows) missed the run targets (15% at 10%, 5% at 50%, 10% at 90%) with a +20% bias. The cause
  was H3's reuse: under `denoise_reuse` or `core_reuse` above 1 the `denoise enqueue` readings arrive in bursts (E0085 at
  `core_reuse` 4 has gaps of 18 to 20 s then three of 0.0 s; E0087 at `denoise_reuse` 2 alternates), so a gap is not a
  step time. The median gap was near 0.02 s, every real step was read as a pause, and a run's mean step came out at 0.0 to
  9 s against a true 19 to 25 s. Those rows fed the fits, the slope and noise spreads and the heuristic's rate. H3
  evaluates the first step, the last and every interval-th (`h3_dit_reuse_schedule`, and the core refresh at
  `core_forward_count % interval`), which the data matches (E0085 has 9 real gaps in 30, E0087 16).
- The step statistics, the live update, the steps still to come and the heuristic's priced stage now count evaluated steps,
  and `denoise_reuse` and `core_reuse` act through that count rather than as fit features, so a reuse value the history has
  not seen is still priced. After the fix the median error is 4.5% to 7.4% from 5% to 75% and 3.8% at 90%, the bias is
  within +-6%, and the 10% and 50% checkpoints meet 15% but the 50% checkpoint's 5.0% only just meets 5%.
- Coverage of the 80% range is 0.65 at 75% and 0.77 at 90%, under the 0.70 floor at 75%; this was not investigated.
- Last-signature is still slightly closer from 5% to 25% (4% to 5%), and much worse after.
- These are real-host numbers on a small window, not a gate; the run targets change only through a changelog entry.
