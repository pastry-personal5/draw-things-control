# Milestone 03: First Input Image Resize

**Phase:** [Phase 1: Basic Functionalities](README.md)
**Status:** done
**Depends on:** [Milestone 01: Job definition and chained batch runs](milestone-01-job-definition-batch.md)

## Goal

Let an `i2v` or `i2i` job start from an input image of any size. The job asks
for a size with two new root-level keys, `desired_input_width` and
`desired_input_height`. Before run 1, the app resizes the first input image to
that size (rounded down to multiples of 64), keeping the input's aspect ratio
(it is never stretched), and generates every run at that size.

## Scope

In scope:

- New optional root-level job keys `desired_input_width` and
  `desired_input_height` (integers), for `i2v` and `i2i` jobs
- New optional root-level job key `max_input_crop_percent`, the largest share
  of the input that may be cropped when one size key is given
- Computing the target size from the keys and the input image's aspect ratio
- Fitting the first input to the target size without distortion, into a
  temporary PNG used by run 1 only: cropped to cover the target when one key
  is given, letterboxed when both are given
- Writing an upright temporary copy of an EXIF-rotated input, even when its
  size already matches
- Passing the target size as `--width` / `--height` to every run, and ignoring
  `width` / `height` from `config_override` and `config_file`, with an INFO
  message
- Reporting the computed size in `validate-job`, `run-job --dry-run`, and
  `run-job`, and recording the resize in the job manifest

Out of scope:

- `t2v` jobs, which have no input image (the keys are an error there)
- Stretching, or choosing the fit per job; the fit follows from which keys
  are set
- Configurable pad color; padding is always black
- An area limit (`max_pixels`) or `size_from_input`
- Resizing later runs' inputs: every later input is a previous output, which
  is already the generation size
- Keeping the resized image after run 1
- The one-off `generate` command
- Any change to jobs that set neither `desired_input_*` key

## Job file keys

```yaml
mode: i2v
input: photo.jpg                       # 1920x1080
desired_input_width: 850               # optional; 1 to 8192
# desired_input_height: 470            # optional; 1 to 8192. With both keys set, the input is letterboxed instead of cropped
max_input_crop_percent: 10             # optional; only with exactly one desired_input_* key; default 10
```

- `desired_input_width` and `desired_input_height` are optional. Set one,
  both, or neither.
- Each value must be an integer from 1 to 8192 (not a boolean or a float).
- They are allowed only in `i2v` and `i2i` jobs. In a `t2v` job, either key is
  a validation error: `'desired_input_width' is not allowed in t2v jobs, which
  have no input image`.
- `max_input_crop_percent` is a number from 0 to 100 (0 allows no crop at
  all), default 10. It is allowed only when exactly one `desired_input_*` key
  is set, since only then is the input cropped; otherwise it is a validation
  error, for example: `'max_input_crop_percent' requires exactly one of
  desired_input_width or desired_input_height`.
- If neither size key is set, nothing changes: the Milestone 01 exact-size
  check applies, the input must already match the job's width and height, and
  the commands are the same as before this milestone.

## Target size

The app computes the target size once, when the job is loaded:

1. **Floor each given value to a multiple of 64.** 850 becomes 832, 470
   becomes 448, 8192 stays 8192, and 64 stays 64.
2. **Derive a missing value from the input's aspect ratio.** The app uses the
   input's displayed size (EXIF rotation applied, as in Milestone 01) and the
   floored given value, then floors the result to a multiple of 64:
   `height = floor64(width × input_height / input_width)`, and the reverse for
   a missing width.
3. **Reject a value under 64 or over 8192.** A given value above 8192, or a
   given or derived value that floors to 0, fails validation with exit code 2.
   The message names the key, the input, and the computed value, for example:

   ```
   data/pano.yaml: 'desired_input_height' derived from panorama.jpg (6000x400)
   and width 640 is 42.7, which floors to 0 (minimum 64)
   ```

   A derived value can exceed 8192 (for example, a very tall input with
   `desired_input_width: 8192`); it is rejected the same way.
