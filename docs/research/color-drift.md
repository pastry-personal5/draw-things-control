# Color through the chain

Where color is lost or biased on the way from the first image to the last run of a chain, what the model itself adds,
and what can be done about each, for [Phase 3, Milestone 09](../phase-3/milestone-09-color-preservation.md).
Researched 2026-09-30.

Nothing was run or measured for this note (owner decision: plan only, no experiments); the measurements it calls for
are the milestone's first step. It rests on:

- This repository's code, as of 2026-09-30.
- The source of Draw Things, [`drawthingsai/draw-things-community`](https://github.com/drawthingsai/draw-things-community)
  at `0e9c1805eeb2898b23249e2764bbdbe8670e3461` (2026-09-29), and the PNG library it pins,
  [`kelvin13/swift-png`](https://github.com/kelvin13/swift-png) at `075dfb248ae327822635370e9d4f94a5d3fe93b2`. The
  installed `draw-things-cli` may be another build; that it matches is not verified.
- Pillow 12.3.0, ffmpeg 8.1.1, and macOS 26.7.1, as installed.
- Wan 2.2's reference configuration and published work on guidance and color transfer (linked where used).

Each claim is marked by what backs it: **source** (read in code), **simulated** (numbers from a model of the code,
with its assumptions), **measured** (from [the ProRes note](prores-color-matrix.md)), or **hypothesis** (not tested).

## Summary

| # | Stage | What happens to color | Backed by |
|---|-------|-----------------------|-----------|
| 1 | First image, read by `dtc` | Any ICC profile is converted to sRGB by LittleCMS (relative colorimetric, black point compensation) into 8 bits; colors outside sRGB, such as Display P3's saturated reds and greens, are clipped per channel, which shifts their hue as well as their chroma. A 16-bit RGB PNG is read by Pillow as its high bytes: truncated, about 0.5 level dark | source |
| 2 | Resize | Linear-light Lanczos when downscaling, sRGB values when upscaling, rounded once; the mean color is checked within 1 level | source, and the resize check |
| 3 | No resize | The input goes to `draw-things-cli` as it is: a PNG is read with no color management at all, anything else through CoreGraphics | source |
| 4 | `draw-things-cli` reads a PNG | swift-png, which ignores `iCCP`, `sRGB`, `gAMA`, and `cHRM`: the values are taken as sRGB. 8-bit samples are exact (`v / 127.5 - 1`); 16-bit samples are cut to 8 bits by `>> 8`. A PNG of another size is resampled nearest-neighbour | source |
| 5 | The model | VAE encode, 40 steps of the two Wan 2.2 experts, VAE decode. Chained i2v is known to drift in contrast, saturation, and hue; this project's configuration uses a higher guidance scale than Wan's reference | literature; the configuration |
| 6 | Draw Things' `colorCalibration` | Off in the configuration (`none`). When `lab`, every frame is matched to the input image, see [below](#draw-things-colorcalibration) | source |
| 7 | `draw-things-cli` quantizes each frame | `Int((v + 1) * 127.5)`: truncated, not rounded, so every frame of every run is 0.5 level dark on average | source |
| 8 | `draw-things-cli` encodes | AVAssetWriter, from 8-bit BGRA pixel buffers with no color attachments and no `AVVideoColorPropertiesKey`: VideoToolbox encodes BT.709 and labels the matrix by frame size | source; measured |
| 9 | `dtc` decodes the last frame | The measured matrix, limited to full range, 16-bit RGB rescaled by 257/256: within 0.01 level of an exact decode of the file, until the change built the same day ([the handoff](#the-handoff)) | measured |
| 10 | The handoff | The 16-bit last frame is read back by stage 4's `>> 8`. With stage 7, each handoff was 0.89 level dark in the shadows, 0.50 in the midtones, and 0.10 in the highlights, until the change built the same day ([the handoff](#the-handoff)) | simulated |
| 11 | Chain | If the model reproduces its conditioning frame's tone, stage 10 compounds: about 9 levels darker in the shadows and 5 in the midtones after 10 runs, before any drift of the model's own | hypothesis |

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

Whether the bias compounds depends on how faithfully Wan reproduces its conditioning frame's tone through the VAE,
which is not measured. Run 2 of the owner's `duo` chain (execution E0016) can tell without a GPU: its frame 0,
banded by tone, against run 1's 16-bit last frame, which was its input. The model above predicts -0.9 in the shadows
to -0.1 in the highlights, plus whatever the VAE adds.

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
The `color_drift` check measures every run from Milestone 09 on.

## Not verified

- That the installed `draw-things-cli` is built from the source read here. Checked: it is not, but the parts this
  note reads are the same ([above](#checked-in-milestone-09s-step-0)).
- How far Wan reproduces its conditioning frame's tone and color through the VAE; so, whether the handoff bias
  compounds.
- What CoreGraphics does to a non-PNG input drawn into a `DeviceRGB` context.
- That the Wan 2.2 i2v path reaches `ColorCalibrator`, and how strong the ghost of `lab` is with motion.
- The effect on drift of guidance 3.5, of CFG-Zero\*, and of `colorCalibration: lab`.
- Vision's mask quality on generated frames, and its speed at 832x448.
- That ffmpeg's `prores_videotoolbox` writes ProRes 4444 from 16-bit 4:4:4 (`p416le`); its help lists the pixel
  format, not the profile it allows. `prores_ks` takes at most 10 bits (`yuv444p10le`, `yuva444p10le`). Checked: it
  does ([above](#checked-in-milestone-09s-step-0)).
