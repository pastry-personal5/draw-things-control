# Time estimation: stage timing evidence and host validation

The measurements behind [Milestone 10](../phase-4/milestone-10-stage-timing.md) (how long each stage of a run
took) and [Milestone 11](../phase-4/milestone-11-time-estimation.md) (the forecast fitted on them), and the owner-host
checks of the recorder. The logs these numbers were read from expire with their executions (14 days by default) and, by
owner decision, no copy is kept: this note and the two milestones are the record. The tests use synthetic transcripts
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

**Draw Things stages.** The CLI prints `Starting...`, `Processing...`, `Sampling... n / N`, `Finishing...`, and `Generated`,
each with a bar and a percentage, then `Wrote:` and a `Generation timing:` block. Its `n / N` is printed when step n starts,
so `40 / 40` begins the last step and 39 are done. In the 8 runs the first sampling reading came 9 to 17 s in, the last step
took 50 to 57 s, `Finishing` (decode and write) took 11 s, and runs took 1962 to 2186 s. The step time was not steady: in 7
runs it rose from 42.7 to 45.3 s in the first quarter of the steps to 50.1 to 56.0 s in the last. The bar's percentage is
the CLI's own prediction, not a measurement. Framecontrol's finishing took about 3 s.

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
  `FFmpeg`; `denoise enqueue` runs ahead of `denoise`.
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

Not yet run. To run it:

1. Start `fc serve` on the new code and run one real Draw Things job and one real H3 job.
2. For each run, `fc timing show EXXXX --run N`, and compare it with the log: every `phase` is a line the backend printed;
   `start` and `duration` agree with the log's own stamps within 0.1 s; the stages add up to the run's time; a Draw Things
   run's total and its sampling average and median agree with the CLI's `Generation timing:` block within 1%.
3. `uv run fc timing import` on the logs that still exist; compare one imported H3 run with the same run's live rows if both
   exist, and note which phases are marked `*`.
4. Record below the executables' versions, any phase name that landed in `other`, and anything that disagrees.

| Run | Backend | Compared with | Result |
|-----|---------|---------------|--------|
| | | | |