4. **Check the crop (one key only).** The share cropped is the cropped pixels
   divided by the scaled image's size on the derived axis. If it exceeds
   `max_input_crop_percent`, validation fails with exit code 2:

   ```
   data/pano.yaml: 'max_input_crop_percent' is 10, but panorama.jpg (6000x400)
   at width 1600 scales to 1600x107 and would lose 43 px of 107 (40.2%) to
   reach 1600x64. Choose a larger desired_input_width, raise
   max_input_crop_percent, or set both keys to letterbox instead.
   ```

| Input | `desired_input_width` | `desired_input_height` | Target | Fit | Notes |
|-------|-----------------------|------------------------|--------|-----|-------|
| 1920x1080 | 850 | (none) | 832x448 | crop | 832 × 1080/1920 = 468, which floors to 448; scaled to 832x468, 10 px cropped top and bottom (4.3%) |
| 1920x1080 | (none) | 720 | 1216x704 | crop | 720 floors to 704; 704 × 1920/1080 = 1251.6, which floors to 1216; scaled to 1252x704, 18 px cropped left and 18 right (2.9%) |
| 1920x1080 | 1280 | 720 | 1280x704 | letterbox | Both given; no derivation; scaled to 1252x704, 14 px black bars left and right |
| 832x448 | 832 | 448 | 832x448 | none | Already the target and upright; the original file is used |
| 1664x896 | 832 | 448 | 832x448 | scale | Same aspect ratio as the target; scaled, no bars |
| 512x512 | 1024 | (none) | 1024x1024 | scale | Upscaled to exactly the target; nothing to crop |
| 6000x400 | 1600 | (none) | error | — | Height 106.7 floors to 64; 40.2% cropped, over the 10% limit |
| 6000x400 | 640 | (none) | error | — | Height 42.7 floors to 0 |
| any | 50 | (none) | error | — | Width 50 floors to 0 |
| any | 9000 | (none) | error | — | Above 8192 |

## Generation size

The target size is the job's generation size. Every run gets
`--width <target width> --height <target height>`, and the full
`--config-json` gets the same `width` and `height`, so the two never disagree.
Run 1's input then matches the output size, and every later run's input (the
previous output or last frame) does too.

`JobDefinition.size` holds the target size, and is `None` when neither
`desired_input_*` key is set. In that case the job service builds `--width`,
`--height`, and `--config-json` exactly as in Milestone 01.

When a `desired_input_*` key is set, `width` and `height` from
`config_override` and `config_file` are ignored. For each ignored value that
is present, an INFO message appears in `validate-job`, `run-job --dry-run`,
and `run-job` (and the job log), in the style of the `batchCount` message:

```
Ignoring config_override.width (832): desired_input_width/desired_input_height set the size (1280x704)
Ignoring height (448) from config_file image-to-video-wan-2-2.example.json: desired_input_width/desired_input_height set the size (1280x704)
```

The Milestone 01 rule that an `i2i` or `i2v` job needs a width and height from
`config_override` or `config_file` applies only when neither key is set.

## Resizing

In every case the input keeps its aspect ratio: both axes are scaled by the
same factor, up or down, with Lanczos resampling. It is never stretched. Which
fit is used depends on the keys:

- **One key given: crop (cover).** The target was derived from the input's
  aspect ratio, so it differs from it only by the rounding down to 64. The
  image is scaled to cover the target and the overflow is cropped, so there
  are no bars. The crop is on one axis only, is less than 64 px in total (plus
  at most 1 px from rounding the scaled size), and is limited by
  `max_input_crop_percent`.
- **Both keys given: letterbox (contain).** The target's aspect ratio is the
  user's choice and can be far from the input's, so the image is scaled to
  fit inside the target and the rest is padded with black. Nothing is cropped.

A temporary copy is written when the input's displayed size differs from the
target, or when its EXIF orientation is anything other than 1 (normal), since
`draw-things-cli` may not apply EXIF rotation. The copy is made in these steps:

