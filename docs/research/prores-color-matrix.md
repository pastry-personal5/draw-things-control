# ProRes color matrix in Draw Things output

Which YCbCr matrix Draw Things' ProRes 4444 output is really encoded with, and whether its frame headers say so,
for [Phase 3, Milestone 08](../phase-3/milestone-08-video-format-and-color.md#the-color-matrix). Researched
2026-09-30.

## Summary

| Question | Answer |
|----------|--------|
| What does `--video-format prores4444` write? | `ap4h` ProRes 4444, `yuva444p12le`, `tv` range, progressive, no `colr` box, no primaries and no transfer. The matrix in the frame header is `bt709` in both clean files of 2026-09-30 (the app's and the current CLI's), and `smpte170m` in the 9 older app files and in the CLI's noise run |
| Which matrix was it encoded with? | BT.709, in all 11 files measured: the 9 older ones and both new ones. A free search over Kr and Kb lands on BT.709's coefficients |
| Does the header tell the truth? | In both new files, yes: header and pixels agree on BT.709 limited range. In the 9 older files, no: they state `smpte170m` and were encoded BT.709 |
| Which matrix does ffmpeg decode ProRes with? | The frame header's, even when a `colr` box in the container says otherwise |
| Which does it decode H.264 with? | The `colr` box's, when the stream itself states none |
| Alpha | Constant 4080 of 4095 (8-bit 255 shifted into 12 bits) in every file, written to a 16-bit PNG as 65280, which reads as 254 in 8 bits |

## The test

An 8-bit RGB frame, converted to 12-bit YCbCr with some matrix, comes back near whole 8-bit levels only when it is
converted back with the same matrix; with any other matrix the fractions spread evenly. The alpha plane at exactly
255 << 4 shows Draw Things gives the encoder 8-bit RGBA. So, for each candidate matrix: decode the frames to
floating-point RGB (limited range), scale to 0 to 255, and take the mean distance to the nearest integer. Random is
0.25; the lower one is the matrix used. ProRes is lossy, so the right matrix does not reach 0.

```bash
ffmpeg -v error -i clip.mov -f rawvideo -pix_fmt yuva444p12le - > frames.raw
```

Then, per pixel, with `Y = (Y12 - 256) / 3504`, `Cb = (U12 - 2048) / 3584`, `Cr = (V12 - 2048) / 3584`, and the
matrix's `Kr`, `Kb` (BT.601 0.299, 0.114; BT.709 0.2126, 0.0722; BT.2020 0.2627, 0.0593): `R = Y + 2(1 - Kr) Cr`,
`B = Y + 2(1 - Kb) Cb`, `G = (Y - Kr R - Kb B) / (1 - Kr - Kb)`.

Two checks were added for the new files:

- **Free search.** The same score over a grid of Kr from 0.18 to 0.32 and Kb from 0.05 to 0.13, in steps of 0.005,
  instead of three named matrices. The minimum is the matrix used, whatever its name.
- **Range.** The same test with full-range scaling (`Y = Y12 / 4095`, `Cb = (U12 - 2048) / 4095`). If the file is
  limited range, every matrix then scores near random.

The header is read from the first decoded frame, which is what ffmpeg decodes with, not from the stream: once a
`colr` box is added, ffprobe's stream-level `color_space` reports the box instead.

```bash
ffprobe -v error -select_streams v:0 -read_intervals %+#1 -show_entries frame=color_space,color_range,color_primaries,color_transfer -of csv clip.mov
```

## Results

| File | Header matrix | BT.601 / `smpte170m` | BT.709 | BT.2020 |
|------|---------------|----------------------|--------|---------|
| Control: 8-bit PNG through `prores_ks` 4444 with BT.601 | `smpte170m` | **0.197** | 0.250 | 0.249 |
| Control: the same with BT.709 | `bt709` | 0.251 | **0.197** | 0.249 |
| Draw Things app, `project-0007/cut-0000-0001`, 1 of 2 | `smpte170m` | 0.249 | **0.217** | 0.247 |
| Same, 2 of 2 | `smpte170m` | 0.259 | **0.217** | 0.251 |
| Same, `cut-0002-all` | `smpte170m` | 0.250 | **0.217** | 0.248 |
| Current `draw-things-cli`, first run (noise, see below) | `smpte170m` | 0.250 | 0.250 | 0.250 |
| Draw Things app, `8005-output/video-20260930-091742-1790727462069617521.mov` | `bt709` | 0.250 | **0.216** | 0.250 |
| Current `draw-things-cli`, job `v-i8x`, `v-i8x-20260930-093737-5745.mov` | `bt709` | 0.241 | **0.216** | 0.260 |

The two new files are scored over all 17 frames; each frame on its own gives BT.709 0.216 to 0.217, and the other
two matrices 0.229 to 0.265.

| New file | Free search, best Kr, Kb | Full-range scaling, all three | Luma, 12-bit | Neighbouring luma correlation |
|----------|--------------------------|-------------------------------|--------------|-------------------------------|
| App, `video-20260930-091742-...` | 0.215, 0.070 to 0.075 | 0.253 to 0.256 | 256 to 2961 | not measured |
| CLI, `v-i8x-20260930-093737-5745` | 0.215, 0.075 | 0.246 to 0.248 | 252 to 3088 | 0.989 (an image, not noise) |

BT.709 is 0.2126, 0.0722. Luma just under 256 is ProRes overshoot at edges, not full range.

On the first older app file, the `smpte170m` decode differs from a BT.709 one by 1.3 levels on average and 10.3 at
most, on an 8-bit scale.

## How ffmpeg reads a tagged file

Tested with `prores_ks` and `libx264` files, before and after `jobs/media/video_color.py` added its BT.709 `colr`
box:

| File | Stream `color_space` | First frame `color_space` |
|------|----------------------|---------------------------|
| H.264, untagged | `unknown` | `unknown` |
| Same, tagged | `bt709` | `bt709` |
| ProRes 4444 made with `smpte170m` | `smpte170m` | `smpte170m` |
| Same, tagged | `bt709` | `smpte170m` |

So a ProRes file whose header says `smpte170m` and whose box says BT.709 contradicts itself, and ffmpeg decodes it as
BT.601. `has_matrix_tag` in `jobs/media/frames.py` reads the stream value after the tagger has run, so it sees the
box, not the header.

## The CLI's runs

The first, on 2026-09-30:

```bash
uv run dtc generate --config-file data/params/image-to-video-wan-2-2-default-i8x.yaml --model wan_v2.2_a14b_hne_i2v_i8x.ckpt --image in48.png --prompt "the two people keep walking, gentle camera motion" --frames 17 --steps 8 --video-format prores4444 --output verify.mov
```

`in48.png` was an 832x448 last frame converted to 16-bit RGB (`rgb48be`, no alpha). The run exited 0 after 106 s and
wrote the format in the summary, but every frame, the first included, is colored noise, with chroma codes over
nearly the whole 12-bit range. Its header stated `smpte170m`. `verify.mov` could not be found afterwards, so the
header cannot be read again.

The second was job `data/jobs/v-i8x.yaml` through `dtc serve`, with `output.video_format: prores4444` and every run's
`--disable-preview`: one run, 17 frames, 576x768, 40 steps (the configuration's), from an 8-bit JPEG input resized
to an 8-bit PNG. Its frames are a clean image, and the measurement above settles its matrix.

The two runs differ in the input's depth and in the steps, so the noise is still not explained. The two headers
also differ, and why is not known: the noise run may have hit an error path in the encoder, or the CLI may have
changed.

## The owner's chain, and the frame size

Job `duo` (execution E0016, 2026-09-30): 2 runs, 832x448, 17 frames at 40 steps, ProRes 4444, run 2 from run 1's
16-bit last frame. Both videos state `smpte170m` in every frame, and both were encoded BT.709 (0.225 against 0.246
to 0.250). Run 2 is a clean image (neighbouring luma correlation 0.993), so a 16-bit PNG input works; the first
verification run's noise then points to its 8 steps, not its 16-bit input.

Every ProRes file measured, by size:

| Size | Frame header states | Pixels encoded |
|------|---------------------|----------------|
| 448x576 (9 older app files) | `smpte170m` | BT.709 |
| 832x448 (the noise run, the two `duo` runs) | `smpte170m` | BT.709 |
| 576x768 (the app's and the CLI's files above) | `bt709` | BT.709 |
| 576x1152 (one app file, not measured) | `bt709` | - |

So the encoder labels by frame size, not by what Draw Things did: the data fits "height 720 or more is BT.709" and
"more pixels than 720x576 is BT.709" alike; a 1024x448 run would tell them apart. Decoding the `duo` last frames as
stated put them 1.4 levels off on average and 9.4 to 12.1 at most (red -1.9, green -1.2, blue 0.0; on saturated
pixels red -5.4 to -5.7).

## ffmpeg's 16-bit RGB

ffmpeg 8.1 converts to 16-bit RGB at about 256 times the 8-bit value: white is 65283, mid-gray 32772, level 16 is
4099, from YCbCr and from 8-bit RGB alike. A reader that divides by 65535 sees such a frame 0.4% dark: on a ProRes
4444 round trip of an 8-bit image, a bias of -0.47 levels on average (reading only the high byte gives the same).
Scaling by 257/256 afterwards (`lutrgb`, then the format set again, or ffmpeg writes an 8-bit PNG) brings the bias to
0.01 levels, the precision of an exact floating-point decode (mean error 0.18 levels). The 8-bit route is unbiased
too, with 95% of values exact.

## Not verified

- That the first CLI run's noise came from its 8 steps: a 16-bit input at 40 steps is clean (above); an 8-bit input
  at 8 steps would confirm it.
- The exact size rule behind the header (height, or pixel count).
- How `draw-things-cli` scales a 16-bit PNG to its own values; run 2 of `duo` shows only that it reads one.
- The primaries and transfer: no file states them, and pixel statistics cannot recover them. The source frames are
  most likely sRGB (BT.709 primaries, sRGB transfer), which is an inference.
- The H.264 output's matrix by this test: its 4:2:0 chroma and lossy coding leave no 8-bit structure to find. The
  BT.709 decode for untagged H.264 rests on a comparison with the input image
  ([Phase 2 changelog](../archive/phase-2/phase-2-changelog.md)).
