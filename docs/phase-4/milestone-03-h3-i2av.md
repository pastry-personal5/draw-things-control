# Milestone 03: H3 image-to-audio-video support

**Phase:** [Phase 4](README.md)
**Status:** in-progress
**Depends on:** [Milestone 02](milestone-02-multiple-backends.md) and the local [h3.c](https://github.com/antirez/h3.c) build and model snapshot.

## Goal

Run `i2av` chains with h3.c so each later segment is first-frame-conditioned on the previous successful segment's final video frame and contains H3's generated audio.

## Scope

In scope:

- `backend: h3`, `i2av` mode, and H3-specific `backend_settings` in the job format, available through the CLI, TUI, HTTP API, and MCP queue flows. H3 jobs omit Draw Things' `config_file` and `config_override`.
- The repo-local `contrib/backends/h3.c/h3` executable and `contrib/backends/h3.c/MiniMax-H3` model snapshot, resolved through `ProjectPaths`, not from a job file. Preflight of those installation assets and FFmpeg/FFprobe requirements.
- One-shot H3 invocations with `--first-frame` for run 1's input and each later run's extracted last frame, a prompt, seed, supported size and frame controls, and an MP4 output.
- Native synchronized video and audio in every successful run's output, with metadata and resume behavior that distinguish `i2av` from `i2v`.
- Clear validation of H3-specific settings and rejection of Draw Things-only settings, negative prompts, `color`, and incompatible output formats.

Out of scope:

- H3's `--ref-image`, video/audio references, `--last-frame`, text-only mode, and interactive session.
- Carrying audio from one segment into the next as a conditioning input. The selected first-frame path carries the video frame; H3 generates each segment's audio.
- Combining generated segments into one continuous audiovisual file.

## Job contract

An H3 job is the only accepted initial shape:

```yaml
version: 1
name: h3-example
backend: h3
mode: i2av
input: opening.png
run_count: 2
prompt_pairs:
  - name: scene
    positive: "A calm autumn street; light wind in leaves; distant city ambience."
    default: true
backend_settings:
  width: 768
  height: 1344
  frames: 96
  seconds: 4
  steps: 30
  seed: 42
output:
  extension: mp4
cooldown:
  mode: off
```

- `backend_settings` is a closed mapping with exactly six required keys: `width`, `height`, `frames`, `seconds`, `steps`, and `seed`. It exposes no H3 performance controls in v1. Dimensions, frames, steps, and seed are integers, excluding booleans; seconds is a finite number, excluding booleans. `seed` is an integer from 0 to the existing `2**32 - 1` job seed bound, even though h3.c accepts a wider CLI seed. The job owns no executable, model, FFmpeg, or FFprobe path and no environment-variable override.
- Width and height are each at least 32, divisible by 32, and their product is at most `768 * 1344`. Steps are in h3.c's supported 2–1000 range. `seconds` is a finite 1–15-second value and `frames` is the corresponding 24–360 positive integer such that `frames == llround(seconds * 24)` (halfway values round away from zero, as in the C CLI); an out-of-range or mismatched value is rejected before queueing. The fixed H3 rate is 24 fps. The effective frame count is the least `5 + 17*n` at least as large as `frames`, no more than 362. Record requested seconds and frames, computed effective frames and duration, and measured output frames/rate separately. For example, a 15-second request is 360 requested frames and 362 effective frames; it is not a promise of an exact 15-second MP4.
- H3 accepts `output.directory` and only `output.extension: mp4`; its default is `mp4`. It rejects `output.video_format`, `config_file`, `config_override`, `desired_input_width`, `desired_input_height`, `max_input_crop_percent`, `color`, and the presence of every `prompt_pairs[].negative`, including an explicit `null` or empty string. This names the actual negative-prompt field rather than a nonexistent top-level field.
- The normal `run_timeout_seconds`, cooldown, run count, prompt scheduling, input confinement, queue limits, cancellation, parking, and run-boundary resume remain common behavior. The H3 adapter supplies its own bounded finishing cost to API-limit calculation.

## Compatibility

This milestone is forward-only. It may change the Milestone 02 backend protocol, queue snapshot version, SQLite state schema, event payloads, manifests, and internal service wiring without readers, shims, or migrations for pre-M03 snapshots, records, manifests, or adapters. New M03 state must be internally self-consistent and resumable; the implementation must update the current test fixtures and documentation to that one contract. Draw Things remains a supported production backend, but preserving undocumented internal interfaces or historical persistence formats is not an acceptance criterion for this milestone.

## Backend and media contract

- Add `ProjectPaths.h3_executable` and `ProjectPaths.h3_model_directory`, then construct one `H3Runtime` from those paths and inject it into `H3Backend` through `Toolkit`. Pass that one registry through parsing, previews, execution, queue submission/claim/recovery/resume, and API rule calculation in every front end. Do not derive machine paths from a job file, `cwd`, or module global; parser-only tests inject a registry with fake runtime paths. The existing front-end `--executable` option continues to select Draw Things only; it must neither override nor be passed to an H3 job. Before reserving an execution ID or creating output files, preflight the H3 executable as a regular executable file, model directory, FFmpeg/FFprobe, and a separately bounded, captured `h3 -d MODEL_DIR --info` probe of model layout and usable Metal device. Classify a missing tool separately from an invalid installation, and name the failing asset. New queue submissions validate job content without requiring the current host to have H3 installed; claim and resume check installation again before starting work.
- H3 runs with its resolved executable and model directory, explicit `-p`, `-d`, `--first-frame`, dimensions, `--seconds`, steps, seed, and `-o` arguments. It validates the required `frames` field against `seconds` but does not pass `--frames`: the checked-in h3.c CLI rejects the two flags together. The adapter uses an argument vector, never a shell. Resolve absolute FFmpeg and FFprobe paths once; reject nonempty inherited `H3_FFMPEG` or `H3_FFPROBE`, and launch H3 with those variables removed and a controlled `PATH` that finds precisely the resolved tools before all inherited entries. Use the same resolved binaries for Framecontrol's normalization, probe, decode, and finishing calls. This avoids H3's `posix_spawnp` selecting a different tool than preflight. Store only the backend-redacted command in previews, events, logs, manifests, history, HTTP, and MCP; the runner gets the unredacted vector. Redaction replaces installation paths with stable labels such as `<h3>`, `<h3-model>`, and `<media-tool>`.
- H3's first input and every handoff are normalized to the exact validated H3 dimensions as an 8-bit sRGB PNG. An aspect-ratio mismatch uses centered crop, never padding or stretching; record the source dimensions and crop/scale plan. The first input is fully decoded before execution, and the exact prepared PNG is what run 1 receives. This must be an adapter-declared input/handoff capability, not inferred from `GenerationMode.is_video` or hard-coded to `i2v`; extend the backend boundary where necessary so H3 receives input checking, resizing, and media-check events without invoking Draw Things color drift or correction.
- Add a backend-neutral frame-extraction policy. The H3 policy decodes the selected final video frame to normal 8-bit sRGB PNG using the stream's declared color information, preserves conventional rounding, and does not apply Draw Things' half-level restoration, ordered dithering, ProRes tagging, color-drift check, or color correction. It writes the handoff atomically/non-overwriting, verifies it decodes as the next run's image, and records it only after that succeeds.
- Reserve the final output name, but run H3 into a unique sibling `.<name>.partial.mp4` that is never a valid chain artifact. Finish and verify that staging file, atomically promote it to the reserved final MP4 only after all output checks pass, then extract the handoff from the promoted file. On child or finishing failure, retain no final output or handoff and clean up the staging file best-effort while reporting its path in diagnostics. This prevents a partial MP4 from being mistaken for a successful run or resume input.
- Before an H3 run is marked successful, FFprobe must establish exactly one video stream and exactly one audio stream, with no other streams. Require the checked-in h3.c contract: H.264 video at the requested width and height, 24 fps, and the computed effective frame count; AAC audio at 32 kHz stereo. Count video frames with FFprobe's frame-counting mode, decode the selected final video frame and a bounded audio interval, and reject invalid timestamps or a difference greater than one 24-fps video frame between the measured audio and video durations (the explicit AAC padding tolerance). A missing, unreadable, mismatched, or unsynchronized stream fails finishing, rather than only producing a warning. Record actual video dimensions, frame count, frame rate, duration, and audio codec, sample rate, channel count, and duration in additive per-run fields/events.
- The H3 adapter's finish is responsible for this verification, atomic promotion, frame extraction, actual-media recording, and H3-specific `MediaChecked` entries. Its FFprobe/FFmpeg calls have bounded timeouts; a cancellation observed during finishing must fail the run before promotion or handoff, while parking is observed only at the existing post-run boundary. Shared execution retains process supervision and public failure/cancellation/timeout mapping. A successful nonfinal run has a verified handoff; the final run need not create an unused handoff unless the current record/resume contract requires it.

## Persistence, queueing, and resume

- Store a safe H3 settings summary in backend-namespaced settings fields. It includes requested and effective temporal values, dimensions, steps, seed source/value, output extension, and the fixed backend identifier; it excludes absolute executable/model paths and any environment. Persist actual-audio fields with actual video properties in run records, execution history, API responses, and manifests.
- A queued H3 job snapshots its validated job text, resolved input/output directories, cooldown policy, backend identity, and safe effective settings exactly as other backend jobs do. It has no Draw Things base-config payload, and snapshot parsing must never invoke Draw Things configuration loading. The model snapshot itself is deliberately not copied or hashed into SQLite: it is an installation dependency that is rechecked before execution and resume. Documentation must state that updating local weights can change later queued or resumed H3 results.
- Queue claim and resume parse the stored H3 snapshot with the H3 adapter and reject a mismatched backend/settings summary, a missing H3 installation, or a missing saved handoff before a child starts. Resume preserves the original resolved seed and starts at the first unfinished run with the last verified H3 handoff; it never recreates a handoff from a partial output and never reads the current job file. Do not require the original first input to remain present or decodable for a resumed run that starts from a saved handoff; validate that original path only when run 1 will execute. Check the handoff's decoded format and dimensions, not merely `is_file()`.

## Planned changes

1. Add the two H3 installation paths to `ProjectPaths`, inject an `H3Runtime` through `Toolkit`, implement `H3Backend`, and register it alongside Draw Things. Remove obsolete Milestone 02 persistence readers and protocol shims rather than preserving them. Keep parser-only tests explicit by injecting a registry with fake runtime paths. Do not change the public one-off `generate` path.
2. Implement the closed H3 parser/schema and H3-specific input normalization. Characterize every invalid field and boundary: mode, absent/extra settings, the required 1–15 second / 24–360 frame range and `frames == llround(seconds * 24)` agreement including half-frame ties, minimum dimensions, pixel cap, 2–1000 steps, seed bound, output extension, negative-key presence, Draw Things fields, input decoding, centered-crop normalization, and path confinement. Pass only `--seconds`, as the checked-in CLI rejects the two temporal flags together.
3. Implement command preparation and preflight. Test command-vector order, arguments, redaction on every stored/exposed surface, no shell, controlled child environment, rejected inherited H3 tool overrides, executable/model/Metal failures, and that the Draw Things `--executable` value cannot affect an H3 command. Exercise the `--info` probe with a fake bounded-process runner in automated tests.
4. Implement H3 staging, finishing, and media persistence. Add a focused FFprobe reader for complete stream inventory and both stream facts, plus bounded decode checks; test staging cleanup and atomic promotion, missing or extra streams, wrong codec/rate/channels/frame rate/dimensions/effective frame count, duration mismatch, unreadable output, successful measurement, and H3 frame extraction. Prove the H3 path never calls Draw Things tagger, half-level extractor, drift checker, or corrector.
5. Adapt the backend contract and common executor only where an explicit backend capability is required: first-input preparation/checks, handoff extraction/validation, and media facts. Keep scheduling, names, process supervision, records, lock, cooldown, cancellation, parking, queue recovery, and status mapping shared. Run fake and Draw Things regression tests after each contract change.
6. Extend queue snapshot parsing, resume validation, events, state rows, serializers, CLI/TUI presentations, HTTP responses, MCP resources/tools, manifests, history import, and API limits. Test M03 Draw Things and H3 queue jobs through submission, worker restart, cancellation, timeout, parking, resume, and missing original input after a successful H3 run. Add user-guide setup/troubleshooting and architecture ownership notes when implementation lands. A human runs the final first-frame H3 acceptance case on the Apple Silicon host, because the Codex execution sandbox cannot expose its Metal device.

## Acceptance criteria

- A valid `i2av` job runs through H3 from the CLI or queue and can be observed through the TUI, HTTP API, and MCP with the same status and history semantics as existing jobs. Its executable and model directory are project-owned, never supplied by the job or Draw Things executable option.
- Each run passes its normalized input image as `--first-frame`; run 2 and later use the preceding successful run's verified final-frame handoff. A resumed chain uses the saved handoff, snapshot, backend, and original seed.
- Each successful run's atomically promoted MP4 has verified H.264 video and AAC audio at the documented H3 properties, dimensions, aligned frame count, and synchronized durations. Actual video and audio properties, output path, and required handoff are recorded; no audio stream or invalid required property fails the run, and a failed run leaves no final MP4 or handoff.
- Unsupported H3 options or combinations, including extra `backend_settings`, `color`, `prompt_pairs[].negative`, Draw Things configuration/size fields, and non-MP4 output fail validation before execution. Missing or invalid executable/model snapshot, FFmpeg, or FFprobe yields a specific tool or input error.
- Cancellation, timeout, worker restart, and resume preserve the existing run-boundary behavior; M03 Draw Things jobs remain supported.
- On the Apple Silicon host, a human verifies a real first-frame H3 run and its generated MP4 against the temporal, video, audio, and final-frame-handoff contracts; the implementation's automated tests cover the same paths with fake process/media tools.
- `make check` passes when the implementation lands.

## Research

The [H3 research notes](../research/h3-c.md) record the upstream CLI behavior and constraints used in this plan.