1. Open the input with Pillow and apply its EXIF orientation.
2. Convert it to 8-bit sRGB (see Fidelity): scale 16-bit and 32-bit
   grayscale down to 8 bits, flatten any transparency onto black, and convert
   an embedded ICC profile to sRGB.
3. Scale by `max(target_width / width, target_height / height)` (crop) or
   `min(...)` (letterbox). The plan (`ResizePlan.box`) holds the source area
   to resample, computed once in `resize_plan`, so the crop that is checked
   and logged is the crop that is written.
4. Crop: resample the exact centered source area, whose edges may fall
   between pixels, straight to the target size in one pass (see Fidelity). When the
   scale is exactly 1, whole pixels are cut instead, with no resampling, and
   an odd leftover comes off the right or bottom.
   Letterbox: resample the whole image to the scaled size (each side rounded
   to the nearest pixel and kept within the target) and paste it centered on
   a black canvas of the target size; an odd leftover is padded on the right
   or bottom. When the scaled picture is exactly the target (fit `scale`),
   there are no bars and nothing visible is cropped.
5. Save it as PNG, which is lossless, in a new temporary directory
   (`tempfile.mkdtemp`).

### Fidelity

The copy must look like the input, only smaller or larger:

- **Color.** An embedded ICC profile (for example, Display P3 from an
  iPhone) is converted to sRGB with LittleCMS, relative colorimetric intent
  with black point compensation, and the PNG is saved untagged (sRGB).
  Dropping the profile instead would shift colors, since `draw-things-cli`
  would read P3 numbers as sRGB. A profile whose description starts with
  `sRGB` (for example, `sRGB IEC61966-2.1` from a camera) is not converted,
  since the pixels are already sRGB. If a profile cannot be used, a WARNING
  says colors may shift and the pixel values are kept as they are.
- **Bit depth.** 16-bit and 32-bit grayscale images are scaled to 8 bits,
  rounding to the nearest level. Pillow's plain conversion clips them, which
  turns a mid-gray 16-bit image white.
- **One resampling pass, in floating point.** The picture is resampled once,
  straight from the source pixels, with Lanczos (3 lobes), which filters over
  the whole source area when downscaling, so fine detail does not alias. The
  math is 32-bit float per channel and rounds to 8 bits once, at the end
  (Pillow's own 8-bit resize rounds between its horizontal and vertical
  passes). For the crop fit, the source area is exact (fractional), so the
  aspect ratio and centering have no rounding error.
- **Downscaling in linear light.** Light adds up in linear light, not in sRGB
  values: averaging sRGB values makes fine bright detail (hair, texture,
  highlights) a third too dark. Linear-light Lanczos rings into dark halos
  beside bright edges, so each output pixel is pulled 70% of the way back
  into the range of the source pixels under the filter's main lobe
  (`ANTI_RINGING = 0.7`). That brings the halo back to the sRGB-value level
  while edges stay as sharp.
- **Upscaling in sRGB values.** When enlarging, sRGB-value Lanczos gives
  sharper edges than linear light (2.19 against 2.43 output px for a 2×
  step edge), so upscaling uses sRGB values, still in floating point.
- **No sharpening filter.** Unsharp masking was measured and rejected: it
  over-brightened thin highlights (119%) without making edges sharper than
  the method above.
- **No loss when nothing is scaled.** An input that only needs rotating
  upright is cut and saved losslessly: its pixels are unchanged.

Measured on 1920x1080 test patterns downscaled to 832x468, against Pillow's
8-bit sRGB-value Lanczos (the Milestone 03 first version):

| Measure | Pillow 8-bit | This milestone | Ideal |
|---------|--------------|----------------|-------|
| 1 px white/black lines, mean | 127.5 | 187.5 | 188 |
| Step edge, 10–90% rise | 1.39 px | 1.38 px | smaller is sharper |
| Dark halo beside a bright edge | 7.3% | 7.3% | 0% |
| Bright overshoot | 6.7% | 1.3% | 0% |
| Light kept in a 2 px highlight | 77% | 103% | 100% |

