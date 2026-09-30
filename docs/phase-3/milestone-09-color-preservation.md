# Milestone 09: Color preservation

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 08](milestone-08-video-format-and-color.md): the resolved decode of each video
(`StreamColor`), its `colr` tag, and the media checks kept per run in the state store.

## Goal

Keep a long chain's colors true to its first image: brightness, contrast, saturation, hue, and skin tones. Every bias
the pipeline itself adds is removed; what the model adds is measured at every run; and when a job asks, it is
corrected in the frame each run hands to the next and in a corrected copy of each clip, without changing a file Draw
Things wrote.

The evidence is in [the color drift research note](../research/color-drift.md). Its summary table lists each stage
from the first image to the handoff, what it does to color, and whether that is read in code, simulated, or a
hypothesis.

## The owner's answers

From an interview on 2026-09-30 (see the [changelog](phase-3-changelog.md)):

- **Drift seen:** brightness and contrast, saturation creep, and a hue or skin-tone cast, all three.
- **Anchor:** what colors are held to is a per-job setting, and a job that says nothing gets no correction.
- **What a correction may touch:** the frame handed to the next run, and a corrected copy of each clip, ramped so
  the clips join without a jump. Draw Things' files are never changed.
- **Method:** region-aware, skin and people apart from the background, with Apple Vision.
- **Also in scope:** drift metrics for every run, generation-side settings, and wide-gamut first images.
- **Corrected copy format:** the original's.
- **No experiments while planning:** the measurements below are this milestone's first step.

## How colors are preserved

Five layers, cheapest first. Each lands and is verified on its own. The first four apply to every video job; the
fifth only when the job asks.

### 1. An exact handoff

The research found three biases in the handoff, all from reading and writing values, none from the picture itself:

