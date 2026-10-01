# Color through the chain

Where color is lost or biased on the way from the first image to the last run of a chain, what the model itself adds,
and what can be done about each, for [Phase 3, Milestone 09](../phase-3/milestone-09-color-preservation.md).
Researched 2026-09-30.

Nothing was run or measured for this note (owner decision: plan only, no experiments); the measurements it calls for
are the milestone's first step. The owner's E0017 chain was measured from its files on 2026-10-01
([Measured on E0017](#measured-on-e0017)). It rests on:

- This repository's code, as of 2026-09-30.
- The source of Draw Things, [`drawthingsai/draw-things-community`](https://github.com/drawthingsai/draw-things-community)
  at `0e9c1805eeb2898b23249e2764bbdbe8670e3461` (2026-09-29), and the PNG library it pins,
  [`kelvin13/swift-png`](https://github.com/kelvin13/swift-png) at `075dfb248ae327822635370e9d4f94a5d3fe93b2`. The
  installed `draw-things-cli` may be another build; that it matches is not verified.
- Pillow 12.3.0, ffmpeg 8.1.1, and macOS 26.7.1, as installed.
- Wan 2.2's reference configuration and published work on guidance and color transfer (linked where used).

Each claim is marked by what backs it: **source** (read in code), **simulated** (numbers from a model of the code,
with its assumptions), **measured** (from [the ProRes note](prores-color-matrix.md) or [E0017](#measured-on-e0017)),
or **hypothesis** (not tested).

## Summary

| # | Stage | What happens to color | Backed by |
|---|-------|-----------------------|-----------|
| 1 | First image, read by `dtc` | Any ICC profile is converted to sRGB by LittleCMS (relative colorimetric, black point compensation) into 8 bits; colors outside sRGB, such as Display P3's saturated reds and greens, are clipped per channel, which shifts their hue as well as their chroma. A 16-bit RGB PNG is read by Pillow as its high bytes: truncated, about 0.5 level dark | source |
| 2 | Resize | Linear-light Lanczos when downscaling, sRGB values when upscaling, rounded once; the mean color is checked within 1 level | source, and the resize check |
| 3 | No resize | The input goes to `draw-things-cli` as it is: a PNG is read with no color management at all, anything else through CoreGraphics | source |
| 4 | `draw-things-cli` reads a PNG | swift-png, which ignores `iCCP`, `sRGB`, `gAMA`, and `cHRM`: the values are taken as sRGB. 8-bit samples are exact (`v / 127.5 - 1`); 16-bit samples are cut to 8 bits by `>> 8`. A PNG of another size is resampled nearest-neighbour | source |
| 5 | The model | VAE encode, 40 steps of the two Wan 2.2 experts, VAE decode. Chained i2v is known to drift in contrast, saturation, and hue; this project's configuration uses a higher guidance scale than Wan's reference. In E0017, chroma x1.16 and hue +5° toward yellow after 3 runs, contrast held ([measured](#measured-on-e0017)) | literature; the configuration; measured |
| 6 | Draw Things' `colorCalibration` | Off in the configuration (`none`). When `lab`, every frame is matched to the input image, see [below](#draw-things-colorcalibration) | source |
| 7 | `draw-things-cli` quantizes each frame | `Int((v + 1) * 127.5)`: truncated, not rounded, so every frame of every run is 0.5 level dark on average | source |
| 8 | `draw-things-cli` encodes | AVAssetWriter, from 8-bit BGRA pixel buffers with no color attachments and no `AVVideoColorPropertiesKey`: VideoToolbox encodes BT.709 and labels the matrix by frame size | source; measured |
| 9 | `dtc` decodes the last frame | The measured matrix, limited to full range, 16-bit RGB rescaled by 257/256: within 0.01 level of an exact decode of the file, until the change built the same day ([the handoff](#the-handoff)) | measured |
| 10 | The handoff | The 16-bit last frame is read back by stage 4's `>> 8`. With stage 7, each handoff was 0.89 level dark in the shadows, 0.50 in the midtones, and 0.10 in the highlights, until the change built the same day ([the handoff](#the-handoff)) | simulated |
| 11 | Chain | If the model reproduces its conditioning frame's tone, stage 10 compounds: about 9 levels darker in the shadows and 5 in the midtones after 10 runs, before any drift of the model's own. Not measured for that handoff; with the one built since, E0017's handoffs added nothing and the model's own drift compounded ([measured](#measured-on-e0017)) | hypothesis; measured |

Stages 7 and 10 are cheap to fix and need no correction of the picture: decode Draw Things' frames half a level up,
and hand off a value `draw-things-cli` reads exactly, rounded so flat areas are unbiased too; that was built the same
day ([The handoff](#the-handoff)). Stages 1 and 3 are fixed by always handing
`draw-things-cli` an 8-bit sRGB PNG of the exact size, made with gamut mapping. Stage 5 is what the correction is for.

## The first image

`jobs/inputs/resize.py`'s `to_srgb` converts an embedded profile to sRGB with LittleCMS's relative colorimetric
intent and black point compensation, into 8-bit RGB. In-gamut colors are converted exactly. Out-of-gamut colors are
clipped channel by channel, which moves them toward the gamut's corners: a saturated P3 red loses chroma and turns
toward orange, and neighbouring shades collapse into one. Phone photos are usually Display P3.

Pillow reads a 16-bit RGB PNG with the raw mode `RGB;16B`
([`PngImagePlugin`](https://github.com/python-pillow/Pillow/blob/main/src/PIL/PngImagePlugin.py): `(16, 2): ("RGB",
"RGB;16B")`), whose unpacker keeps the first, high byte of each sample
([`Unpack.c`](https://github.com/python-pillow/Pillow/blob/main/src/libImaging/Unpack.c), `unpackRGB16B`). That is a
truncation: 0.5 level dark on average. 16-bit grayscale is scaled with rounding by `_to_8_bit_gray`.

When a job resizes, `draw-things-cli` gets the PNG `resize_image` wrote: 8-bit sRGB values, rounded once, exact size.
When it does not, the input goes as it is, and the input check warns unless it is plain 8-bit sRGB RGB.

## How `draw-things-cli` reads an input

`Apps/DrawThingsCLI/DrawThingsCLI.swift`: `loadInputImageTensor` calls `loadTrainingTensor`, which tries swift-png
first and falls back to ImageIO.

- **PNG (swift-png).** `PNGFile.read`, then `unpack(as: PNG.RGBA<UInt8>.self)`. No color chunk is read, so an
  embedded profile is ignored and a Display P3 PNG is taken as sRGB (it looks duller). The size is fitted by
  aspect-preserving scale and center crop, sampled nearest-neighbour (`Int(Double(x + offsetX) * ...)`). Each
  sample becomes `Float(v) / 127.5 - 1`.
- **16-bit PNG.** swift-png's `convolve` narrows a sample by `$0 &>> shift` (`Sources/png/convolution.swift`), so
  16 to 8 bits keeps the high byte. The low byte, which the 16-bit last frame was made to carry, is dropped, and the
  truncation biases the value.
- **Anything else (JPEG, HEIC, TIFF).** ImageIO decodes, and CoreGraphics draws into an 8-bit
  `CGColorSpaceCreateDeviceRGB()` context with `.high` interpolation. CoreGraphics converts from the image's profile
  to that space and resamples in gamma-encoded values. What "DeviceRGB" means for a bitmap context on this macOS was
  not verified.

So `draw-things-cli` sees 8 bits whatever it is given, sees PNG values raw, and does the least to a PNG of exactly
the generation size. The tool can decide every value the model sees by always handing it such a PNG.

## How `draw-things-cli` writes a video

`writeVideo` fills `kCVPixelFormatType_32BGRA` pixel buffers from the frames (`populate`) with `pixelByte`:

```swift
private func pixelByte(_ value: FloatType) -> UInt8 {
  UInt8(min(max(Int((value + 1) * 127.5), 0), 255))
}
```

`Int` truncates, so each value lands on the level below it: 0.5 level dark on average, and 255 only for an exact
1.0. The same function writes PNG output. Alpha is set to 255 (the 4080 of 4095 the ProRes note found). The ProRes
and H.264 settings pass only the codec and size, no color properties, so the matrix label is VideoToolbox's own
choice, which fits the frame-size rule in [the ProRes note](prores-color-matrix.md#the-owners-chain-and-the-frame-size).

## The handoff

A model of stages 7 to 10: the model's value is uniform within each level; `draw-things-cli` truncates it; the ProRes
round trip adds noise with a standard deviation of 0.25 level ([the ProRes note](prores-color-matrix.md) measured a
mean error of 0.18); `dtc` decodes the file exactly; `draw-things-cli` reads the handoff back. Mean error of what the
next run sees, in 8-bit levels, by tone:

| Handoff | 0-31 | 32-95 | 96-159 | 160-223 | 224-255 |
|---------|------|-------|--------|---------|---------|
| Today: 16-bit, rescaled by 257/256, read by `>> 8` | -0.89 | -0.67 | -0.50 | -0.33 | -0.10 |
| 16-bit as ffmpeg writes it (about 256 times), read by `>> 8` | -0.98 | -1.00 | -1.00 | -1.00 | -1.00 |
| 8-bit, rounded | -0.50 | -0.50 | -0.50 | -0.50 | -0.50 |
| 8-bit, rounded from the decode plus 0.5 level | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

The rescale was chosen for a reader that divides by 65535 (Milestone 08); `draw-things-cli` shifts instead, which
turns the rescale into a tone-dependent error: shadows darker, highlights less so, a small contrast increase at each
handoff. Without the rescale it is a flat -1 level. Only an 8-bit value that has been rounded, after undoing the
truncation, is unbiased. A 16-bit file can be made unbiased for this reader too, by writing the rounded 8-bit value
`v` as `v * 256 + 128`, but it then carries nothing a plain 8-bit file does not.

The table's model gives every sample some decode noise, which is what spreads the rounding over both neighbouring
levels. In a flat area there is none to speak of: measured on ProRes 4444 and H.264 made from flat 8x8 blocks at
every 8-bit level, rounding `e + 0.5` puts every sample of a block on the same side, half a level off at every level
(it averages to 0.05 only across many levels). A 2x2 ordered dither (thresholds 1/8, 5/8, 7/8, 3/8 of a level) brings
ProRes to 0.04 level per block, 0.25 at most; for H.264, the 0.29 left is its own 8-bit YCbCr error. Adding the half
level to a black sample would lift every clipped black to 1, so the half is tapered to nothing below one level.

Also measured with ffmpeg 8.1.1: swscale decodes 8-bit YCbCr (H.264) to 16-bit RGB 0.8 to 1.6 levels dark unless
asked for `accurate_rnd` (with `full_chroma_int`), which brings it to within 0.03; 8-bit output, and ProRes to 16-bit,
are unbiased either way.

**What was built (owner decisions, 2026-09-30):** the 16-bit handoff with `v * 256 + 128`, from every source, `v` the
decoded level plus the tapered half level, rounded against the ordered dither, decoded with accurate rounding; see
[Milestone 08](../phase-3/milestone-08-video-format-and-color.md#the-handoff).

Whether the bias compounds depends on how faithfully Wan reproduces its conditioning frame's tone through the VAE.
Run 2 of the owner's `duo` chain (execution E0016) could have told for the handoff before 2026-09-30, but its clips
are gone. E0017, made with the handoff built, measures what Wan does to its conditioning frame
([Measured on E0017](#measured-on-e0017)).

## The model's own drift

Chained image-to-video drifts even with an exact handoff: each run starts from a generated frame, and errors in
contrast, saturation, and white balance feed the next run. Reports for Wan:
[Wan2.2 #172](https://github.com/Wan-Video/Wan2.2/issues/172) (the color shifts when a new clip starts from the last
frame, and color matching did not bring it back) and
[ComfyUI-WanVideoWrapper #1541](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1541) (contrast grows over
time when looping). The usual remedy is matching each clip to a reference image with a global color transfer, as
ComfyUI-KJNodes' [Color Match](https://comfyui-wiki.com/en/custom-nodes/ComfyUI-KJNodes/nodes/color-match) does with
the `color-matcher` library (`mkl`, `hm`, `reinhard`, `mvgd`, and their compounds). A global transfer cannot tell a
color drift from a change of content: when the camera turns to a red wall, it pulls the wall toward the reference.
#172's report that matching did not fix the shift fits that.

Guidance drives saturation. High classifier-free guidance oversaturates, mostly through the component of the
guidance update parallel to the conditional prediction
([Sadat et al., 2024](https://arxiv.org/abs/2410.02416), adaptive projected guidance), and CFG-Zero\*
([Fan et al., 2025](https://arxiv.org/abs/2503.18886)) improves guidance for flow-matching models, Wan 2.1 among
them, with an optimized scale and zeroed first steps. Draw Things has CFG-Zero\* (`cfgZeroStar`,
`cfgZeroInitSteps`), off in this project's configuration; it has no projected guidance.

This project's Wan 2.2 configuration (`data/params/image-to-video-wan-2-2-default-i8x.yaml`) against Wan 2.2's
reference for A14B image-to-video ([`wan/configs/wan_i2v_A14B.py`](https://github.com/Wan-Video/Wan2.2/blob/main/wan/configs/wan_i2v_A14B.py)):

| Setting | Configuration | Wan reference | Can a job override it today |
|---------|---------------|---------------|-----------------------------|
| Guidance | `guidanceScale: 5` | 3.5 for both experts | yes, `guidance_scale` |
| Shift | `shift: 3.99` | 5.0 | yes, `shift` |
| Steps | 40 | 40 | yes, `steps` |
| Expert switch | `refinerStart: 0.1` | boundary 0.900 (in timestep) | yes, `refiner_start` |
| Sampler | 17, UniPC Trailing | UniPC | no |
| CFG-Zero\* | off | - | no |
| `colorCalibration` | `none` | - | no |
| `sharpness`, `teaCache` | 0, off | - | no (both fine for color) |

A guidance scale of 5 against 3.5 is the most likely model-side contributor to saturation and contrast creep; that
it is, in this chain, is a hypothesis.

## Draw Things' `colorCalibration`

`Libraries/LocalImageGenerator/Sources/ColorCalibrator.swift`, applied in `upscaleImageAndToCPU` to the decoded
frames when `colorCalibration` is not `disabled`; the values are `none` and `lab`. In `generateTextOnly` the
reference is the input image (`colorCalibrationReference ?? image`). For each frame:

1. **Wavelet reconstruction** (StableSR's wavelet color fix, `ccv_wavelet_decompose` in
   [`liuliu/ccv`](https://github.com/liuliu/ccv/blob/unstable/lib/ccv_wavelet.c)): five à-trous blurs of radius 1, 2,
   4, 8, and 16 pixels (capped at an eighth of the shorter side) split the frame and the reference into detail and
   base. The frame's detail is added to the reference's base. The base is the reference's, at the reference's
   positions.
2. **Histogram matching in CIELAB** (D65), 4096 bins per channel, from the reconstructed frame to the reference: `a`
   and `b` are replaced by their matched values, `L` moves a fifth of the way (`0.8 * l0 + 0.2 * matched`).

With a video, every frame is matched to the one input image, and its large-scale color and brightness come from the
input at the input's positions. On a locked camera with little motion, that holds color very tightly. With motion it
leaves a ghost of the first frame's shading, and it removes any intended change of light within the run. It is the
"previous run" anchor, applied inside Draw Things, at full strength. That the i2v path of Wan 2.2 reaches this call
is read from the code, not verified, and a job cannot set it today.

## Regions with Apple Vision

Vision has no skin segmentation. What it has, through
[`pyobjc-framework-Vision`](https://pypi.org/project/pyobjc-framework-Vision/) (12.2.2, wheels for CPython 3.12 to
3.14, universal2):

| Request | Since | Gives |
|---------|-------|-------|
| `VNGeneratePersonSegmentationRequest` | macOS 12 | One soft mask of every person; quality `fast`, `balanced`, or `accurate` |
| `VNGeneratePersonInstanceMaskRequest` | macOS 14 | A mask per person, up to four |
| `VNDetectFaceLandmarksRequest` | macOS 10.13 | Per face: contour, eyes, brows, nose, lips, as normalized points |

A skin mask can be built from these: the face's own skin gives a sample of this chain's skin colors, a Gaussian over
those colors in Oklab gives a likelihood, and the person mask confines it. `faceContour` runs from one cheek over the
chin to the other, open at the top, so the face's skin is taken as the hull of the contour and the brows, less the
eyes, the brows, and the lips. Landmark points are relative to the face's bounding box, with the origin at its lower
left ([`VNFaceLandmarks2D`](https://developer.apple.com/documentation/vision/vnfacelandmarks2d)). That needs no model weights, runs on the Neural Engine, and follows each person's own skin
rather than a fixed skin-color range. Draw Things itself runs only on Apple platforms, so a macOS-only dependency
costs this project nothing.

## Vision on generated frames

Measured on 2026-10-01, with no GPU run, on E0017's run 3 and E0021's run 2 (`duo`, 81 frames each at 832x448, two
people, both faces toward the camera), with pyobjc 12.2.2 on macOS 26.7.1.

- **Speed.** Person segmentation (`balanced`) and face landmarks together, from PNG bytes: about 0.95 s for the first
  request of a process, then 16 to 17 ms a frame (20 ms at most), 1.3 to 1.4 s for a clip. With the PNG encode, the
  Oklab conversion, the skin model, and the region statistics, 64 ms a frame (178 ms at most), 5.3 s for a clip. The
  correction of E0017's run 3 with regions took 15.4 s in all, against 10.5 s without, under its limit of 91 s.
- **The mask** comes back 8-bit (`kCVPixelFormatType_OneComponent8`, `L008`) at 512x384 whatever the frame's shape,
  stretched over the whole frame: 1.63 times across and 1.17 times down for 832x448. Rows are read past their padding
  (`bytesPerRow`). Stretched back, it follows both people closely; a small dark object behind them is taken in.
- **Faces.** Both faces in all 81 frames of both clips, every landmark region present (contour, eyes, brows, nose,
  outer lips), the lower face turned about 90°. `pointsInImageOfSize_` gives image pixels with the origin at the lower
  left, so y is turned over for the frame's rows; drawn over the frames, the points then sit on the faces.
- **pyobjc.** The requests' `init` is unavailable (`NS_UNAVAILABLE`) in 12.2.2; `initWithCompletionHandler_(None)`
  makes them. Without an autorelease pool around each call, a thread's memory grew about 1 MB a call (2 GB after 2000
  calls); with one, 122 MB to 155 MB over 2000 calls, slowing (+5, +8, +1, and +3.6 MB in the last four steps of 400
  calls), on a thread as `dtc serve`'s worker runs jobs.
- **Skin.** A Gaussian of the face's skin over Oklab's `L`, `a`, and `b` kept the faces but dropped lit and shaded
  body skin, since a face spans a narrow range of light. Over `a` and `b` only, it covers the faces and the body's
  skin, leaves the clothes and most hair out, and drops only some highlight edges, which the feathering softens.
  Divided by `L` (`a/L`, `b/L`), it took in more hair. Skin was 16 to 23% of the frame with `L`, 24 to 29% without.
- **What regions do on E0017's run 3** (`blend` 0.25 toward `duo.png`). Its drift sits mostly in the background:
  since the first image, the background's chroma x1.20 and hue +11°, people's x0.99 and +3°, skin's +2°. Corrected
  over the whole frame, people were taken too far: left within the run, people's chroma x0.93 and hue -1.8°, skin's
  `L` -3.4 and hue -3.2°. With regions, people's chroma x0.99 and hue +0.5°, skin's `L` -0.2 and hue -0.7°. The
  background, corrected by the whole frame's transform as planned, kept its hue +6.6° within the run; by its own
  (owner decision, 2026-10-01), +6.1°, its hue cap (6° a run) binding in 71 of 81 frames. Its chroma is about 0.015,
  so that turn is a small change of color.

## Color space for measuring and correcting

[Oklab](https://bottosson.github.io/posts/oklab/) (Ottosson, 2020): lightness `L`, and `a`, `b`, from linear sRGB
through two 3x3 matrices and a cube root. Chroma is `C = sqrt(a² + b²)` and hue `h = atan2(b, a)`. It predicts
lightness, chroma, and hue better than CIELAB for its cost, keeps hue nearly constant when chroma changes (CIELAB
bends blue toward purple), and its Euclidean distance works as a color difference (`ΔE_OK`, about 0.02 for a
just-noticeable difference). Brightness, contrast, saturation, and hue, the owner's four drifts, are then its `L`
median, `L` spread, `C`, and `h`.

## Checked in Milestone 09's step 0

Checked on 2026-09-30, before any code:

- **The installed `draw-things-cli`** (`--version` prints `dev`) was built on 2026-09-23 from a local checkout of
  `draw-things-community` at `da9b0c8` (2026-09-22), with no tracked file changed, not from `0e9c180`. Its
  `pixelByte`, the PNG branch of `loadTrainingTensor`, and its `swift-png` pin (`075dfb2`) are the same as this note
  read, so what this note says of them holds for the installed build.
- **The generation settings.** The JSON override names are `cfgZeroStar` (a Bool), `cfgZeroInitSteps` (an Int32),
  and `colorCalibration` (a string). `lab` turns calibration on, and any other value, `none` included, turns it off
  (`Libraries/Scripting/Sources/ScriptModels.swift`). All three names are in the binary. Of the configuration files,
  the `image-to-video-wan-2-2.example` pair writes all three; `image-to-video-wan-2-2-default-i8x.yaml` writes
  `cfgZeroStar: false` and `colorCalibration: none`, and no `cfgZeroInitSteps`.
- **The corrected copy's encoder.** ffmpeg 8.1.1's `prores_videotoolbox -profile:v 4444` takes `p416le` and writes
  ProRes 4444 (`ap4h`, `yuv444p12le`). A 17-frame 832x448 test pattern went through it and came back, BT.709 limited
  range, within 0.005 level on average (0.023 mean absolute). Its pixels measure BT.709 (0.047 against 0.202 for
  BT.601). `prores_ks` from `yuv444p10le` came back within 0.030 (0.136 mean absolute). The encoder writes the
  matrix and range into the frame header but not the primaries or the transfer, so the `colr` box states them.
  `h264_videotoolbox` and `hevc_videotoolbox` both encode from `nv12`.
- **Upstream reports** on `pixelByte` and the 16-bit read are drafted in
  [draw-things-upstream-reports.md](draw-things-upstream-reports.md), for the owner to file.

Step 1's measurement of the handoff on E0016 is deferred (owner decision): that chain's clips are no longer on disk.
The `color_drift` check measures every run from Milestone 09 on. E0017, made before the check was built, is measured
[below](#measured-on-e0017).

## Measured on E0017

Measured on 2026-10-01 from the files on disk, with no GPU run. The owner's `duo` job, execution E0017 (2026-09-30):
3 runs of 81 frames at 832x448, ProRes 4444, `image-to-video-wan-2-2-default-i8x.yaml`, seed 720708700, no resize.
Run 1 starts from `duo.png` (8-bit sRGB), runs 2 and 3 from the previous run's last frame. The runs used
[the handoff](#the-handoff) built on 2026-09-30. They finished before the correction was committed (`58ce65c`), so
nothing was corrected and the state store has no `color_drift` check for them.

Frames are decoded as `dtc` decodes them: `decode_filter` with the matrix `resolve_video_color` measures (BT.709,
limited range, in all three), plus the tapered half level (`decoded_levels`). Inputs are read as `draw-things-cli`
reads them: `duo.png`'s 8-bit values, a 16-bit PNG's high bytes. Statistics are `color_stats.measure` and `compare`,
as the `color_drift` check computes them; the check itself, run over these files, gives the same numbers. `L`, `a`,
and `b` are in hundredths of Oklab's, and levels are 8-bit.

### The files and the handoff

- **The handoff is exact.** Each last-frame PNG equals, sample for sample, the handoff rebuilt from frame 80 with
  `srgb_filter` (frame 79 matches 21 to 22% of the samples), and every sample's low byte is 128. What
  `draw-things-cli` read at each handoff was the decoded last frame, rounded as designed.
- **The frame header states another matrix.** Every frame states `smpte170m`. The pixels measure BT.709 (0.225
  against 0.246 to 0.249), and the `colr` box states BT.709 primaries and matrix and the sRGB transfer, as
  [the ProRes note](prores-color-matrix.md#the-owners-chain-and-the-frame-size) found at 832x448. `dtc` decodes with
  the measured matrix, so the chain is not affected. Decoding the last frame as its header states, which ffmpeg does
  when not told otherwise, moves it by ΔE_OK 0.005 on average (0.012 at the 95th percentile, 0.030 at most): red
  -1.2, green -0.5, blue +0.6 levels, chroma x0.985. That is about one run's drift, so a comparison made with such a
  decode can pass for drift.
- **Nothing else is off.** Alpha is 4080 in every frame. Luma spans 247 to 3770 at 12 bits, under a level past
  limited range's 256 to 3760, which the decode clips. The 81 frames last 37 or 38 units of a 600 timescale, 16.003
  frames a second on average.

### Frame 0 against its input

Frame 0 is the model's regeneration of its input, in place, so the two are compared pixel for pixel. Banding single
pixels by the input's value pulls the outer bands toward the mean by frame 0's texture error alone (3.2 levels mean
absolute per pixel), so the tone bands are of 16x16 block means (1.6 to 1.7 levels mean absolute). The bands hold
about 5, 13, 26, 34, and 23% of the pixels. Frame 0 minus its input, red / green / blue, in levels:

| Run | Whole frame | 0-31 | 32-95 | 96-159 | 160-223 | 224-255 |
|-----|-------------|------|-------|--------|---------|---------|
| 1 | +0.75 / +0.19 / -0.24 | +2.2 / +1.4 / +1.6 | +2.0 / +1.5 / +1.3 | +1.7 / +0.7 / +0.1 | +0.4 / -0.2 / -0.9 | -0.2 / -1.0 / -1.8 |
| 2 | -0.11 / -0.67 / -1.03 | +0.7 / +0.2 / +0.1 | +0.9 / +0.2 / +0.1 | +0.0 / -0.5 / -0.8 | -0.3 / -1.0 / -1.6 | -0.2 / -1.1 / -1.8 |
| 3 | +0.05 / -0.76 / -1.28 | -0.4 / -0.8 / -0.4 | +0.2 / -0.4 / -0.8 | -0.3 / -1.2 / -1.7 | +0.0 / -0.7 / -1.1 | +0.4 / -0.5 / -1.5 |

Without the half level, each value is 0.45 to 0.5 lower. Per pixel, frame 0 is ΔE_OK 0.011 from its input on average
(0.032 to 0.033 at the 95th percentile), mostly lost detail. Its statistics against its input: `L` -0.0, -0.3, and
-0.4; contrast x0.99 to x1.00; chroma x1.04, x1.02, and x1.02; hue +0°.

So Draw Things' regeneration of its input (the VAE and the model, which the files cannot tell apart) takes 0.9 to 1.8
levels of blue and up to 1.1 of green from the upper midtones and highlights in every run, a shift toward yellow, and
raises chroma 2 to 4%. The shadows were lifted in runs 1 and 2, not in run 3. The -0.9 to -0.1 that
[the handoff](#the-handoff)'s model predicted was for the handoff before 2026-09-30, which E0017 did not use.

### Within each run

Frame 0 to frame 80, over the whole frame:

| Run | `L` | Contrast | Chroma | Hue |
|-----|-----|----------|--------|-----|
| 1 | +3.7 | x0.98 | x1.01 | +1° |
| 2 | +1.1 | x1.01 | x1.03 | +1° |
| 3 | +0.9 | x1.01 | x1.03 | +2° |

The `L` median does not move steadily within a run (run 1's is +4.7 at frame 60), and much of it is motion
([a still region](#a-still-region)).

### Over the chain

From `duo.png` (`L` median 0.729, chroma median 0.0214, hue 51.1°, mean RGB 174.6 / 161.5 / 150.1), over the whole
frame:

| Frame | `L` | Contrast | Chroma | Hue |
|-------|-----|----------|--------|-----|
| Run 1, frame 0 | -0.0 | x0.99 | x1.04 | +0° |
| Run 1, frame 80 | +3.7 | x0.97 | x1.05 | +2° |
| Run 2, frame 0 | +3.4 | x0.96 | x1.08 | +2° |
| Run 2, frame 80 | +4.5 | x0.97 | x1.10 | +3° |
| Run 3, frame 0 | +4.1 | x0.98 | x1.12 | +3° |
| Run 3, frame 80 | +5.0 | x0.99 | x1.16 | +5° |

Run 3's frame 80 has a mean RGB of 178.8 / 166.7 / 153.7. The last-frame PNGs, read by their high bytes, measure as
their frame 80 does. Samples clipped at 255 grow from 0.02% in `duo.png` to 0.04 to 0.08% in run 1, 0.11 to 0.18% in
run 2, and 0.37 to 0.38% in run 3; samples clipped at 0 stay between 0.1 and 0.45%, with no trend. The `color_drift` check, with its proposed limits (`L` 3, ratios
0.10, hue 5°), warns on all three runs since the first image (`L` +3.7, +4.5, and +5.0), and on run 1 within the run
(`L` +3.7).

### A still region

The camera is locked, so the pixels that change least over all 243 frames can be compared in place, free of motion:
the tenth of the pixels with the lowest temporal standard deviation (under 4.3 levels). They are one bright,
near-neutral area (`L` 0.90, chroma 0.016), not skin. Pixels that drift more vary more and are left out, so this is a
lower bound. Hue means little at that chroma, so `b` stands for it. The region's mean color, from `duo.png`'s:

| Frame | ΔE_OK | `L` | `a` | `b` | Chroma | RGB, levels |
|-------|-------|-----|-----|-----|--------|-------------|
| Run 1, frame 0 | 0.0015 | -0.12 | +0.05 | +0.08 | x1.01 | +0.2 / -0.5 / -1.0 |
| Run 1, frame 80 | 0.0065 | +0.63 | -0.03 | +0.15 | x1.06 | +2.3 / +2.1 / +1.0 |
| Run 2, frame 0 | 0.0048 | +0.44 | +0.02 | +0.19 | x1.07 | +2.1 / +1.4 / +0.0 |
| Run 2, frame 80 | 0.0098 | +0.94 | -0.08 | +0.25 | x1.12 | +3.2 / +3.3 / +1.3 |
| Run 3, frame 0 | 0.0103 | +0.98 | -0.02 | +0.32 | x1.15 | +4.0 / +3.2 / +0.9 |
| Run 3, frame 80 | 0.0134 | +1.28 | -0.19 | +0.35 | x1.19 | +3.9 / +4.6 / +1.7 |

Per pixel, detail and noise add a floor: the same region is ΔE_OK 0.007 from `duo.png` on average at run 1's frame 0,
and 0.020 at run 3's frame 80.

### What E0017 shows

- **The handoff adds nothing of its own.** `draw-things-cli` read exactly the handoff as designed; what changes at
  frame 0 is Draw Things' own.
- **The model's drift compounds, in one direction.** Chroma rises about 5% a run (x1.04, x1.08, and x1.12 at frame 0;
  x1.16 at the end), hue turns about 1.7° a run toward yellow (51.1° to 56.2°), and blue falls behind red and green.
  Contrast holds (x0.96 to x0.99); white clipping grows, still under 0.4%.
- **Where it comes from.** Chroma grows at the handoffs and within the runs about equally. Lightness rises within the
  runs (the still region: +0.75, +0.50, and +0.30) and not at the handoffs (-0.19 and +0.04). Yellowing comes from
  both (`b` +0.04 and +0.07 at the handoffs; +0.07, +0.06, and +0.03 within the runs).
- **Whole-frame lightness is mostly motion.** Over the chain, the whole frame's `L` rose 5.0 and the still region's
  1.3. Run 1's +3.7 within the run, on which the check warns, is +0.75 in the still region.
- **Size.** After three runs the still region's mean color is ΔE_OK 0.013 from the first image, about two thirds of a
  just-noticeable difference. Chroma, +16% over the frame, is the largest change.

## Not verified

- That the installed `draw-things-cli` is built from the source read here. Checked: it is not, but the parts this
  note reads are the same ([above](#checked-in-milestone-09s-step-0)).
- How far Wan reproduces its conditioning frame's tone and color through the VAE; so, whether the handoff bias
  compounds. Checked on one chain and seed, with the handoff built on 2026-09-30: the handoff adds nothing, and the
  model's own drift compounds ([E0017](#measured-on-e0017)). The bias of the handoff before it was not measured.
- What CoreGraphics does to a non-PNG input drawn into a `DeviceRGB` context.
- That the Wan 2.2 i2v path reaches `ColorCalibrator`, and how strong the ghost of `lab` is with motion.
- The effect on drift of guidance 3.5, of CFG-Zero\*, and of `colorCalibration: lab`.
- Vision's mask quality on generated frames, and its speed at 832x448. Checked on two clips of one scene
  ([above](#vision-on-generated-frames)); not on other scenes, a single person, faces turned away, or other sizes.
- That ffmpeg's `prores_videotoolbox` writes ProRes 4444 from 16-bit 4:4:4 (`p416le`); its help lists the pixel
  format, not the profile it allows. `prores_ks` takes at most 10 bits (`yuv444p10le`, `yuva444p10le`). Checked: it
  does ([above](#checked-in-milestone-09s-step-0)).