Cost: a 6000x4000 photo resizes in about 1 s with 0.7 GB peak memory, and an
8064x6048 (48 MP) photo in about 3 s with 1.2 GB (Apple silicon).

For example, with `desired_input_width: 850`, a 1920x1080 input and target
832x448 scales to 832x468 and loses 10 px at the top and 10 px at the bottom.
With `desired_input_width: 1280` and `desired_input_height: 720`, the same
input and target 1280x704 scales to 1252x704 and gets 14 px black bars on the
left and right.

**When it happens.** When a copy will be written, `validate-job` and
`run-job --dry-run` fully decode the input (`Image.load()`), not only its
header, after the target and crop checks pass, so an image that cannot be
decoded fails there with exit code 2. A real `run-job` skips that decode,
since it writes the temporary copy right after validation and before the job
manifest and log are created; a decode or resize failure there also exits
with code 2 and leaves no manifest. An input used as-is (fit `none`) is not
decoded, as without the keys. An image over Pillow's pixel limit (about 179
MP) fails with exit code 2 as not readable. Loading a job does not import
`input_resize` (numpy, LittleCMS); `run-job` imports it only when a copy is
made.

**Cleanup.** Run 1 uses the temporary PNG as `--image`. The temporary
directory is deleted when run 1 ends, whatever its outcome, and in the job's
`finally`, so an exception, Ctrl-C, `SIGTERM`, or `SIGHUP` also removes it. A
process killed with `SIGKILL` cannot clean up and leaves the directory in the
system temporary folder. If no copy is needed, run 1 uses the original file.

## Reporting

- `validate-job` and `run-job --dry-run` compute and log the target without
  writing any file:
  - `Input photo.jpg (1920x1080) will be scaled to 832x468 and cropped to 832x448 (4.3%) for run 1`
  - `Input photo.jpg (1920x1080) will be scaled to 1252x704 and letterboxed to 1280x704 for run 1`
  - `Input photo.jpg (1664x896) will be scaled to 832x448 for run 1`
  - `Input photo.jpg (EXIF orientation 6) will be rotated upright; already 832x448`
  - `Input photo.jpg is already 832x448; no resize needed`
- `run-job --dry-run` shows run 1's `--image` as a placeholder when a copy
  will be written, so the preview cannot be mistaken for the real command:
  `--image '<photo.jpg resized to 832x448>'`. Otherwise it shows the original
  path.
- `run-job` logs the same line, plus the temporary file's path, before run 1.
- The job manifest gets a new job-level field, `null` when neither
  `desired_input_*` key is set:

  ```json
  "input_resize": {
    "desired_width": 850,
    "desired_height": null,
    "max_crop_percent": 10,
    "input_size": [1920, 1080],
    "exif_orientation": 1,
    "target_size": [832, 448],
    "fit": "crop",
    "scaled_size": [832, 468],
    "crop_percent": 4.3
  }
  ```

  `fit` is `crop`, `letterbox`, `scale` (scaled to exactly the target: no
  crop and no bars), `rotate` (already the target size, copied upright), or
  `none` (an upright input already at the target size, used as-is).
  `max_crop_percent` and `crop_percent` are `null` unless exactly one
  `desired_input_*` key is set. Run 1's `input` still records the original
  input path, and its `resized_input` records the temporary path that
  `command` passes as `--image`; that file is gone after the run, so
  replaying the command means rebuilding it from `input_resize`.
  `resized_input` is `null` for every other run.

## Planned changes

