# h3.c for Phase 4

Researched 2026-10-12 for [Phase 4, Milestone 03](../phase-4/milestone-03-h3-i2av.md), then updated 2026-10-13 for [Milestone 05](../phase-4/milestone-05-h3-r2av.md). Source: the [antirez/h3.c README](https://github.com/antirez/h3.c) and its [CLI entry point](https://github.com/antirez/h3.c/blob/main/main.c), checked against the local `contrib/backends/h3.c` checkout; the current [MiniMax H3 README](https://github.com/MiniMax-AI/MiniMax-H3) and `MiniMaxAI/MiniMax-H3` Hub listing were checked for Ref2VA model limits. The underlying model's documentation describes a broader set of modes; the plans target the h3.c executable's current interface.

- h3.c is an Apple Silicon MiniMax H3 inference engine. Its one-shot CLI takes `-d MODEL_DIR`, `-p PROMPT`, `-o OUTPUT`, dimensions, temporal controls, steps, seed, and conditioning flags. Without a prompt it enters an interactive session, which does not fit the existing run-boundary queue model.
- `--first-frame` selects first-frame conditioning. `--ref-image` uses a separate ordered-reference checkpoint and treats the image as a reference rather than the opening frame. The owner chose `--first-frame` for `i2av` chains.
- The output is MP4 with synchronized H.264 video and 32 kHz stereo AAC audio. FFmpeg and FFprobe must be available for media inputs and MP4 output; the executable also accepts `H3_FFMPEG` and `H3_FFPROBE` overrides.
- Output is 24 fps. Requested frame counts are rounded upward to `5 + 17*n` (for example, 23 becomes 39). Width and height must be multiples of 32; their product is capped at `768 * 1344` pixels. Short development clips work, while the released workflow targets roughly 4–15 seconds.
- The public CLI currently supports first/last-frame conditioning and ordered image, video, and audio references. These are different modes with different inputs; only first-frame `i2av` is planned for the initial integration.
- The local checkout contains `contrib/backends/h3.c/h3` and `contrib/backends/h3.c/MiniMax-H3`. The executable path is the owner's chosen integration path; the model snapshot location is the planned default to validate at implementation time.
- Verified on 2026-10-12 against the checked-in executable and `main.c`: `--seconds` parses a positive finite duration as `llround(seconds * 24)` frames, and `--seconds` with `--frames` is rejected as mutually exclusive. A job can therefore require and cross-check both values while passing only `--seconds`. The Codex execution sandbox reports `h3: no Metal device available` for `--info`, although the underlying host can run H3; a human performs the actual first-frame inference and MP4 inspection on that host.
- Updated 2026-10-13: h3.c exposes optional internal render size, denoiser reuse, retained DiT layers, core reuse, token reduction, SSD weight streaming, row-FC2 quantization, released-reference RoPE, and ten slower compatibility paths. The supported ranges in the checked-in validator are render dimensions that are same-aspect and no larger than the output, denoiser reuse 1–3, layers 35–50, and core reuse 1–6. Denoiser reuse and core reuse cannot both exceed 1. `--use-int8-row-fc2` requires a Metal 4 GPU and cannot be combined with `--ssd-streaming` or `--use-slower-bf16-mlp`. The h3.c comments identify 1/50/1 as close-reference defaults, 2/45/4 as validated fast values, and 3/40/6 as aggressive values; the latter tuples describe separate reuse alternatives rather than valid simultaneous settings.
- Ref2VA takes an ordered list of `--ref-image`, `--ref-video`, `--ref-silent-video`, `--ref-video-audio VIDEO AUDIO`, and `--ref-audio` flags plus `--ref-image-size match|max`. Its local validator accepts at most 9 images, 3 videos, 3 audio inputs, and 12 references; audio needs an image or video reference. `--ref-silent-video` does not count as audio, while `--ref-video` and `--ref-video-audio` do. The upstream MiniMax H3 documentation adds a 2–15 second limit for each video/audio reference; h3.c caps total decoded reference audio at 15 seconds and truncates a video's soundtrack to the video frames it consumes. Video references with audio require at least 56 effective output frames. h3.c selects Ref2VA when references are present and rejects references combined with first/last frame anchors; a mixed Framechain plan must therefore make the Ref2VA run independent, then may pass its verified output frame only to a later FL2VA `i2av` run.

## Ref2VA prompt structure

Framechain passes the selected positive prompt unchanged. Organize an `r2av` prompt with these six sections, in order:

1. `subject_definitions`
2. `summary`
3. `retention_analysis`
4. `detailed_description`
5. `overall_soundscape`
6. `non_diegetic_music`

Reference labels are assigned in declared pair order, independently by kind: `<Picture 1>`, `<Video 1>`, `<Audio 1>`, and so on. A `video_audio` entry contributes one video and one audio label; its separate audio uses the next audio ordinal. The prompt should use labels for the pair selected by that run. Framechain records the selected pair and asset inventory but does not rewrite or interpret natural-language prompt text.
