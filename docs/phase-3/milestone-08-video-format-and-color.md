# Milestone 08: Video format and color

**Phase:** [Phase 3: API server and MCP server for AI](README.md)
**Status:** done (2026-10-01, owner decision; the owner's 2-run chain passed on 2026-09-30, and the decode rule and
the handoff changed since were measured exact on E0017's chain)
**Depends on:** [Phase 2](../archive/phase-2/README.md)'s color tagging and sRGB last frames (`jobs/media/`), and
[Milestone 01](milestone-01-queue-run-manager.md)'s queue snapshots, which are parsed again when an entry runs.

## Goal

Keep more of each generated clip, and keep the color of the frame that starts the next clip true, so a long chain
drifts less. Video jobs write ProRes 4444 by default instead of `draw-things-cli`'s H.264: 4:4:4 chroma at 12 bits,
near-lossless, where H.264 is 4:2:0 at 8 bits and lossy. The last frame loses its alpha channel. Its color decode,
and the `colr` tag written into each video, both follow the color space the video's own stream states (owner
decision: honor the header).

## As built

Built on 2026-09-30, in two steps. Step 1 added `output.video_format` (then with no default) and `--disable-preview`
so a job could write ProRes for the color investigation; the rest followed an interview the same day (see the
[changelog](phase-3-changelog.md)). Everything below [Behavior](#behavior) holds, with these as the built facts:

- `output.video_format` defaults to `prores4444` in every video job; an `mp4` job must name `h264` or `hevc`. An
  unknown value is refused with `output.video_format: must be prores4444, prores422hq, h264, hevc`. The format is an
  `output format` row of `jobs/text.job_summary` (so in `validate-job` and the TUI's Job Definition widget), is in
  the TUI preview's commands, and is `video_format` in `GET /v1/jobs/{job}`.
- Every job run passes `--disable-preview`. The one run with it (`v-i8x`) wrote a clean video.
- `dtc generate` passes `--video-format prores4444` for a `.mov` output with none.
- Trust the pixels (superseding honor the header, see [The color matrix](#the-color-matrix)):
  `jobs/media/stream_color.py`'s `resolve_video_color` reads the first frame's matrix, range, and the source's bits
  once, before tagging, and for 4:4:4 at 10 bits or more measures the matrix the pixels were encoded with; the
  measured matrix wins when it is conclusive. `RunFinisher` passes that `StreamColor` to the tagger (`colr` matrix
  code from it, the `nclx` range flag from it) and to the extractor, which always names the matrix and range to
  ffmpeg. `has_matrix_tag` and `UNTAGGED_DECODE` are gone. A stated matrix outside the table is decoded by ffmpeg's
  own choice and left untagged, with a warning.
- The last frame has no alpha. It was first 16-bit RGB from a source of more than 8 bits (ProRes) and 8-bit from
  H.264, a 16-bit frame rescaled by 257/256 (owner decisions). Later the same day, after the
  [color drift research](../research/color-drift.md#the-handoff) found that `draw-things-cli` reads a 16-bit PNG by
  its high byte and truncates the values it writes, it became the handoff (owner decisions superseding both): 16-bit
  RGB from every source, each sample one 8-bit value `v` as `v * 256 + 128`, so `draw-things-cli` reads exactly `v`.
  `v` is the decoded level plus the half level Draw Things truncated (tapered to nothing at black), rounded against a
  2x2 ordered-dither pattern so a flat area is not half a level off. See [The last frame](#the-last-frame). The decode
  asks swscale for accurate rounding, without which 8-bit H.264 decodes to 16-bit RGB 1 to 1.5 levels dark.
- Not in the plan: the media checks (input, resized copy, video, last frame) of every video job, as `media_checked`
  events on `dtc serve`'s output, the job log, the TUI's Messages, and the gRPC stream, kept per run in the state
  store (schema 7, `media_checks`) and shown in the TUI's execution detail and `GET /v1/executions/{id}`. See
  [the user guide](../user-guide.md#where-outputs-go).
- From the code review's open items, the owner decided (2026-09-30): a resume's input, the chain's own last frame, is
  checked as a handoff (an RGB PNG without alpha, 8- or 16-bit), not as a person's input, which ends the false
  warning on every ProRes resume; a media check that cannot be stored is skipped and logged once, and the rest of
  the execution is still recorded; the checks of a run that never started are shown on the execution (`checks` in
  `GET /v1/executions/{id}`, and `Before run N (never started)` in the TUI); the checker finds `ffmpeg` and
  `ffprobe` at each check, so a long-running `dtc serve` finds them once installed; and the resized-input summary
  says `mean color moved 3.2 levels` when it warns, `kept within` otherwise.

The owner's chain run (`duo`, execution E0016, 832x448, 2 runs at 40 steps) met the 16-bit criterion: run 2 started
from run 1's 16-bit last frame and is a clean image (neighbouring luma correlation 0.993). It also showed that Draw
Things' ProRes states BT.601 at 832x448 while its pixels are BT.709, which honoring the header turned into a
1.4-level (up to 12) error in each last frame; the decode rule was changed the same day. Its two last frames were
made before the change, and are not re-extracted.

## Behavior

### The video format

A job's `output` gains `video_format`: `prores4444` (the default), `prores422hq`, `h264`, or `hevc`, passed to every
run as `--video-format`. The default applies whether the key is omitted or not, with any extension (owner decision).

| `output` | Result |
|----------|--------|
| nothing, or `extension: mov` | `prores4444` into `.mov` |
| `video_format: h264` (or `hevc`), `extension: mov` or `mp4` | That format, in that container |
| `video_format: prores422hq`, `extension: mov` | ProRes 422 HQ into `.mov` |
| `extension: mp4` with no `video_format`, or with a ProRes one | Refused: `output.video_format: prores4444 requires extension mov; set video_format to h264 or hevc for mp4` |

`.mov` stays the default extension, and is required only by the ProRes formats (owner decision); `mp4` stays
allowed with `h264` and `hevc`. An image job (`i2i`) refuses the key: `output.video_format: only in video jobs`.

The resolved format is shown by an `output format` row in `jobs/text.job_summary` (so in `validate-job` and the
TUI's Job Definition widget, which both print it), in the TUI preview's commands (they carry `--video-format`), and
in `GET /v1/jobs/{job}` (`video_format` beside `extension`). No front end shows the extension today, and `dtc` has
no `--dry-run`.

A queued entry runs its snapshot, which is parsed again when it is claimed and when it is resumed, so it gets the
new default too. Every snapshot in the state store today uses `mov` (checked on 2026-09-30), so none is refused. A
snapshot with `mp4` and no `video_format` would fail to start, naming the field, as any snapshot that no longer
parses does. A resume of an execution that ran as H.264 continues in ProRes: its later runs are `.mov` ProRes, its
earlier ones stay as they were, and the last frame it continues from is an 8-bit PNG, which is a valid input.

ProRes 4444 files are about 7 times larger per pixel than Draw Things' H.264: an 81-frame 448x576 clip is 28 MB,
where an 81-frame 832x448 H.264 clip is 5.7 MB. The user guide says so, and says how to choose `h264`.

### `dtc generate`

`dtc generate` passes `--video-format prores4444` when `--output` ends in `.mov` and no `--video-format` is given
(owner decision). An `.mp4` output, or an explicit `--video-format`, is passed on as today. `generate` still neither
tags its output nor extracts a last frame.

### The last frame

The last frame is saved without alpha (owner decision). Draw Things' ProRes 4444 carries an alpha plane at 4080 of
4095, which ffmpeg writes as 65280 of 65535 in the PNG; a correct 16-to-8-bit conversion reads that as 254, not 255,
and Draw Things treats alpha as a mask. The sRGB label is unchanged.

**Superseded the same day by [the handoff](#the-handoff) below:** the rest of this section is the record of the
first depth rule. The format list becomes `rgb24|rgb48be`: ffmpeg picks 16-bit RGB for 12-bit ProRes and 8-bit RGB
for H.264, as it picks the depth today (owner decision: keep the source's depth).

**Settled: 16-bit from ProRes, 8-bit from H.264 (owner decision, 2026-09-30), pending the owner's chain run.** The
first verification run fed `draw-things-cli` a 16-bit
RGB PNG at 8 steps and got pure noise from the first frame on. The second, job `v-i8x`, gave a clean video from an
8-bit input at 40 steps ([research note](../research/prores-color-matrix.md#the-clis-runs)). The two differ in both
the depth and the steps, so the noise is still unexplained. A run with a 16-bit input at 40 steps tells them apart.
The owner chose 16-bit and verifies it with a chain run of the built change. If `draw-things-cli` cannot read a
16-bit PNG, a 16-bit last frame would break every chain, and the format list becomes `rgb24` alone.

### The color matrix

**Settled: trust the pixels (owner decision, 2026-09-30),** superseding "honor the header" of the same day. When the
pixels tell which matrix encoded them ([the test](../research/prores-color-matrix.md#the-test), for 4:4:4 at 10 bits
or more: ProRes 4444), extraction decodes with that matrix and the tagger writes it; otherwise both follow the matrix
the stream states; BT.709 limited range when it states none. The range always follows the stream. The output check
still says when the pixels overrule the stream.

Honor the header held for one day. The owner's `duo` chain (832x448) then showed that Draw Things' ProRes states
`smpte170m` for 832x448 and 448x576 frames and `bt709` for 576x768 and 576x1152, while the pixels are BT.709 at
every size: the encoder labels by frame size (the data fits "height 720 or more" and "more pixels than 720x576"
alike; a 1024x448 run would tell them apart). Honoring that header decoded each 832x448 last frame 1.4 levels off on
average and 12 at most (red -1.9, green -1.2), and the error went into the next run.

The evidence ([research note](../research/prores-color-matrix.md)):

- Both clean ProRes 4444 files of 2026-09-30, one from the Draw Things app and one from the current `draw-things-cli`
  (job `v-i8x`), state `bt709` and `tv` in every frame header, with no primaries, no transfer, and no `colr` box.
- Both were encoded with BT.709 in limited range. Every frame scores 0.216 for BT.709 against 0.229 to 0.265 for
  BT.601 and BT.2020 (random is 0.25). A free search over Kr and Kb lands on 0.215, 0.07 to 0.075 (BT.709 is 0.2126,
  0.0722), and full-range scaling scores near random for every matrix. So in current output, the header tells the
  truth.
- The 9 older app files (448x576) state `smpte170m` but were encoded BT.709, and so do the owner's two 832x448 `duo`
  runs and the first verification run (the noise one, 832x448). The header follows the frame size.
- ffmpeg decodes a ProRes frame with the matrix its header states, whatever the `colr` box says. For H.264 whose
  stream states nothing, it decodes with the `colr` box's. ffprobe's stream-level `color_space` reports the box once
  there is one, so it no longer shows what the stream states.

The table below is what the stream states, used when the pixels cannot tell (H.264, HEVC, ProRes 422, or an
inconclusive measurement). What "the header" is: the first decoded frame's `color_space` and `color_range`, read with ffprobe before the tagger
runs (`-read_intervals %+#1 -show_entries frame=color_space,color_range`). For ProRes that is the frame header; for
H.264 and HEVC it is the stream's VUI. The same reading drives both the decode and the tag.

| The stream states | Extraction decodes with | The `colr` matrix code |
|-------------------|-------------------------|------------------------|
| `bt709` | `in_color_matrix=bt709` | 1 |
| `smpte170m` | `in_color_matrix=smpte170m` | 6 |
| `bt470bg` | `in_color_matrix=bt470` (the name every ffmpeg version reads; newer ones also read `bt470bg`) | 5 |
| `bt2020nc` | `in_color_matrix=bt2020` | 9 |
| `fcc` | `in_color_matrix=fcc` | 4 |
| `smpte240m` | `in_color_matrix=smpte240m` | 7 |
| nothing (`unknown`, `unspecified`, `reserved`, empty, or ffprobe missing or failing) | `in_color_matrix=bt709` (Phase 2's measured default for Draw Things' untagged H.264) | 1 |
| any other matrix | ffmpeg's own choice, with a warning naming it | none: the file is left untagged |

The range is honored the same way: `in_range=tv` or `in_range=pc` as stated, `tv` when nothing is stated. The PNG is
always written full range (`out_range=pc`) and labeled sRGB, as today. Extraction always passes the matrix and the
range explicitly, so it no longer depends on whether ffmpeg reads them from the stream or from the box.

**What remains.** A ProRes file whose frames state `smpte170m` is tagged `bt709` in its `colr` box, so it still
contradicts itself for a reader of the frame header, such as ffmpeg (QuickTime reads the box). Rewriting the frame
headers would change Draw Things' bytes, which the tagger never does. Where the pixels cannot tell (H.264), a wrong
header would still be followed. Existing outputs are not re-extracted or retagged (out of scope).

### The handoff

**Settled: 16-bit, exact high byte, from every source (owner decisions, 2026-09-30),** superseding 16-bit from ProRes
only and the 257/256 rescale. Draw Things' source shows `draw-things-cli` reads a 16-bit PNG by its high byte
(`>> 8`), and truncates the values it writes (`Int((v + 1) * 127.5)`). With the rescale, that made each handoff
0.89 level dark in the shadows and 0.10 in the highlights, simulated
([the handoff](../research/color-drift.md#the-handoff)). Now:

- Each sample holds one 8-bit value `v` as `v * 256 + 128`: `draw-things-cli` reads exactly `v`, and a reader that
  divides by 65535 sees `v` within half a level. Every source gets it, H.264 included (owner decision), so the last
  frame check expects 16-bit always.
- `v` is the decoded level `e` plus `0.5 * min(1, e)`: the half level Draw Things truncated, tapered to nothing at
  black, where every value at or below black also lands, so black and letterbox bars stay 0 (design decision).
- A flat area cannot hold `e + 0.5` in 8 bits: plain rounding puts every sample of it on the same side, half a level
  off. Each sample is rounded against a 2x2 ordered-dither threshold (1/8, 5/8, 7/8, 3/8 of a level) instead (owner
  decision), so a flat area holds `v` and `v + 1` in equal parts. Measured on ProRes 4444 made from flat blocks at
  every 8-bit level: 0.04 level from the true mean per level, against 0.50 for plain rounding; for H.264, the 0.29
  left is its own 8-bit YCbCr error.
- The decode passes `flags=accurate_rnd+full_chroma_int` to swscale. Without it, ffmpeg 8.1.1 decodes 8-bit YCbCr to
  16-bit RGB 0.8 to 1.6 levels dark (ProRes is the same either way).
- The pattern is below half a level, and the VAE's 8x8 downsampling averages it away. `ffmpeg`'s `geq` does it; the whole
  extraction of an 832x448 ProRes 4444 frame takes 0.3 s.

### The `colr` tag

The tagger writes the matrix extraction decoded with (owner decision): the measured one, or from the table above, so a player and the
next run see the same colors. The primaries stay BT.709 (code 1) and the transfer sRGB (code 13), as today:
Draw Things' models produce sRGB, and no Draw Things file states either. In an `mp4` (`nclx` box), the full-range
flag follows the stated range; a QuickTime `nclc` box has no range field. A file that already has a `colr` box is
still left alone, since its tags are the writer's statement.

## Scope

In scope:

- `output.video_format`, its default, its rules, and where it is shown (the key itself is built)
- `--disable-preview` on every job run (built)
- The `dtc generate` default for `.mov`
- Dropping alpha from the last frame
- Reading the stated color space once, before tagging, and using it for both the decode and the `colr` tag, for
  every format
- The measurement behind the rule, saved in [docs/research](../research/prores-color-matrix.md)

Out of scope:

- `data/params/`: `--video-format` is a `draw-things-cli` option, not a key of the Draw Things configuration, so no
  configuration file changes, and none may be edited ([development rules](../development-rules.md))
- Re-encoding, retagging, or re-extracting existing outputs
- Choosing the format per run, or in the global configuration
- HDR or wide-gamut output, and honoring stated primaries or transfer; the working space stays sRGB
- Recording the decoded color space in the manifest (the media checks, stored per run, name it)
- The first input's resized copy, which stays 8-bit sRGB RGB

## Before done

1. A chain run with the changed decode rule and the new handoff, to see an 832x448 chain decoded as BT.709 and handed
   off unbiased. The `duo` run showed 16-bit input works; the first verification run's noise then points to its 8
   steps (not tested).

## Planned changes

### `jobs/`

- `definition.py` (built): `JobDefinition.video_format: str | None`; the default is still to add. `GenerationMode`
  keeps `default_extension` and `allowed_extensions`.
- `parsing.py` (built): `OUTPUT_KEYS` has `video_format`, and `_video_format` checks the value, the image-job
  refusal, and the ProRes and `mp4` rule after the extension, naming `output.video_format`. Still to add: the
  `prores4444` default for video jobs.
- `planning.py` (built): `plan_run` passes `video_format` and `disable_preview=True` to `DrawThingsGenerateArguments`.
- `text.py`: `job_summary` gains an `output format` row, the format and the extension (for example
  `prores4444 (.mov)`), for video jobs.
- `media/color.py` (new): `read_video_color(video, ffprobe) -> VideoColor`, the first frame's matrix and range as
  above, with `stated` telling a stated value from the default. It holds the table: ffprobe's name to the `scale`
  filter's name and to the H.273 code.
- `media/frames.py`: `SRGB_FORMATS` without `rgba` and `rgba64be`, pending the depth; `srgb_filter(color)` always
  starts with `scale=in_color_matrix=<matrix>:in_range=<range>:out_range=pc`, or leaves the decode to ffmpeg for a
  matrix outside the table. `has_matrix_tag` and `UNTAGGED_DECODE` go: they read the stream after tagging, which
  reports the box.
- `media/video_color.py`: `tag_video_colors(video, color)` writes the matrix code of `color`, and the `nclx`
  full-range flag from its range; for a matrix outside the table it writes nothing and returns `False`.
- `media/toolkit.py`: `MediaTools` gains `color_reader: Callable[[Path], VideoColor]`; `FrameExtractor` becomes
  `Callable[[Path, Path, VideoColor], None]` and `VideoTagger` `Callable[[Path, VideoColor], bool]`.
- `run_finisher.py`: for a video run, read the color once, before tagging, log it (`Decoding v-...mov as bt709, tv,
  as its stream states`, or `as BT.709 limited range: its stream states no matrix`), and pass it to the tagger and
  the extractor.

### `core/`

- `generation.py`: `GenerationService.prepare` sets `video_format` to `prores4444` for a `.mov` output that has none.
  `DrawThingsGenerateArguments` already checks the value and that ProRes needs `.mov`.

### `services/`

- `toolkit.py`: builds `MediaTools` with `color_reader`, and the extractor and tagger with their new signatures.

### `server/`

- `serializers.py`: `job_detail` gains `video_format`.

### `tui/`

- Nothing of its own: the Job Definition widget prints `job_summary`, so it shows the new row.

### Repository

- `data/jobs/example-job.yaml` (built): a commented `video_format` line, and the `extension` comment corrected ("mp4
  only with h264 or hevc"). The owner's other job files get the same comment fix, and no other change (`v-i8x.yaml`
  has it, with `video_format: prores4444` set for the investigation).

### Tests

- Parsing (built): no key; each allowed value; an unknown value; `mp4` with a ProRes format, refused naming the
  field; `mp4` with `hevc`; the key in an `i2i` job, refused. Still to add: the default, and `mp4` with no format
  refused.
- Planning (built): every run's command carries the job's `--video-format`, and `--disable-preview`; the golden
  files in `tests/jobs/golden/` carry `--disable-preview`. Still to add: the default in every video run's command.
- A snapshot with `mp4` and no format fails to start, naming the field; one with `mov` runs with the default.
- `generate`: a `.mov` output gets `prores4444`; `.mp4` and an explicit value are passed on unchanged.
- `job_summary`, `validate-job`, and the TUI's Job Definition tests: the `output format` row for a video job, none
  for an image job.
- `test_media_color.py` (new), with ffmpeg-made files: `prores_ks` with `smpte170m` reads `smpte170m`, and still
  does after `tag_video_colors` has added a BT.709 box; with `bt709`, `bt709`; untagged `libx264` reads as not
  stated; a `pc`-range file reads `pc`; a missing ffprobe reads as not stated.
- `test_media_frames.py`: the PNG is `rgb48be` from 12-bit ProRes and `rgb24` from 8-bit H.264, with no alpha, still
  labeled sRGB (the depth test that expects alpha is changed accordingly); a `prores_ks` file made with `smpte170m`
  is decoded as BT.601 and one made with `bt709` as BT.709, told apart by a saturated test color; an untagged H.264
  file is decoded as BT.709; the filter chain always names the matrix and the range.
- `test_video_color.py`: the matrix code written for each row of the table, the `nclx` range flag, no box for a
  matrix outside the table, and a file with a box left alone.
- `run_finisher`: the color is read once, before the tagger, and both the tagger and the extractor get it.
- The serializer shows `video_format`.
- No test starts the real `draw-things-cli`; ProRes test files are made with ffmpeg's `prores_ks`, as today.

### Documentation, when it lands

- `docs/user-guide.md`: `output.video_format` in the job file table (done for the key) and "Where outputs go"; the
  default; the file size; the last frame's format; the color rule (the video's stated color space, BT.709 limited
  range when it states none); `dtc generate`'s default.
- `docs/architecture.md`: the color reading, the decode, and the tag in the `jobs/media/` row.
- The phase changelog, and this document's status and an "As built" section.

## Acceptance criteria

- A video job with no `video_format` runs `draw-things-cli` with `--video-format prores4444` into `.mov`, and
  `validate-job`, the TUI's Job Definition widget and preview, and `GET /v1/jobs/{job}` say so.
- `mp4` with no format, or with a ProRes one, is refused naming `output.video_format`; `mp4` with `h264` or `hevc`
  runs; `video_format` in an image job is refused.
- Every job run passes `--disable-preview`.
- `dtc generate -o x.mov` passes `--video-format prores4444`; `-o x.mp4` and an explicit `--video-format` do not
  change.
- Every last frame is 16-bit RGB with no alpha, labeled sRGB, each sample `v * 256 + 128` (built and tested with
  ffmpeg-made clips), and a real chain run with the installed `draw-things-cli` shows the next run reads it
  correctly.
- Extraction decodes with the matrix and range the video's stream states (BT.709 limited range when it states
  none), and the `colr` box states the same matrix, for ProRes and H.264; a ProRes file stating `smpte170m` is
  decoded and tagged as BT.601.
- No file under `data/params/` changes.
- `make check` passes.