| File | Change |
|------|--------|
| `input_size.py` | `MAX_DESIRED_SIZE = 8192`; `floor_to_step(value)`; `read_image_info(path)` returns the displayed size and the EXIF orientation (it replaces `read_image_size`); `decode_image(path)` fully decodes for validation; `resize_plan(name, input_size, orientation, desired_width, desired_height, max_crop_percent)` returns a `ResizePlan` (target, fit `crop` / `letterbox` / `scale` / `rotate` / `none`, scaled size, crop share, source `box`, `needs_copy`, `describe()`, `as_manifest()`), or raises `ValueError` naming the key and values; the Milestone 01 mismatch message now suggests the new keys |
| `input_resize.py` (new) | `resize_image(source, plan, destination)` performs steps 1–5 with Pillow; `to_srgb(image)` does step 2; `resample(image, size, box)` does the float, linear-light downscale with partial anti-ringing and the sRGB-value upscale; `TemporaryInput` writes the copy into its own temporary directory and removes it with `cleanup()` (safe to call twice) |
| `job_definition.py` | Add `desired_input_width`, `desired_input_height`, and `max_input_crop_percent` to `JOB_KEYS`; validate types, ranges, mode, and the one-key rule for `max_input_crop_percent`; `JobDefinition` gains `size` (the target, or `None`) and `input_resize` (the plan, or `None`); skip `_job_size` and `check_input_size` when a key is set; decode the input fully; record ignored `width` / `height` alongside `ignored_config` |
| `job_definition.py` | `report_ignored_config` also reports ignored `width` / `height` and the resize line |
| `job_service.py` | When `job.size` is set, build `--width` / `--height` and the `--config-json` `width` / `height` from it; otherwise unchanged. In `run()`, write the temporary copy before the manifest and log are created; pass it as run 1's image; delete it after run 1 and in `finally`. `preview()` uses the placeholder for run 1's image |
| `job_manifest.py` | `input_resize` field on `JobManifest`; `resized_input` field on `RunRecord` |
| `pyproject.toml`, `uv.lock` | Add `numpy` (via `uv add numpy`) for the float resampling |
| `data/example-job.yaml` | Commented `desired_input_width`, `desired_input_height`, and `max_input_crop_percent` examples |
| `README.md` | Document the new keys, the rounding and limits, the two fits (crop with one key, letterbox with both), the crop limit, EXIF handling, and the INFO messages |
| `tests/test_input_size.py` | Flooring; derivation for width and for height; both given; under-64 and over-8192 errors for given and derived values; crop share at, below, and above the limit, and at 0; EXIF-rotated input needs a copy even at the target size |
| `tests/test_input_resize.py` (new) | Output size for both fits; the scaled picture's aspect ratio matches the input's within 1 px; crop is centered and on one axis only; bar placement; an odd leftover; upscaling; transparency flattened to black; EXIF orientation applied; an undecodable file fails `decode_image`. Fidelity: 16-bit gray scaled not clipped; Display P3 converted to sRGB (skipped where the macOS profile is missing); an sRGB profile leaves colors alone; an unusable profile warns; a circle stays round and centered in every fit; a fractional crop is mirror-symmetric; a one-pixel checkerboard halves to even gray at the right brightness (188); downscaled edges no softer than 1.40 px with halos under 8%; a thin highlight keeps its light within 6%; upscaled edges no softer than 2.25 px; rotation only is lossless |
| `tests/test_job_definition.py` | Size keys rejected in `t2v`, rejected as non-integers or out of range, accepted in `i2i` and `i2v`; `max_input_crop_percent` rejected without exactly one size key and out of range; no width/height needed when a key is set; exact-size check and `size is None` without the keys |
| `tests/test_job_service.py` | Run 1 gets the temporary file and later runs do not; `--width` / `--height` and `--config-json` use the target; commands unchanged for a job without the keys; the temporary directory is removed after run 1 succeeds, fails, or is interrupted; no file when the input is upright and already matches; a resize failure leaves no manifest; dry run writes nothing and shows the placeholder; manifest `input_resize` |

## Acceptance criteria

All met; see Verification.

- [x] An `i2v` job with a 1920x1080 input and `desired_input_width: 850`
      validates, and `--dry-run` reports a target of 832x448 (4.3% cropped)
      with `--width 832 --height 448` on every run and the placeholder as run
      1's `--image`.
