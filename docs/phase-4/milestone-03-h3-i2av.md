# Milestone 03: H3 image-to-audio-video support

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 02](milestone-02-multiple-backends.md) and the local [h3.c](https://github.com/antirez/h3.c) build and model snapshot.

## Goal

Run `i2av` chains with h3.c so each segment begins from the previous segment's final video frame and contains H3's generated audio.

## Scope

In scope:

- `backend: h3`, `i2av` mode, and H3-specific `backend_settings` in the job format, available through the CLI, TUI, HTTP API, and MCP queue flows. H3 jobs omit Draw Things' `config_file` and `config_override`.
- The repo-local `contrib/backends/h3.c/h3` executable, resolved through project paths; preflight of the H3 model snapshot and FFmpeg/FFprobe requirements.
- One-shot H3 invocations with `--first-frame` for run 1's input and each later run's extracted last frame, a prompt, seed, supported size and frame controls, and an MP4 output.
- Native synchronized video and audio in every successful run's output, with metadata and resume behavior that distinguish `i2av` from `i2v`.
- Clear validation of H3-specific settings and rejection of Draw Things-only settings, negative prompts, `color`, and incompatible output formats.

Out of scope:

- H3's `--ref-image`, video/audio references, `--last-frame`, text-only mode, and interactive session.
- Carrying audio from one segment into the next as a conditioning input. The selected first-frame path carries the video frame; H3 generates each segment's audio.
- Combining generated segments into one continuous audiovisual file.

## Planned changes

- Define an `i2av` job example with `backend: h3` and `backend_settings` for H3's model location, dimensions, frame count or duration, denoising steps, and supported performance controls. Resolve the executable from `ProjectPaths`; the local `contrib/backends/h3.c/MiniMax-H3` snapshot is the planned model location. Keep runtime paths out of modules and prevent job files from selecting arbitrary executables.
- Validate H3's dimensions and temporal shape before work is queued. H3 currently requires width and height divisible by 32, within its pixel limit, and rounds requested frames to a supported `5 + 17*n` shape at 24 fps. Record requested and actual frame counts without assuming they match.
- Build one-shot commands using `-d`, `-p`, `--first-frame`, `--width`, `--height`, `--frames` or `--seconds`, `--steps`, `--seed`, and `-o`. Use the existing supervised process and queue lock; do not use H3's interactive session for a queued run.
- Validate that the output contains both video and audio, measure its actual properties, extract the final frame for the next run, and retain the MP4 with H.264 video and AAC audio. Decode the handoff frame under H3's media contract, without Draw Things' half-level correction or ProRes tagging.
- Carry H3 settings and backend identity through snapshots, previews, run logs, manifests, history, interruption, and resume. Make an interrupted run restart at its boundary with the original seed and selected backend.
- Document setup, a copyable `i2av` job, the supported settings, audio behavior, and constraints in the user guide and architecture document.

## Acceptance criteria

- A valid `i2av` job runs through H3 from the CLI or queue and can be observed through the TUI, HTTP API, and MCP with the same status and history semantics as existing jobs.
- Each run passes its input image as `--first-frame`; run 2 and later use the preceding successful run's extracted final frame. A resumed chain uses the saved handoff and backend.
- Each successful run's MP4 has measurable video and audio streams, and its actual dimensions, frame count, and output path are recorded.
- Unsupported H3 options or combinations, including `color` and Draw Things configuration fields, fail validation before execution; missing binary, model snapshot, FFmpeg, or FFprobe yields a specific tool or input error.
- Cancellation, timeout, worker restart, and resume preserve the existing run-boundary behavior; Draw Things jobs remain compatible.
- `make check` passes when the implementation lands.

## Research

The [H3 research notes](../research/h3-c.md) record the upstream CLI behavior and constraints used in this plan.