- `draw-things-cli` truncates every output frame to 8 bits (`Int((v + 1) * 127.5)`): half a level dark on average,
  in every run and every format ([source](../research/color-drift.md#how-draw-things-cli-writes-a-video)).
- It reads a 16-bit PNG by its high byte (`>> 8`) and drops the rest. With Milestone 08's 257/256 rescale, each
  handoff is then 0.89 level dark in the shadows and 0.10 in the highlights, simulated
  ([the handoff](../research/color-drift.md#the-handoff)).
- It reads PNG values raw, with no color management, and resamples a PNG of another size nearest-neighbour. Other
  formats go through CoreGraphics, whose handling of them is not verified.

The fixes:

- **Undo the truncation.** Every frame the tool reads from a Draw Things video is taken half a level up, in 8-bit
  sRGB units, before anything else: for the last frame (built in Milestone 08), the metrics, and the correction.
- **Hand off a value draw-things-cli reads exactly.** Built in Milestone 08 (owner decisions, 2026-09-30):
  [the handoff](milestone-08-video-format-and-color.md#the-handoff) is 16-bit RGB from every source, each sample one
  8-bit value `v` as `v * 256 + 128`, which reads back as `v` through `>> 8`. `v` is the decoded level plus the
  truncated half level (none at black), rounded against a 2x2 ordered dither, so flat areas are unbiased too. The
  correction (layer 5) writes its handoff the same way.
- **Always hand `draw-things-cli` a normalized PNG for run 1.** A job that does not resize still gets a copy of its
  input: upright, alpha flattened, converted to sRGB (layer 2), 8-bit, at exactly the generation size. `draw-things-cli`
  then only ever reads a PNG it takes as it is, and the tool decides every value the model sees.
- **Read 16-bit sources with rounding.** Pillow keeps a 16-bit RGB PNG's high bytes. A 16-bit first image is read
  through ffmpeg into 16-bit RGB, as last frames are, and rounded once at the end.

### 2. The first image's gamut

`to_srgb` clips colors outside sRGB channel by channel, which bends their hue (a Display P3 red turns orange) and
merges neighbouring shades. Instead:

- A matrix-and-curves profile (Display P3, Adobe RGB, ProPhoto, Rec. 2020: the `rXYZ`, `gXYZ`, `bXYZ` colorants and
  the `rTRC`, `gTRC`, `bTRC` curves, of type `curv` or `para`) is applied in floating point: source values to linear,
  to XYZ, adapted from the profile connection space's D50 to D65 by Bradford, to linear sRGB, unbounded.
- Colors outside sRGB are brought in at constant Oklab lightness and hue, by compressing chroma toward the neutral
  axis. A soft knee starts at 80% of the sRGB boundary's chroma for that lightness and hue, so the most saturated
  colors keep their order and in-gamut colors near the boundary barely move.
- Any other profile (lookup tables, CMYK) goes through LittleCMS with the perceptual intent. Today's relative
  colorimetric intent clips.
- The resize still resamples in linear light and rounds once. The resized-input check gains how many pixels were out
  of gamut and the largest chroma reduction.

### 3. Measuring drift in every run

Every video run gets a `color_drift` media check, whether or not its job corrects (owner decision: metrics for every
run). It is measured in Oklab, where the owner's four drifts are the
lightness median (brightness), the lightness spread from the 10th to the 90th percentile (contrast), the chroma
median (saturation), and the chroma-weighted circular mean hue. A cast is the mean `a` and `b` of near-neutral
pixels (chroma under 0.03). Each is measured for the whole frame and, with Vision (layer 5's regions), for people,
skin, and the background.

Three comparisons, since pixels cannot be matched one to one across motion; statistics of each region are compared
instead:

| Comparison | Tells |
|------------|-------|
| The run's frame 0 against its input | How faithfully the model and the handoff carry color into a run; a nonzero mean here is a pipeline bias, not drift |
| The last frame against frame 0 | The model's own drift within the run |
| The last frame against the first image | The chain's drift so far |

The check's summary reads like `since the first image: L -2.1, contrast x1.06, chroma x1.08, hue +3°, skin hue
+2°`, with `L` in hundredths of Oklab lightness, and it warns beyond thresholds to be set from the first
measurements (proposed: 3 in `L`, 10% in contrast or chroma, 5° in hue). The facts keep every number per region, and
for the in-run comparison, the curve over every fourth frame. It is kept in `media_checks` like every check, with no
schema change, and shown wherever checks are shown.

### 4. Generation-side settings

A job can already set `guidance_scale`. This project's configuration uses 5, and Wan 2.2's reference for A14B
image-to-video is 3.5. High guidance is the best-documented cause of oversaturation. Three Draw Things settings
become new `config_override` keys, mapped to the Draw Things keys and never written to `data/params/`:

| Key | Draw Things key | Values |
|-----|-----------------|--------|
| `cfg_zero_star` | `cfgZeroStar` | `true` or `false` |
| `cfg_zero_init_steps` | `cfgZeroInitSteps` | an integer from 0 |
| `color_calibration` | `colorCalibration` | `none` or `lab` |

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
  anchor: blend        # none (the default), previous, first, or blend
  strength: 1.0        # 0 to 1: how much of the estimated correction is applied
  first_weight: 0.25   # blend only: the share of the remaining gap to the first image each run closes
  regions: true        # people and skin corrected apart from the background (Apple Vision); false: the whole frame as one
```

**The anchors.** Each run is first corrected back to its own input, which removes the drift the model added in that
run. The anchor then says what else pulls it:

| Anchor | Pull toward the first image | Over many runs |
|--------|-----------------------------|----------------|
| `previous` | none | What each correction leaves adds up like a random walk: the spread grows with the square root of the runs |
| `first` | all of the remaining gap, within each run's caps | Held to the first image; an intended change of light or scene is pulled back |
| `blend` | `first_weight` of the gap | Bounded: the deviation shrinks by `1 - first_weight` each run, so it settles near its per-run error divided by `sqrt(1 - (1 - first_weight)²)` (1.5 times at 0.25) instead of growing; an intended change fades back over a few runs |

**The transform**, in Oklab, from statistics only, per region:

- **Tone:** a monotone curve on lightness through three matched percentiles (10th, 50th, 90th), with a monotone cubic
  between them and identity beyond. Moving the median is brightness; stretching the spread is contrast.
- **Color:** `(a, b)` becomes `s · R(θ) · (a, b) + d`. The gain `s` is the ratio of chroma medians (saturation),
  `θ` is the difference of the mean hues, and `d` is the difference of the neutral cast (the whole frame only).
- **Regions:** the whole frame first, then people, then a skin residual: skin's lightness median and mean `(a, b)`
  moved to skin's own reference, within tight caps. The skin mask comes from Vision: the face's own skin (inside the
  face contour, outside the eyes, brows, and lips) sets a Gaussian over Oklab colors, and the person mask confines
  it. A region counts only with enough of it: people 2% of the frame, skin 0.5%, a face in at least half of the frames
  sampled. Otherwise its parent's transform applies. With `regions: false`, or no Vision, only the whole frame's.
- **Blending:** soft masks, feathered by 1% of the frame's shorter side and smoothed over frames, blend the region
  transforms per pixel, so no edge shows.
- **Caps per run** (proposed, then set from the A/B chains): lightness median ±4 hundredths, spread ×0.92 to 1.08,
  chroma gain 0.88 to 1.12, hue ±6°, cast 0.015, skin residual 0.02. A cap that binds is reported, never exceeded. A
  large difference is more likely a change of scene than drift.
- **Back to sRGB:** linear sRGB, the same gamut compression as layer 2, then the sRGB curve, in floating point.

**Over the frames of a run.** The correction of frame `t` is the fit that takes frame `t`'s statistics to the run
input's, followed by the anchor's pull, ramped in by a smoothstep from 0 at frame 0 to all of it at the last frame.
The parameters are smoothed over frames (a running median of 5, then a Gaussian of 2 frames) so nothing flickers.
`strength` scales every parameter toward identity. Frame 0 is the model's copy of the run's input, which is the
previous corrected clip's last frame, so it is barely changed and the clips join without a jump.

**What it writes:**

- **The handoff** is the corrected last frame, from the floating-point result, not decoded again from the copy. It
  is written as the run's `...-last.png`, so resume reads it as today. The uncorrected frame stays beside it as
  `...-last-raw.png`.
- **The corrected copy**, `<clip>-cc.mov` beside the original, in the original's format. Frames are piped to ffmpeg
  as 16-bit RGB and converted with a named BT.709 matrix in limited range (ffmpeg's default RGB-to-YCbCr matrix is
  BT.601, so it is always named). ProRes 4444 is encoded by `prores_videotoolbox` from `p416le`, or by `prores_ks`
  from `yuv444p10le` when VideoToolbox is missing. H.264 and HEVC originals use ffmpeg's VideoToolbox encoders at no
  less than the original's bit rate. Frame rate and frame count are the original's. Frame headers and the `colr` box
  both state BT.709, so the copy does not contradict itself as Draw Things' files do. Milestone 08's output check
  runs on it, and its matrix measurement must read BT.709.
- **The anchor**, the first image exactly as `draw-things-cli` saw it, kept as `<job>-anchor.png` in the output
  directory. Run 1's resized copy is otherwise deleted after run 1, and `first`, `blend`, and the metrics need the
  anchor after a resume.
- **A `color_correction` media check** per run: each region's parameters, the caps that bound, and the drift left
  after correction (layer 3's numbers, measured on the corrected frames).

**When it fails** (Vision or an encoder missing, ffmpeg failing), the correction's check warns and the run hands off
the uncorrected frame (proposed; see [open questions](#open-questions)). A missing Vision falls back to the whole
frame, with a note.

**Cost:** per run, one decode to gather statistics, one decode to apply the correction, one encode, and Vision on
each frame. The estimate is seconds to tens of seconds for an 81-frame 832x448 clip; the first measurements set it.

## Scope

In scope:

- The half-level decode correction for the metrics and the correction (the handoff has it, from Milestone 08), the
  normalized first input, and 16-bit sources read with
  rounding (layer 1)
- Gamut mapping of the first image (layer 2)
- The `color_drift` check for every video run (layer 3)
- The `config_override` keys `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration`, and the A/B chains'
  results in the research note (layer 4)
- The `color` block, the region-aware correction, the handoff, the corrected copies, the anchor file, and the
  `color_correction` check (layer 5)
- Apple Vision through `pyobjc-framework-Vision`, a macOS-only dependency

Out of scope:

- `data/params/`: no configuration file changes, and none may be edited ([development rules](../development-rules.md))
- Changing any file Draw Things wrote, or retagging, correcting, or re-extracting existing outputs
- Stitching the clips of a chain into one video (not chosen)
- Grading toward anything but the chain's own images: no LUTs, looks, or reference images from outside the chain
- HDR or wide-gamut output; the working space stays sRGB
- A different segmenter (a classical skin model, or ONNX face parsing, were offered and not chosen)
- Filing upstream reports with Draw Things; offered to the owner separately, see [open questions](#open-questions)

## Order of work

0. **Check the source.** Confirm the installed `draw-things-cli` is built from the source the research read
   (`draw-things-community` `0e9c180`, swift-png `075dfb2`). If it is not, read its version of `pixelByte` and
   `loadTrainingTensor` again before step 2.
1. **Measure what needs no GPU.** In the owner's `duo` chain (execution E0016), compare run 2's frame 0 with run 1's
   16-bit last frame, its input, banded by tone. The handoff model predicts -0.9 levels in the shadows to -0.1 in the
   highlights, plus what the VAE adds. Also time Vision's masks at 832x448 and look at them on those frames, and
   check that `prores_videotoolbox` writes ProRes 4444 from `p416le` (its help lists the format, not the profile
   with it), else plan the copy on `prores_ks`.
2. **Layers 1 and 3:** the rest of the exact handoff (Milestone 08 built the handoff itself) and the metrics. They are
   cheap and change no picture, and from then on every run measures itself.
3. **A/B chains** (the owner's GPU time), measured by layer 3: the chains of [layer 4](#4-generation-side-settings).
   They set layer 5's caps and thresholds and say which settings to recommend.
4. **Layer 2:** the first image's gamut.
5. **Layer 5:** the correction and its copies, tuned on step 3's chains.

## Planned changes

### `jobs/`

- `media/oklab.py` (new): sRGB to linear and back, linear sRGB to Oklab and back, `ΔE_OK`, chroma and hue, and
  gamut compression at constant lightness and hue. numpy only.
- `media/clip_frames.py` (new): the frames of a video as floating-point sRGB, decoded with its `StreamColor` as
  `frames.py` does and raised half a level for Draw Things' truncation. A generator over frames, with the frame rate
  and count, so a clip never sits in memory whole. It also holds the encoder for the corrected copy.
- `media/regions.py` (new): the `Segmenter` protocol (a frame to a person mask and face landmarks), `RegionMasks`,
  the face-sampled skin model, feathering, and smoothing over frames. With no segmenter, the whole frame is one
  region.
- `media/vision_segmenter.py` (new): the Apple Vision `Segmenter`, with `VNGeneratePersonSegmentationRequest`
  (`balanced`) and `VNDetectFaceLandmarksRequest`, from PNG bytes through `VNImageRequestHandler`'s
  `initWithData_options_`. pyobjc is imported inside its functions, as `checks.py` now imports LittleCMS, so no other
  command loads it.
- `media/color_stats.py` (new): robust statistics per region (percentiles, chroma median, circular mean hue, neutral
  cast), and the comparison of two sets of them.
- `media/drift.py` (new): the `color_drift` check.
- `media/correction.py` (new): the fit, the anchor's pull, the ramp, the smoothing, the caps, and applying the
  transform to a frame. It knows nothing of files.
- `media/frames.py`: built in Milestone 08 (the handoff). The correction's handoff reuses its rounding.
- `media/checks.py`: the resized-input check reports the
  gamut mapping.
- `media/toolkit.py`: `MediaTools` gains `segmenter: Segmenter | None`.
- `inputs/gamut.py` (new): reading a matrix-and-curves ICC profile, and the conversion and gamut mapping of layer 2.
- `inputs/resize.py`: `to_srgb` uses `gamut.py`, and falls back to LittleCMS with the perceptual intent. 16-bit RGB
  is read through ffmpeg. A job without resize gets the normalized copy (`TemporaryInput` makes it, at scale 1).
- `definition.py` and `parsing.py`: the `color` block (`ColorPolicy`: `anchor`, `strength`, `first_weight`, `regions`),
  refused in image jobs, each key checked and named as `color.<key>` on error.
- `overrides.py`: `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration`, checked and mapped to the Draw
  Things keys.
- `output_naming.py` and `planning.py`: the names `...-last-raw.png`, `<clip>-cc.mov`, and `<job>-anchor.png`, reserved
  with each run's other paths so nothing is overwritten; `PlannedRun` carries the raw frame's and the copy's.
- `run_finisher.py`: after the last frame, the `color_drift` check; with an anchor, the correction, the handoff, the
  raw frame, the copy, and the `color_correction` check.
- `executor.py`: keeps `<job>-anchor.png` before run 1, and gives it to each run's finisher. On resume it reads it
  from the output directory.
- `text.py`: `job_summary` gains a `color` row (for example `blend, first 0.25, regions`), and the check lines of the
  new stages.

### `state/`

- A migration (schema 8): `runs.corrected_output`, the copy's file name, and `executions.anchor`, the anchor's.
  The new check stages need no schema change.

### `services/`

- `toolkit.py`: builds the Vision segmenter when pyobjc and macOS allow, else none. `queue_resume.py` passes the
  anchor to a resumed execution.

### `server/` and `tui/`

- `serializers.py` and `monitor.proto`: a run's `corrected_output`, and the execution's `anchor`. The `color` policy
  in `GET /v1/jobs/{job}`.
- `tui/text/execution.py`: the corrected copy under each run. The new checks show as every check does.

### Repository

- `pyproject.toml`: `pyobjc-framework-Vision>=12.2.2; sys_platform == 'darwin'`.
- `data/jobs/example-job.yaml`: a commented `color` block and the three new `config_override` keys.

### Tests

- `oklab`: Ottosson's published reference values; round trips; gamut compression keeps lightness and hue.
- `gamut`: a matrix-and-curves profile written by the test. A P3 pure red keeps its Oklab hue within 2° and lightness
  within 0.01; in-gamut colors move less than half a level; a `para` curve and a `curv` curve are both read.
- `clip_frames`: an ffmpeg-made clip whose frames were truncated as Draw Things does decodes with a mean bias under
  0.05 level; ProRes and H.264.
- `color_stats` and `drift`: synthetic frames with a known change (lightness +0.03, chroma ×1.1, hue +5°, a cast)
  are measured back within tolerance, per region, with a fake segmenter.
- `correction`: no change gives identity; a synthetic drift is removed; caps hold and are reported; `strength: 0` is
  identity; the ramp leaves frame 0 unchanged, and the last frame equals the handoff.
- **A simulated chain:** no `draw-things-cli`, a "model" that adds a fixed drift per run (lightness -0.01, chroma
  ×1.03, hue +1.5°), 20 runs. With `none` the drift grows as added. With `previous` the drift left over the 20 runs is
  small. With `blend` at 0.25 it stays within `ΔE_OK` 0.02 of the first image, and with `first` closer still.
- The corrected copy: the original's format, frame count, and frame rate; its matrix measures BT.709; its last frame
  equals the handoff within 1 level; the original's bytes are unchanged.
- `vision_segmenter`: one test on a synthetic frame, skipped when pyobjc or macOS is missing. Everything else uses the
  fake segmenter, and importing `draw_things_control.cli.app` does not import Vision.
- Parsing: the `color` block's keys, values, and errors; the three `config_override` keys.
- Executor and resume: the anchor is kept, and a resumed `blend` execution uses it; the handoff is `...-last.png`.
- `tests/test_architecture.py` still passes. No test starts the real `draw-things-cli`.

### Documentation, when it lands

- `docs/user-guide.md`: the `color` block, the new `config_override` keys, the files a corrected run writes, and the
  new checks.
- `docs/architecture.md`: the new modules in the `jobs/media/` and `jobs/inputs/` rows.
- The phase changelog, and this document's status and an "As built" section.

## Open questions

1. **A new anchor when the prompt pair changes.** A run whose prompt pair differs from the previous run's could start
   a new anchor from its input, so an intended change of scene is not pulled back. Recommended: not by default, as a
   `color.reanchor: prompt_pair` option.
2. **A failed correction:** hand off the uncorrected frame with a warning (recommended, as media checks only warn), or
   fail the run.
3. **The normalized copy for jobs without resize** changes what `draw-things-cli` reads for them. Recommended: yes.
4. **Upstream reports.** `pixelByte`'s truncation and the 16-bit read are Draw Things behavior. Offer: draft a report
   for the owner to file. Nothing is sent without the owner.
5. **Where Milestone 09 goes** in the order.

## Acceptance criteria

- Every frame the tool reads from a Draw Things video is raised half a level; a clip truncated as Draw Things does
  decodes with a mean bias under 0.05 level.
- A corrected handoff is written as Milestone 08's handoff is, and `draw-things-cli` gets, for run 1, an 8-bit sRGB
  PNG of exactly the generation size, for every job.
- A Display P3 first image's out-of-gamut colors keep their Oklab hue within 2° and lightness within 0.01; in-gamut
  colors move less than half a level.
- Every video run has a `color_drift` check, whether or not its job corrects, in the job log, the TUI, the gRPC
  stream, and `GET /v1/executions/{id}`.
- `cfg_zero_star`, `cfg_zero_init_steps`, and `color_calibration` pass to `draw-things-cli` from `config_override`.
- With `color.anchor` set, each run writes `<clip>-cc.mov` in the original's format, whose last frame equals the
  handoff within 1 level and whose frame 0 is no further from the run's input than the original's frame 0 is. No file
  Draw Things wrote changes.
- The simulated chain meets its bounds for each anchor.
- The A/B chains' results are in the research note.
- No file under `data/params/` changes.
- `make check` passes.