- [x] The same job run for real passes run 1 a temporary 832x448 PNG: the
      picture scaled to 832x468 (16:9 kept) with 10 px cropped at the top and
      bottom, no bars, and the file is gone after run 1.
- [x] With only one key set, the resized image has no black bars, and its
      picture has the input's aspect ratio (within 1 px of rounding).
- [x] With only one key set, a crop over `max_input_crop_percent` (default
      10) fails with exit code 2, naming the pixels, the share, and the ways
      to fix it.
- [x] With both keys set, the picture keeps the input's aspect ratio and the
      rest of the target is black.
- [x] An `i2i` job behaves the same way.
- [x] A `t2v` job with either size key fails with exit code 2 and names the
      key; `max_input_crop_percent` without exactly one size key does too.
- [x] A value that floors to 0, or is above 8192, given or derived, fails
      with exit code 2 and names the key, the input, and the computed value.
- [x] With a key set, `width` / `height` in `config_override` and
      `config_file` are ignored, and an INFO line names each ignored value.
- [x] Without the keys, a mismatched input still fails the Milestone 01
      check, and the commands are unchanged.
- [x] An upright input already at the target size is used as-is, with no
      temporary file; an EXIF-rotated one gets an upright temporary copy.
- [x] An input that cannot be decoded fails `validate-job` with exit code 2.
- [x] Ctrl-C during run 1 still removes the temporary file.
- [x] `make check` passes.

## Verification

- `make check` passes: 127 tests (50 new), Ruff and Black clean.
- Fidelity is measured by tests, not only sizes: for example, Display P3
  (200, 100, 50) comes out as sRGB (215, 93, 31), and a 16-bit mid-gray
  comes out as 128.
- End to end with the real CLI wiring and a stub `draw-things-cli`, a
  1920x1080 JPEG and `desired_input_width: 850`:
  - `validate-job` and `run-job --dry-run` print the INFO lines for the
    ignored `config_override.width` and `config_file` width and height, and
    `will be scaled to 832x468 and cropped to 832x448 (4.3%)`. The dry run
    shows `--width 832 --height 448` and the placeholder `--image`.
  - A 2-run `i2i` `run-job` passed run 1 the temporary
    `photo-832x448.png` and run 2 run 1's output. Both outputs are
    832x448, and no `draw-things-control-*` directory was left in the
    temporary folder.
  - `desired_input_width: 30` fails `validate-job` with exit code 2.
- Not run with the real `draw-things-cli` or a real `i2v` video model.
- A truncated PNG is already rejected when its header is read (Pillow reads
  to the end of a PNG to find EXIF), with the Milestone 01 message `'input'
  is not a readable image`. The new full decode is what catches a truncated
  JPEG, whose header is still readable.
- Where both `config_override` and `config_file` set `width` (or `height`),
  both ignored values are reported, one INFO line each.

### Review fixes

A code review of the first version found ten issues, all fixed:

- An image over Pillow's pixel limit raised `DecompressionBombError`, which
  is not an `OSError`, and escaped as a traceback. It is now a validation
  error.
- Run 1's manifest record did not say which file its `command` passed as
  `--image`; it now has `resized_input`.
- A mode Pillow cannot convert to RGB (for example, LAB) raised a bare
  `ValueError`; it now names the input and the resize step.
- An exact fit was reported as `letterbox` or `cropped (0.0%)`; it is now
  fit `scale`, and a rotate-only copy is fit `rotate`.
- The input was decoded during validation before the crop check, even when
  it was used as-is, and again by a real run. It is now decoded after the
  checks, only when a copy is made, and once per real run.
- Loading a job imported numpy and LittleCMS; `decode_image` moved to
  `input_size.py` and `input_resize` is imported only when a copy is made.
- The cover geometry was computed twice; it now lives only in the plan.
- The "needs a copy" check is one `JobDefinition.input_copy` property, and
  the default crop limit is applied only in `resize_plan`.
- `read_image_size` was unused and is removed.
- An embedded sRGB profile is no longer run through LittleCMS.

`make check` passes: 134 tests.

## Open questions

None.
