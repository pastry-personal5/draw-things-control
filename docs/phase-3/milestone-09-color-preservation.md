# Milestone 09: Color preservation

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done (2026-10-01; increments A to D built on 2026-09-30, E on 2026-10-01; the A/B chains dropped and the constants accepted as they are, owner decision; see [As built](#as-built))
**Depends on:** [Milestone 08](milestone-08-video-format-and-color.md): the resolved decode of each video
(`StreamColor`), its `colr` tag, the handoff, and the media checks kept per run in the state store.

## Goal

Keep a long chain's colors true to its first image: brightness, contrast, saturation, hue, and skin tones. Every bias
the pipeline itself adds is removed; what the model adds is measured at every run; and when a job asks, it is
corrected in the frame each run hands to the next and in a corrected copy of each clip. A file Draw Things wrote is
never re-encoded and none of its pixels change; Milestone 08's `colr` tag stays the only change made to one.

The evidence is in [the color drift research note](../research/color-drift.md). Its summary table lists each stage
from the first image to the handoff, what it does to color, and whether that is read in code, simulated, or a
hypothesis.

## As built

Built on 2026-09-30 and 2026-10-01, in the increments the owner chose (see the [changelog](phase-3-changelog.md)): A,
the `config_override` keys (layer 4); B, the rest of the exact handoff, the normalized first input, the first image,
schema 8, and the `color_drift` check (layers 1 and 3); C, the gamut mapping (layer 2); D, the correction over the
whole frame (layer 5 without Vision); and E, Apple Vision's regions, in the drift check and the correction. Everything
below holds, with these as the built facts:

- **Step 0.** The installed `draw-things-cli` is built from `da9b0c8`, not `0e9c180`; the code the research read is the
  same in both ([research note](../research/color-drift.md#checked-in-milestone-09s-step-0)). The upstream reports are
  drafted in [draw-things-upstream-reports.md](../research/draw-things-upstream-reports.md). `prores_videotoolbox`
  writes ProRes 4444 from `p416le`.
- **Step 1 is deferred** (owner decision): E0016's clips are gone. Every run's `color_drift` check measures from now on.
  The handoff was measured on E0017 instead on 2026-10-01
  ([research note](../research/color-drift.md#measured-on-e0017)), and Vision's masks were timed and inspected on its
  frames and E0021's ([research note](../research/color-drift.md#vision-on-generated-frames)).
- **Schema 8** also adds `queue.resume_first_image` and `queue.resume_anchor`, since a queued resume carries its whole
  resume point on its queue row.
- **The first image without records.** A video job keeps it whether or not it writes records, named from the stem its
  manifest would have (owner decision, 2026-10-01). Until then a job without records kept none, and E0021's `blend`
  chain ran as `previous`.
- **The cast** is the mean `a` and `b` of the least chromatic tenth of the pixels, when that tenth is near-neutral
  (chroma under 0.03), rather than of every pixel under 0.03: the same pixels stay neutral when saturation changes.
- **The fit.** Each frame is fitted once, to a target between the run's input and the anchor, ramped by the smoothstep;
  its chroma gain is then refined on a sample of the frame, for the chroma the tone curve and the sRGB edge take.
- **The copy** holds the corrected values rounded to 8 bits as the handoff's are, so its pixels measure BT.709 and its
  last frame is the handoff's values. A ProRes copy's frame header leaves the transfer unknown, since ProRes has no sRGB
  transfer, and its `colr` box states it (owner decision).
- **Tested**: the handoff's numpy twin equals the `geq` handoff, and `strength: 0` hands off exactly what the
  extraction does; the simulated chain gives, averaged over four chains, `none` 0.207, `previous` 0.045, `blend`
  0.012, and `first` 0.004 in `ΔE_OK` of the statistics from the first image; and a 2-run `blend` job through the
  real media tools (a runner writing Draw Things-like ProRes) writes each run's copy, corrected handoff, and raw
  frame, keeps every pixel of the original, and starts run 2 from run 1's corrected handoff.

- **The background's own transform** (owner decision, 2026-10-01). With people corrected apart, the background is
  fitted to its own statistics, not corrected by the whole frame's transform: on E0017 the drift sat mostly in the
  background, and the whole frame's transform took people too far. The whole frame's applies where people do not
  count, and to a frame's region that has too little of it.
- **Skin** is set by a Gaussian over Oklab's `a` and `b` of each frame's face skin, fitted, then fitted again within 3
  standard deviations; a pixel is all skin within 2 and none beyond 3, times the person mask. With lightness in it, lit
  and shaded body skin fell out.
- **Blending.** The background's (or the whole frame's) transform, then people's blended by the person weight, then
  skin's residual on people's result, blended by skin's share of the person weight, so a pixel's share of skin is its
  skin weight. Masks are averaged over three frames at the segmenter's resolution, stretched to the frame, then
  feathered there, since the stretch differs across and down.
- **A region is corrected apart for a run** only when the input, the anchor, and half of the frames have enough of
  it; a frame with too little takes its parent's fit before the smoothing. The skin residual is fitted after people's
  unscaled transform, and `strength` scales both.
- **Vision in every drift check.** Where Vision is available, every video run's `color_drift` check measures regions,
  whatever `color.regions` says, so `dtc serve`'s worker loads pyobjc at its first check. A Vision that is missing,
  or cannot be loaded, is silent there. One that fails mid-run is a note in both checks: the drift check measures the
  whole frame from then on, and the correction corrects the whole run as one region, so no clip switches mid-way.
- **The segmenter** is given to `MediaChecker` and `ColorCorrector`, made once per process by `services/toolkit.py`,
  rather than carried on `MediaTools`. Each call runs in an autorelease pool.
- **`ColorStats.mean`**, the mean `a` and `b`, is added for the skin residual.
- **Tested** with a fake segmenter: regions found and measured, a drift of people only measured on people and skin
  and not the background, removed by the correction with the background left as it was, and Vision missing or
  failing mid-run leaving the whole frame corrected with a note; the landmark y-flip; one real Vision call; and that
  starting the CLI loads no pyobjc.

Dropped (owner decision, 2026-10-01):

- **Step 3, the A/B chains.** Not run. The caps, the drift check's limits, and the correction's time limit keep the
  values proposed in the plan, accepted as they are: caps of lightness median ±4 hundredths, spread ×0.92 to 1.08,
  chroma ×0.88 to 1.12, hue ±6°, cast 0.015, and skin residual 0.02 (`jobs/media/correction.py`); limits of 3 in `L`,
  10% in contrast or chroma, and 5° in hue (`jobs/media/drift.py`); and 10 seconds plus 1 a frame
  (`jobs/definition.py`). No generation setting is recommended over the configuration's. The job files
  `data/jobs/ab-*.yaml` stay, for the owner to run whenever.
- **Filing the upstream reports** is no longer part of this milestone; the drafts stay in
  [draw-things-upstream-reports.md](../research/draw-things-upstream-reports.md).

## The owner's answers

From an interview on 2026-09-30 (see the [changelog](phase-3-changelog.md)):

- **Drift seen:** brightness and contrast, saturation creep, and a hue or skin-tone cast, all three.
- **Anchor:** what colors are held to is a per-job setting, and a job that says nothing gets no correction.
- **What a correction may touch:** the frame handed to the next run, and a corrected copy of each clip, ramped so
  the clips join without a jump. Draw Things' files are never changed (that is, never re-encoded and no pixel
  changed; Milestone 08's `colr` tag aside, see [the goal](#goal)).
- **Method:** region-aware, skin and people apart from the background, with Apple Vision.
- **Also in scope:** drift metrics for every run, generation-side settings, and wide-gamut first images.
- **Corrected copy format:** the original's.
- **No experiments while planning:** the measurements below are this milestone's first step.

From the plan's review, the same day:

- **Order:** Milestone 09 is built next, before Milestone 07.
- **A failed correction** warns and hands off the uncorrected frame; the run still succeeds.
- **A stop or park during the correction** takes effect when the correction ends.
- **The normalized first input** goes to every job with an input, video or image, resized or not.
- **Re-anchoring** is on by default: a run whose prompt pair differs from the previous run's makes its own input the
  anchor, at every change, even when pairs alternate. `color.reanchor: never` turns it off.
- **The first image's file** stays when its execution is deleted, as outputs do.
- **Gamut compression** starts at 90% of the sRGB edge's chroma, and goes only as far as the source profile reaches.
- **Upstream reports** are drafted for the owner once step 0 confirms the installed build. Nothing is sent without
  the owner.

From the code review of increment D, 2026-10-01:

- **The API's worst case** also adds the `color_drift` check's 300 s to each video run, correcting ones included.
- **Lab, YCbCr, and HSV inputs** are converted, not refused: Lab from its values, YCbCr and HSV to RGB and then by
  their profile.
- **`strength` and `regions`** are refused with `anchor: none`.
- **The first image** is kept for every video job, with or without records: E0021 (`duo-blend-i8x`, 6 runs) wrote
  none, so its `blend` had no anchor and acted as `previous`.
- **A `t2v` job's first image** that is not kept is dropped by a `first_image_dropped` event.
- **A VideoToolbox that cannot encode** is found by a one-frame test encode, once per process, and `prores_ks` writes
  the copy.
- **The resized input check** keeps reading the source again.
- **The sRGB curve** has one copy, `oklab.srgb_to_linear`.

## How colors are preserved

Five layers, cheapest first. Each lands and is verified on its own. The first four apply to every video job; the
fifth only when the job asks.

### 1. An exact handoff

The research found three biases in the handoff, all from reading and writing values, none from the picture itself:

- `draw-things-cli` truncates every output frame to 8 bits (`Int((v + 1) * 127.5)`): half a level dark on average,
  in every run and every format ([source](../research/color-drift.md#how-draw-things-cli-writes-a-video)).
- It reads a 16-bit PNG by its high byte (`>> 8`) and drops the rest. With Milestone 08's first 257/256 rescale, each
  handoff was then 0.89 level dark in the shadows and 0.10 in the highlights, simulated
  ([the handoff](../research/color-drift.md#the-handoff)).
- It reads PNG values raw, with no color management, and resamples a PNG of another size nearest-neighbour. Other
  formats go through CoreGraphics, whose handling of them is not verified.

The fixes:

- **Undo the truncation.** Every frame the tool decodes from a Draw Things video is raised by the half level Draw
  Things truncated, `0.5 * min(1, e)` in 8-bit sRGB units, tapered to nothing at black as the handoff's is, before
  anything else: for the last frame (built in Milestone 08), the metrics, and the correction. A PNG the tool wrote
  (a handoff, the first image) is read as `draw-things-cli` reads it, an 8-bit value or a 16-bit sample's high byte,
  and is not raised.
- **Hand off a value draw-things-cli reads exactly.** Built in Milestone 08 (owner decisions, 2026-09-30):
  [the handoff](milestone-08-video-format-and-color.md#the-handoff) is 16-bit RGB from every source, each sample one
  8-bit value `v` as `v * 256 + 128`, which reads back as `v` through `>> 8`. `v` is the decoded level plus the
  truncated half level (none at black), rounded against a 2x2 ordered dither, so flat areas are unbiased too. The
  corrected handoff (layer 5) starts from floating-point values that carry the half level already, so it takes only
  the dither and `v * 256 + 128`, never the half level again. Milestone 08's rounding is an ffmpeg `geq` expression;
  the correction's is its numpy twin, and a test holds the two to the same output on one frame.
- **Always hand `draw-things-cli` a normalized PNG for run 1** (owner decision). Every job with an input, video or
  image, resized or not, gets a copy of it: upright, alpha flattened, converted to sRGB (layer 2), 8-bit, at exactly
  the generation size. A job without `desired_input_*` gets its copy at scale 1, since its input must already be the
  generation size, as today. `draw-things-cli` then only ever reads a PNG it takes as it is, and the tool decides
  every value the model sees. The input check's warning for an input that is not plain 8-bit sRGB RGB becomes a
  note, since the copy converts it, and the resized-input check runs on every copy.
- **Read 16-bit sources with rounding.** Pillow 12.3 opens a 16-bit RGB PNG as 8-bit RGB, keeping each sample's high
  byte. A 16-bit RGB first image is read through ffmpeg into 16-bit RGB instead, as last frames are, with its profile
  still read by Pillow, and rounded once at the end. An image job, which does not need ffmpeg, keeps Pillow's high
  bytes when ffmpeg is not found, and the resized-input check says so.

### 2. The first image's gamut

`to_srgb` clips colors outside sRGB channel by channel, which bends their hue (a Display P3 red turns orange) and
merges neighbouring shades. Instead, for a first image whose profile is not sRGB (an image with no profile is taken as
sRGB, as today):

- A matrix-and-curves profile (Display P3, Adobe RGB, ProPhoto, Rec. 2020: the `rXYZ`, `gXYZ`, `bXYZ` colorants and
  the `rTRC`, `gTRC`, `bTRC` curves, of type `curv` or `para`) is applied in floating point: source values to linear,
  to XYZ, adapted from the profile connection space's D50 to D65 by Bradford, to linear sRGB, unbounded.
- Colors outside sRGB are brought in at constant Oklab lightness and hue, by compressing chroma toward the neutral
  axis (owner decision). Colors within 90% of the sRGB edge's chroma, at their lightness and hue, do not move. From
  there, a smooth curve with slope 1 at the knee takes the source profile's own edge onto the sRGB edge. Where the
  profile reaches no further than sRGB, nothing moves. The most saturated colors keep their order, and colors inside
  sRGB beyond the knee lose a little chroma (about 4% at most for Display P3). Both edges are tabulated over
  lightness and hue from the profile's colorants, and interpolated.
- Any other profile (lookup tables, CMYK) goes through LittleCMS with the perceptual intent, which uses the profile's
  own gamut mapping where it has one. Today's relative colorimetric intent clips. What is still outside sRGB is
  clipped, and the resized-input check says so.
- The resize resamples as today (linear light when downscaling, sRGB values when upscaling), now from the
  floating-point result instead of 8-bit values, still one channel at a time, and rounds once. The resized-input
  check gains how many pixels were beyond the knee and the largest chroma reduction.

### 3. Measuring drift in every run

Every video run gets a `color_drift` media check, whether or not its job corrects (owner decision: metrics for every
run). It is measured in Oklab, where the owner's four drifts are the
lightness median (brightness), the lightness spread from the 10th to the 90th percentile (contrast), the chroma
median (saturation), and the chroma-weighted circular mean hue. A cast is the mean `a` and `b` of near-neutral
pixels (chroma under 0.03). Each is measured for the whole frame and, with Vision (layer 5's regions), for people,
skin, and the background. Vision runs on the frames compared: the run's input, frame 0, the last frame, and every
fourth frame between them.

Three comparisons, since pixels cannot be matched one to one across motion; statistics of each region are compared
instead:

| Comparison | Tells |
|------------|-------|
| The run's frame 0 against its input | How faithfully the model and the handoff carry color into a run; a nonzero mean here is a pipeline bias, not drift |
| The last frame against frame 0 | The model's own drift within the run |
| The last frame against the first image | The chain's drift so far |

The first image is the file layer 5's [What it writes](#5-correction) describes, kept for every video execution. An
execution recorded before this milestone has none, so a resume of one leaves the third comparison out, with a note.
Its snapshot has no `color` block, so nothing else needs it. A `t2v` job has no input: its run 1 has no input to
compare frame 0 with, and its first image is run 1's last frame, the first frame any of its runs is given.

The check's summary reads like `since the first image: L -2.1, contrast x1.06, chroma x1.08, hue +3°, skin hue
+2°`, with `L` in hundredths of Oklab lightness, and it warns beyond thresholds to be set from the first
measurements (proposed: 3 in `L`, 10% in contrast or chroma, 5° in hue). The facts keep every number per region, and
for the in-run comparison, the curve over every fourth frame. It is kept in `media_checks` like every check, with no
schema change, and shown wherever checks are shown.

### 4. Generation-side settings

A job can already set `guidance_scale`. This project's configuration uses 5, and Wan 2.2's reference for A14B
image-to-video is 3.5. High guidance is the best-documented cause of oversaturation. Three Draw Things settings
become new `config_override` keys. `core/arguments.py`'s `OVERRIDE_TARGETS` maps them to the Draw Things keys as keys
with no `draw-things-cli` flag, as it maps `shift`, and they are never written to `data/params/`:

| Key | Draw Things key | Values |
|-----|-----------------|--------|
| `cfg_zero_star` | `cfgZeroStar` | `true` or `false` |
| `cfg_zero_init_steps` | `cfgZeroInitSteps` | an integer from 0 |
| `color_calibration` | `colorCalibration` | `none` or `lab` |

The configuration files write `cfgZeroStar: false`, `cfgZeroInitSteps: 0`, and `colorCalibration: none`; step 0
confirms the names and values against the source.

`colorCalibration: lab` is Draw Things' own "previous run" anchor, applied to every frame inside Draw Things
([how it works](../research/color-drift.md#draw-things-colorcalibration)). It takes each frame's large-scale color
and brightness from the input image at the input's positions, so expect a ghost of the first frame with motion, and
no intended change of light within a run. It is tested, not adopted: the tool's correction (layer 5) can anchor to
the first image and keeps the original untouched.

What to recommend comes from A/B chains the owner runs (step 3 of [the order](#order-of-work)). Each chain has the
same input, seed, and prompts, at least 6 runs, and is measured with layer 3. The chains: today's settings; guidance
3.5; CFG-Zero\*; `color_calibration: lab`; and each of layer 5's anchors. The results go into the research note. Job
files change only by the owner's choice.

### 5. Correction

Opt-in, per job, with a new `color` block:

```yaml
color:
  anchor: blend          # none (the default), previous, first, or blend
  strength: 1.0          # 0 to 1: how much of the estimated correction is applied
  first_weight: 0.25     # blend only: the share of the remaining gap to the anchor each run closes
  reanchor: prompt_pair  # first and blend: prompt_pair (the default) or never; see below
  regions: true          # people and skin corrected apart from the background (Apple Vision); false: the whole frame as one
```

Each key is checked and named as `color.<key>` on error. `first_weight` is refused unless `anchor` is `blend`, and
`reanchor` unless it is `first` or `blend`. An image job refuses the block.

**The anchor** is what `first` and `blend` pull toward: the first image, exactly as `draw-things-cli` saw it for run 1.
With `reanchor: prompt_pair` (the default, owner decision), a run whose prompt pair differs from the previous run's
makes its own input, the previous run's corrected handoff, the anchor from then on, so an intended change of scene
is not pulled back past it. Every change counts: a job whose pairs alternate, as `example-job.yaml`'s walk and wave
do, re-anchors at every run, and `first` and `blend` then act as `previous` does (owner decision, knowing that).
`reanchor: never` holds such a job to the first image.

**The anchors.** Each run is first corrected back to its own input, which removes the drift the model added in that
run. The anchor setting then says what else pulls it:

| Anchor | Pull toward the anchor | Over many runs |
|--------|------------------------|----------------|
| `previous` | none | What each correction leaves adds up like a random walk: the spread grows with the square root of the runs |
| `first` | all of the remaining gap, within each run's caps | Held to the anchor; an intended change of light or scene within one prompt pair is pulled back |
| `blend` | `first_weight` of the gap | Bounded: the deviation shrinks by `1 - first_weight` each run, so it settles near its per-run error divided by `sqrt(1 - (1 - first_weight)²)` (1.5 times at 0.25) instead of growing; an intended change fades back over a few runs |

**The transform**, in Oklab, from statistics only, per region:

- **Tone:** a monotone curve on lightness through five points: black and white, which stay where they are, and three
  matched percentiles (10th, 50th, 90th), with a monotone cubic (Fritsch–Carlson) between them. Moving the median is
  brightness; stretching the spread is contrast.
- **Color:** `(a, b)` becomes `s · R(θ) · (a, b) + d`. The gain `s` is the ratio of chroma medians (saturation),
  `θ` is the difference of the mean hues, and `d` is the difference of the neutral cast (the whole frame only).
- **Regions:** the whole frame first, then people, then a skin residual: skin's lightness median and mean `(a, b)`
  moved to skin's own reference, within tight caps. The skin mask comes from Vision: the face's own skin sets a
  Gaussian over Oklab colors, and the person mask confines it. The face's skin is the hull of its contour and its
  brows, less the eyes, the brows, and the outer lips: `faceContour` runs from one cheek over the chin to the other,
  open at the top, so it bounds nothing on its own. Landmark points are relative to the face's bounding box, with the
  origin at its lower left, and are converted to pixels with `pointsInImage`. A region counts only with enough of
  it: people 2% of the frame, skin 0.5%, a face in at least half of the frames sampled. Otherwise its parent's
  transform applies. With `regions: false`, or no Vision, only the whole frame's.
- **Blending:** soft masks, feathered by 1% of the frame's shorter side and smoothed over frames, blend the region
  transforms per pixel, so no edge shows.
- **Caps per run** (proposed, then set from the A/B chains): lightness median ±4 hundredths, spread ×0.92 to 1.08,
  chroma gain 0.88 to 1.12, hue ±6°, cast 0.015, skin residual 0.02. A cap that binds is reported, never exceeded. A
  large difference is more likely a change of scene than drift.
- **Back to sRGB:** linear sRGB, then the sRGB curve, in floating point. A color the transform leaves inside sRGB
  passes through unchanged; only one it pushes outside is brought back onto the edge at constant lightness and hue.
  Layer 2's knee runs once, on the first image; a knee here would take chroma from near-edge colors in every run, and
  the chain would compound it.

**Over the frames of a run.** The correction of frame `t` is the fit that takes frame `t`'s statistics to the run
input's, applied in full, followed by the anchor's pull, ramped in by a smoothstep from nothing at frame 0 to all of
it at the last frame. Frame 0 is the model's copy of the run's input, which is the previous corrected clip's last
frame, so its fit is small: it brings frame 0 to the input's statistics, and the clips join without a jump. Only the
pull is ramped, so the frames in the middle of a clip are corrected in full for the run's own drift. The parameters
are smoothed over frames (a running median of 5, then a Gaussian of 2 frames) so nothing flickers. `strength` scales
every parameter toward identity.

**What it writes:**

- **The handoff** is the corrected last frame, from the floating-point result, not decoded again from the copy. It
  is written as the run's `<clip>-last-frame.png`, Milestone 08's name, so resume reads it as today. The uncorrected
  frame, extracted as Milestone 08 extracts it, stays beside it as `<clip>-last-frame-raw.png`.
- **The corrected copy**, `<clip>-cc.<ext>` beside the original, in the original's format and container. Frames are
  piped to ffmpeg as 16-bit RGB and converted with a named BT.709 matrix in limited range (ffmpeg's default
  RGB-to-YCbCr matrix is BT.601, so it is always named). ProRes 4444 is encoded by `prores_videotoolbox` from
  `p416le`, or by `prores_ks` from `yuv444p10le` when VideoToolbox is missing. H.264 and HEVC originals use ffmpeg's
  VideoToolbox encoders at no less than the original's bit rate. Frame rate and frame count are the original's. The
  frame headers and the `colr` box state what Milestone 08's tagger writes: BT.709 primaries, the sRGB transfer, the
  BT.709 matrix, limited range. So the copy does not contradict itself as Draw Things' files do. Milestone 08's
  output check runs on it, and its matrix measurement must read BT.709.
- **The first image**, `<manifest stem>-first-image.png` in the output directory, a copy of run 1's normalized input,
  kept for every video execution whether or not its job corrects: the metrics compare with it, and `first` and
  `blend` start from it. Run 1's normalized copy is otherwise deleted after run 1. `job_file_stem` takes a stem only
  when this name is free too, as it does for the manifest and the log. A resumed execution records the first image
  of the execution it resumes. Deleting an execution (Milestone 06) leaves the file, as it leaves outputs (owner
  decision), since a resumed execution may still need it. For a `t2v` job, which has no input, the first image is a
  copy of run 1's last frame, and run 1 is measured within itself and not corrected, with a note in its check.
- **Each run's anchor**, the file it was held to (the first image, or the input of the run that last re-anchored;
  none with `previous` or `none`), recorded with the run. A resume takes the anchor of the run it continues after,
  or, re-anchoring, its own input.
- **A `color_correction` media check** per run: each region's parameters, the anchor and whether the run
  re-anchored, the caps that bound, and the drift left after correction (layer 3's numbers, measured on the
  corrected frames).

**When it fails** (Vision or an encoder missing, ffmpeg failing, its time limit reached), the run still succeeds
(owner decision): its `color_correction` check warns, the uncorrected frame is the handoff (moved from
`-last-frame-raw.png` to `-last-frame.png`), and no copy is kept (a partial one is deleted). The next run corrects
back to its own input as every run does, so one failure leaves an uncorrected join, not a broken chain. A missing
Vision is not a failure: the correction falls back to the whole frame, with a note.

**A stop or park during the correction** takes effect when the correction ends (owner decision), as one during the
last frame's extraction does today. The correction has its own time limit (proposed: 10 seconds plus 1 per frame,
set from the first measurements), and running out of it is a failure as above.

**Cost:** per run, one decode to gather statistics, one decode to apply the correction, one encode, and Vision once
on each frame; the masks are kept between the two passes at Vision's resolution, 8-bit. The estimate is seconds to
tens of seconds for an 81-frame 832x448 clip; the first measurements set it. The API's `max_job_seconds` worst case
adds the correction's time limit to each run of a job that corrects, so the limit stays a bound.

## Scope

In scope:

- The half-level decode correction for the metrics and the correction (the handoff has it, from Milestone 08), the
  normalized first input for every job, and 16-bit sources read with rounding (layer 1)
- Gamut mapping of the first image (layer 2)
- The `color_drift` check for every video run, and the first image kept for every video execution (layer 3)
- The `config_override` keys `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration`, and the A/B chains'
  results in the research note (layer 4)
- The `color` block with re-anchoring, the region-aware correction, the handoff, the corrected copies, and the
  `color_correction` check (layer 5)
- Apple Vision through `pyobjc-framework-Vision`, a macOS-only dependency
- Drafts of the upstream reports, for the owner to file, after step 0

Out of scope:

- `data/params/`: no configuration file changes, and none may be edited ([development rules](../development-rules.md))
- Re-encoding any file Draw Things wrote or changing its pixels, or retagging, correcting, or re-extracting existing
  outputs
- Stitching the clips of a chain into one video (not chosen)
- Grading toward anything but the chain's own images: no LUTs, looks, or reference images from outside the chain
- HDR or wide-gamut output; the working space stays sRGB
- A different segmenter (a classical skin model, or ONNX face parsing, were offered and not chosen)
- Sending anything to Draw Things: the reports are drafts the owner files

## Order of work

0. **Check the source.** Confirm the installed `draw-things-cli` is built from the source the research read
   (`draw-things-community` `0e9c180`, swift-png `075dfb2`). If it is not, read its version of `pixelByte` and
   `loadTrainingTensor` again before step 2. Confirm the names and values of `cfgZeroStar`, `cfgZeroInitSteps`, and
   `colorCalibration`. Then draft the upstream reports on `pixelByte`'s truncation and the 16-bit read, citing that
   build, for the owner to file.
1. **Measure what needs no GPU.** In the owner's `duo` chain (execution E0016), compare run 2's frame 0 with run 1's
   16-bit last frame, its input, banded by tone. The handoff model predicts -0.9 levels in the shadows to -0.1 in the
   highlights, plus what the VAE adds. Also time Vision's masks at 832x448 and look at them on those frames, and
   check that `prores_videotoolbox` writes ProRes 4444 from `p416le` (its help lists the format, not the profile
   with it), else plan the copy on `prores_ks`.
2. **Layers 1 and 3:** the rest of the exact handoff (Milestone 08 built the handoff itself), the normalized first
   input, the first image, and the metrics. They are cheap and change no picture the model makes, and from then on
   every run measures itself.
3. **A/B chains** (the owner's GPU time), measured by layer 3: the chains of [layer 4](#4-generation-side-settings).
   They set layer 5's caps and thresholds and say which settings to recommend. (Dropped, owner decision, 2026-10-01:
   the proposed values stand.)
4. **Layer 2:** the first image's gamut.
5. **Layer 5:** the correction and its copies, tuned on step 3's chains.

## Planned changes

### `core/`

- `arguments.py`: `OVERRIDE_TARGETS` gains `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration`, each with
  no flag, so they reach `draw-things-cli` in `--config-json` as `cfgZeroStar`, `cfgZeroInitSteps`, and
  `colorCalibration`.

### `jobs/`

- `media/oklab.py` (new): sRGB to linear and back, linear sRGB to Oklab and back, `ΔE_OK`, chroma and hue, the sRGB
  edge's chroma tabulated over lightness and hue, and the knee's compression at constant lightness and hue. numpy
  only.
- `media/clip_frames.py` (new): the frames of a video as floating-point sRGB, decoded with its `StreamColor` as
  `frames.py` does and raised by the tapered half level for Draw Things' truncation. A generator over frames, with
  the frame rate and count, so a clip never sits in memory whole. It also holds the encoder for the corrected copy.
- `media/regions.py` (new): the `Segmenter` protocol (a frame to a person mask and face landmarks, in pixels),
  `RegionMasks`, the face-sampled skin model, feathering, and smoothing over frames. With no segmenter, the whole
  frame is one region.
- `media/vision_segmenter.py` (new): the Apple Vision `Segmenter`, with `VNGeneratePersonSegmentationRequest`
  (`balanced`) and `VNDetectFaceLandmarksRequest`, from PNG bytes through `VNImageRequestHandler`'s
  `initWithData_options_`, converting landmark points out of the face box's lower-left-origin coordinates. pyobjc is
  imported inside its functions, as `checks.py` imports LittleCMS, so no other command loads it.
- `media/color_stats.py` (new): robust statistics per region (percentiles, chroma median, circular mean hue, neutral
  cast), and the comparison of two sets of them.
- `media/drift.py` (new): the `color_drift` check.
- `media/correction.py` (new): the fit, the anchor's pull, the ramp, the smoothing, the caps, and applying the
  transform to a frame. It knows nothing of files.
- `media/frames.py`: the handoff's rounding as numpy too (the dither and `v * 256 + 128`, without the half level),
  for the correction's handoff.
- `media/checks.py`: the resized-input check runs on every copy and reports the gamut mapping and a 16-bit source
  read by its high bytes; the input check's warning for an input that is not plain 8-bit sRGB becomes a note.
- `media/toolkit.py`: `MediaTools` gains `segmenter: Segmenter | None`.
- `inputs/gamut.py` (new): reading a matrix-and-curves ICC profile, its edge tabulated in Oklab, and the conversion
  and gamut mapping of layer 2.
- `inputs/resize.py`: `to_srgb` uses `gamut.py`, and falls back to LittleCMS with the perceptual intent. 16-bit RGB
  is read through ffmpeg when it is found. `resample` takes floating-point channels.
- `inputs/size.py`: a scale-1 `ResizePlan` for an input already at the generation size, whose `needs_copy` is true
  and whose description says it is copied as 8-bit sRGB.
- `definition.py`: `ConfigOverride` gains `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration`;
  `JobDefinition` gains `color: ColorPolicy` (`anchor`, `strength`, `first_weight`, `reanchor`, `regions`), and
  `input_copy` gives a plan for every job with an input.
- `parsing.py`: the `color` block, with the rules above; a scale-1 plan for a job without `desired_input_*`.
- `overrides.py`: the three new keys' values checked, naming `config_override.<key>`.
- `output_naming.py`: `raw_last_frame_path` (`<clip>-last-frame-raw.png`) and `corrected_output_path`
  (`<clip>-cc.<ext>`); `next_output_path` draws a new number while either of them is taken too, and `job_file_stem`
  while `<stem>-first-image.png` is.
- `planning.py`: `PlannedRun` carries the raw frame's and the copy's paths, reserved with each run's other paths.
- `events.py`: `JobStarted` gains `first_image`, `RunStarted` gains `anchor`, and `RunFinished` gains
  `corrected_output`; `event_to_dict` carries them.
- `records.py`: the manifest gains `first_image`, and each run's record `anchor` and `corrected_output`.
- `run_finisher.py`: after the last frame, the `color_drift` check; with an anchor, the correction, the handoff, the
  raw frame, the copy, and the `color_correction` check. A stop requested meanwhile waits for it.
- `executor.py`: keeps the first image before run 1, tracks each run's anchor from the schedule, and gives both to
  each run's finisher. `ResumePoint` gains `first_image` and `anchor`.
- `text.py`: `job_summary` gains a `color` row (for example `blend, first 0.25, reanchor on prompt pair, regions`),
  and the check lines of the new stages.

### `state/`

- A migration (schema 8): `runs.corrected_output`, the copy's file name; `runs.anchor`, the file the run was held to;
  and `executions.first_image`. The new check stages need no schema change.
- `recorder.py`, `executions.py` (`start_run`, `finish_run`), `execution_rows.py` (`RUN_COLUMNS`), and
  `history_import.py` carry the new columns from the events and the manifest.

### `services/`

- `toolkit.py`: builds the Vision segmenter when pyobjc and macOS allow, else none.
- `queue_resume.py`: gives a resumed execution the first image of the execution it resumes, and the anchor of the run
  it continues after.
- `api_rules.py`: the worst case adds the correction's time limit to each run of a job that corrects.
- `history_delete.py`: nothing; the first image stays, as outputs do.

### `server/` and `tui/`

- `serializers.py`: each run's `corrected_output` and `corrected_output_exists` beside `last_frame_exists`, and its
  `anchor`; the execution's `first_image`; the `color` policy in `GET /v1/jobs/{job}`. `monitor.proto` does not change:
  events travel in its `data_json`.
- `tui/text/execution.py`: the corrected copy under each run. The new checks show as every check does.

### Repository

- `pyproject.toml`: `pyobjc-framework-Vision>=12.2.2; sys_platform == 'darwin'`.
- `data/jobs/example-job.yaml`: a commented `color` block, noting that its alternating pairs re-anchor at every run
  unless `reanchor: never`, and the three new `config_override` keys.

### Tests

- `oklab`: Ottosson's published reference values; round trips; gamut compression keeps lightness and hue, and leaves
  colors inside the knee alone.
- `gamut`: a matrix-and-curves profile written by the test. A P3 pure red keeps its Oklab hue within 2° and lightness
  within 0.01; colors within 90% of the sRGB edge's chroma move less than half a level; none moves away from neutral;
  an sRGB-sized profile moves nothing; a `para` curve and a `curv` curve are both read.
- Inputs: a job without `desired_input_*` gets an 8-bit sRGB copy at scale 1, and a 16-bit RGB PNG is read through
  ffmpeg with rounding, or by its high bytes with a note when ffmpeg is missing.
- `clip_frames`: an ffmpeg-made clip whose frames were truncated as Draw Things does decodes with a mean bias under
  0.05 level, and black stays black; ProRes and H.264.
- `frames`: the numpy handoff rounding, given the decoded level plus the tapered half level, equals the `geq` handoff
  of the same frame.
- `color_stats` and `drift`: synthetic frames with a known change (lightness +0.03, chroma ×1.1, hue +5°, a cast)
  are measured back within tolerance, per region, with a fake segmenter; with no first image, the third comparison
  is left out with a note.
- `correction`: no change gives identity; a synthetic drift is removed; caps hold and are reported; `strength: 0` is
  identity, and a saturated color inside sRGB comes out of it unchanged; the tone curve keeps black and white and is
  monotone; the pull is nothing at frame 0 and all of it at the
  last frame, and frame 0's statistics are brought to the input's; the last frame equals the handoff.
- **A simulated chain:** no `draw-things-cli`, a "model" that adds a fixed drift per run (lightness -0.01, chroma
  ×1.03, hue +1.5°), 20 runs with one prompt pair throughout. With `none` the drift grows as added. With `previous` the drift left over the 20 runs is
  small. With `blend` at 0.25 it stays within `ΔE_OK` 0.02 of the first image, and with `first` closer still. With
  `reanchor: prompt_pair` and alternating pairs, `blend` behaves as `previous`; with `never`, as `blend`.
- The corrected copy: the original's format, container, frame count, and frame rate; its matrix measures BT.709; its
  last frame equals the handoff within 1 level; the original's bytes are unchanged. A failed correction hands off
  the raw frame and leaves no copy.
- `vision_segmenter`: one test on a synthetic frame, skipped when pyobjc or macOS is missing. Everything else uses the
  fake segmenter, and importing `draw_things_control.cli.app` does not import Vision.
- Parsing: the `color` block's keys, values, and errors, `first_weight` without `blend` and `reanchor` without
  `first` or `blend` refused; the three `config_override` keys, and their names in `--config-json`.
- Executor and resume: the first image is kept and named from the manifest's stem; each run records its anchor; a
  resumed `blend` execution uses the first image and the anchor of the run it continues after; the handoff is
  `<clip>-last-frame.png`; a stop during the correction waits for it; a `t2v` job's first image is run 1's last
  frame, and its run 1 is not corrected.
- The migration, the recorder, and the history import carry the new columns; the serializer shows them.
- `tests/test_architecture.py` still passes. No test starts the real `draw-things-cli`.

### Documentation, when it lands

- `docs/user-guide.md`: the `color` block and re-anchoring, the new `config_override` keys, the normalized first
  input, the files a video run and a corrected run write, and the new checks.
- `docs/architecture.md`: the new modules in the `jobs/media/` and `jobs/inputs/` rows.
- The phase changelog, and this document's status and an "As built" section.

## Acceptance criteria

- Every frame the tool decodes from a Draw Things video is raised by the tapered half level; a clip truncated as Draw
  Things does decodes with a mean bias under 0.05 level.
- A corrected handoff is written as Milestone 08's handoff is, without adding the half level twice, and
  `draw-things-cli` gets, for run 1, an 8-bit sRGB PNG of exactly the generation size, for every job with an input.
- A Display P3 first image's out-of-gamut colors keep their Oklab hue within 2° and lightness within 0.01; colors
  within 90% of the sRGB edge's chroma move less than half a level.
- Every video run has a `color_drift` check, whether or not its job corrects, in the job log, the TUI, the gRPC
  stream, and `GET /v1/executions/{id}`, and every video execution keeps its first image.
- `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration` pass to `draw-things-cli` from `config_override`.
- With `color.anchor` set, each run writes `<clip>-cc.<ext>` in the original's format, whose last frame equals the
  handoff within 1 level and whose frame 0 is no further from the run's input than the original's frame 0 is. No file
  Draw Things wrote is re-encoded or has a pixel changed.
- A failed correction leaves the run succeeded, handing off the uncorrected frame, with a warning.
- The simulated chain meets its bounds for each anchor, with and without re-anchoring.
- ~~The A/B chains' results are in the research note.~~ Dropped (owner decision, 2026-10-01): the chains are not run,
  and the proposed constants stand.
- No file under `data/params/` changes.
- `make check` passes.
