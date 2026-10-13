# Milestone 05: H3 controls and mixed reference pipelines

**Phase:** [Phase 4](README.md)
**Status:** in-progress
**Depends on:** [Milestone 03](milestone-03-h3-i2av.md)'s H3 installation, one-shot runner, MP4 finishing, and queue snapshots.

## Goal

Expose reproducible h3.c rendering controls and let one H3 job combine first-frame-conditioned image-to-audio-video (`i2av`) runs with Ref2VA reference-to-audio-video (`r2av`) runs.

`r2av` is the public name for H3's Ref2VA model family. A detailed per-run definition selects the pipeline and, for `r2av`, the named reference pair used by that run.

## Scope

In scope:

- Optional H3 render, speed, memory, numerical-compatibility, and reference-image sizing controls for all H3 runs.
- `backend: h3`, `mode: h3av`, a complete `h3_runs` pipeline plan, and named ordered `reference_pairs` for Ref2VA image, video, video-with-separate-audio, silent-video, and audio inputs.
- A mixed chain in which an `r2av` run can seed a later `i2av` run through its verified final frame. Any `r2av` run remains reference-conditioned and never receives a frame anchor.
- Validation, confinement, media preparation, command construction, queue/resume snapshots, records, manifests, HTTP/MCP, and CLI/TUI for the selected pipeline and reference pair of every run. Persist the provenance facts that Milestone 10 will later project into a graph.

Out of scope:

- h3.c's interactive UI, `--show`, `--zoom`, `--frames-dir`, `--profile`, and user-selected `--info`; Framecontrol supervises a noninteractive child and owns progress, preflight, and artifacts. Its bounded internal `--info` probe remains required.
- Automatic H3 Context-IR prompting or H3 Regenerate-2K. H3 receives the selected positive prompt as written.
- H3's direct last-frame and first-and-last-frame pipeline. A later `i2av` run can use a prior generated output only as its first frame.

## Pipeline contract

`h3av` is a video-only H3 job mode. It has `run_count`, the existing named `prompt_pairs`, and one `h3_runs` entry for every run number. `h3_runs` is a closed ordered mapping list with exactly the numbers `1..run_count` once each:

```yaml
h3_runs:
  - run: 1
    pipeline: r2av
    reference_pair: performer
  - run: 2
    pipeline: i2av
  - run: 3
    pipeline: r2av
    reference_pair: location-and-voice
  - run: 4
    pipeline: i2av
```

- `pipeline` is `i2av` or `r2av`. `reference_pair` is required only for `r2av`, names exactly one declared pair, and is forbidden for `i2av`. The existing `prompt_pairs[].runs` and `default` fields still select the prompt for each run; the detailed H3 plan selects conditioning independently.
- A job whose first `h3_runs` entry is `i2av` requires the existing top-level image `input`. A job beginning with `r2av` forbids `input`. Every later `i2av` run consumes the verified extracted final frame of its immediately preceding successful run, whether that predecessor was `i2av` or `r2av`.
- An `r2av` run receives only its selected reference pair and never a first or last frame. Consecutive `r2av` runs are intentionally independent generations; the earlier output is retained in history but is not an input to the next one. An `r2av` final frame is extracted and retained when, and only when, the next planned run is `i2av`.
- Existing `mode: i2av` jobs remain valid and retain their current all-`i2av` schedule. `mode: r2av` is not introduced: the explicit `h3av` plan avoids a second incompatible top-level job shape and makes each run's conditioning visible.

## Reference pairs

`reference_pairs` is a nonempty closed list only when an `h3_runs` entry uses `r2av`. Each pair has a unique `name` and an ordered `references` list. The pair is the complete Ref2VA conditioning set for each run that selects it:

```yaml
reference_pairs:
  - name: performer
    references:
      - kind: image
        path: singer.png
      - kind: audio
        path: singer-voice.wav
  - name: location-and-voice
    references:
      - kind: video
        path: candlelit-stage.mp4
      - kind: video_audio
        path: performance.mp4
        audio: alternate-vocal.wav
```

- A reference entry is a closed mapping. `image`, `video`, `silent_video`, and `audio` require `kind` and `path`; `video_audio` additionally requires `audio`. They map, in list order, to `--ref-image`, `--ref-video`, `--ref-silent-video`, `--ref-audio`, and `--ref-video-audio VIDEO AUDIO`.
- Each pair allows at most 12 entries, 9 images, 3 videos, and 3 audio inputs. `video` and `video_audio` each count as one video and one audio input; `silent_video` counts only as a video. A pair needs at least one image or video. `video` requires a decodable embedded audio stream; `silent_video` ignores any embedded audio; `video_audio` uses its separate audio file instead of the video's soundtrack. Enforce a 2–15 second source-video duration and a 2–15 second duration for each consumed audio input, with at most 15 seconds of total consumed audio per pair. For audio associated with a video, measure the span H3 will decode after its output-frame cap and soundtrack truncation as well as the source duration. A video with audio also requires enough effective output frames for H3's two-second minimum (at least 56 effective frames). Include all these checks in API limits and preflight.
- Every `path` and `audio` resolves against the snapshotted global input directory, whether or not the job has a top-level `input`. Reject links, nonregular files, and paths outside that directory. Fully decode images and inspect the exact audio/video streams with the resolved FFprobe/FFmpeg pair before queue submission. A field-specific error names the pair, entry, and field that fails. Require every declared pair to be selected by at least one `r2av` run; forbid `reference_pairs` when no run selects `r2av`.
- Reference labels are numbered in the declared order within the pair, independently by media kind. Documentation provides the six-section Ref2VA prompt structure: `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, and `non_diegetic_music`. The prompt for an `r2av` run must use labels that match the pair selected for that run; Framecontrol documents this contract but does not rewrite prompt text.

## H3 controls

The Milestone 03 keys remain required in `backend_settings`:

```yaml
backend_settings:
  width: 768
  height: 1344
  frames: 120
  seconds: 5
  steps: 30
  seed: 42
  render_width: 576
  render_height: 1008
  denoise_reuse: 2
  dit_layers: 45
  core_reuse: 1
  token_reduction: false
  ssd_streaming: false
  int8_row_fc2: false
  reference_rope: false
  reference_image_size: match
  compatibility: {}
```

- Omitted optional values resolve to h3.c's checked-in defaults: native render size, `denoise_reuse: 1`, `dit_layers: 50`, `core_reuse: 1`, and false for Boolean controls. Records and snapshots contain all resolved values. The resolved settings are an audit contract; command construction must not pass mutually incompatible default flags together.
- `render_width` and `render_height` occur together or not at all, obey normal H3 dimension rules, are no larger than the output canvas, and have its exact aspect ratio. They affect inference only; output verification still requires the requested output dimensions.
- `denoise_reuse` is 1–3, `dit_layers` is 35–50, `core_reuse` is 1–6, and `token_reduction`, `ssd_streaming`, `int8_row_fc2`, and `reference_rope` are booleans. `denoise_reuse > 1` and `core_reuse > 1` are mutually exclusive; reject that combination during job validation, before queueing. Emit at most one of `--reuse` and `--core-reuse` with a value greater than 1. `int8_row_fc2` conflicts with both `ssd_streaming` and `compatibility.bf16_mlp`; reject either combination before queueing. `int8_row_fc2: true` emits the exact flag `--use-int8-row-fc2` for every H3 run; false omits it. This option requires an M5-class Metal 4 GPU, which preflight must confirm before starting a child, and is less numerically conservative than grouped int8. `reference_image_size` is `match` or `max` and maps to `--ref-image-size` only for `r2av` runs; reject `max` when the job has no `r2av` run.
- The resolved settings map to the checked-in one-shot CLI as follows: `render_width`/`render_height` → `--render-width`/`--render-height`; `denoise_reuse` → `--reuse`; `dit_layers` → `--layers`; `core_reuse` → `--core-reuse`; `token_reduction` → `--token-reduction`; `ssd_streaming` → `--ssd-streaming`; `int8_row_fc2` → `--use-int8-row-fc2`; and `reference_rope` → `--use-reference-rope`. Numeric options are emitted only when explicitly selected or needed to preserve a non-default resolved value; Boolean switches are emitted only when true. In particular, do not emit both `--reuse` and `--core-reuse` at values above 1, or `--ssd-streaming` and `--use-int8-row-fc2` together.
- `compatibility` is a closed Boolean mapping for h3.c's slower reference paths: `bf16_mlp`, `bf16_qkv`, `bf16_attention_output`, `row_major_attention_output`, `unfused_int8_inputs`, `unfused_qkv_rope`, `scalar_qkv_rms`, `uncached_int8_scales`, `dynamic_fc1_k`, and `grouped_quantizer`. They map respectively to `--use-slower-bf16-mlp`, `--use-slower-bf16-qkv`, `--use-slower-bf16-attention-output`, `--use-slower-row-major-attention-output`, `--use-slower-unfused-int8-inputs`, `--use-slower-unfused-qkv-rope`, `--use-slower-scalar-qkv-rms`, `--use-slower-uncached-int8-scales`, `--use-slower-dynamic-fc1-k`, and `--use-slower-grouped-quantizer`; all default to false.
- Queue submission records all resolved controls and explicit CLI switch choices without requiring a local H3 installation, as in Milestone 03. At claim, a bounded `--info` probe records the reported H3 version, Metal capability, checkpoint inventory, and a digest of the executable in execution history. Resume checks that fingerprint against the prior execution before a child starts; it refuses a changed executable even if its reported version string is unchanged. The model snapshot remains Milestone 03's mutable installation dependency. Emit explicit numeric defaults where needed to avoid silently adopting changed executable defaults; never emit a false Boolean switch.

## Example mixed job

```yaml
version: 1
name: performer-story
backend: h3
mode: h3av
run_count: 4
prompt_pairs:
  - name: introduce
    positive: |
      subject_definitions:
      <Subject 1> is the singer from <Picture 1>.
      <Audio 1> is the vocal-timbre reference for <Subject 1> (S1).

      summary:
      [reference generation + audio reference] <Subject 1> sings in a candlelit studio.

      retention_analysis:
      <Subject 1> (appears in [Shot 1]): fully_preserved - the singer's appearance remains recognizable.
      <Audio 1>: reference - the target voice follows its timbre without copying the source signal.

      detailed_description:
      [Shot 1] <Subject 1> (S1) sings one quiet verse at a vintage microphone as the camera slowly pushes in.

      overall_soundscape:
      Quiet studio room tone under close-miked singing.

      non_diegetic_music:
      None.
    runs: [1]
  - name: continue
    positive: "The singer steps away from the microphone and walks toward the window."
    runs: [2, 4]
  - name: relocate
    positive: "Use the selected references to stage a new performance in the described location."
    runs: [3]
h3_runs:
  - run: 1
    pipeline: r2av
    reference_pair: performer
  - run: 2
    pipeline: i2av
  - run: 3
    pipeline: r2av
    reference_pair: location-and-voice
  - run: 4
    pipeline: i2av
reference_pairs:
  - name: performer
    references:
      - kind: image
        path: singer.png
      - kind: audio
        path: singer-voice.wav
  - name: location-and-voice
    references:
      - kind: video
        path: candlelit-stage.mp4
      - kind: audio
        path: alternate-vocal.wav
backend_settings:
  width: 768
  height: 1344
  frames: 120
  seconds: 5
  steps: 30
  seed: 42
output:
  extension: mp4
cooldown:
  mode: off
```

Run 1 uses `performer`; run 2 receives run 1's verified final frame; run 3 uses `location-and-voice` independently; run 4 receives run 3's final frame.

## Execution, persistence, and resume

- At queue submission, after validation and before making the entry claimable, copy every selected reference asset into a private, queue-owned input snapshot. Open each source without following links, verify the opened file is regular and still under the snapshotted input directory, copy from that same file descriptor, and atomically publish the copied file with a digest and measured media facts. Store kind, pair name, ordinal, field (`path` or `audio`), basename, digest, and snapshot role in the queue snapshot; bound individual and aggregate copy sizes and the submission time. If copying or queue insertion fails, clean up the unpublished snapshot. An `r2av` child receives only verified copies, so editing or deleting a source after submission cannot change queued, later, or resumed runs.
- Keep the queue-owned copies through resumable terminal states and all descendants that reuse them; reclaim them when retention can no longer resume or trace the chain. Resume and claim verify the saved copy's digest and media facts before use and do not replace a missing or corrupt copy from the live source. A direct, nonqueued job uses the same verified copy contract before its first child and cleans up the private copies when execution ends. Never put private snapshot paths in public commands or ordinary API responses.
- Build every child command as an argument vector. All runs receive the executable/model paths, positive prompt, output, dimensions, timing, seed, and applicable resolved controls. Continue Milestone 03's `--seconds`-only command contract; never pass both `--seconds` and `--frames`. `i2av` adds `--first-frame` for its selected chain input. `r2av` adds the selected pair's reference flags in declared order and no anchor. Commands shown outside the child redact installation and private input paths while keeping pipeline, flags, pair name, ordinal, and basenames.
- H3 outputs use existing staged MP4 promotion and H.264/AAC verification. The planner reserves a handoff path for every run whose next run is `i2av`, irrespective of the current run's pipeline. It extracts, decodes, records, and retains the final frame only after promotion succeeds.
- The plan, resolved controls, H3 fingerprint, prompt schedule, selected pair per run, reference inventory, and handoff dependency are durable in queue snapshots or execution settings as appropriate, and in runs, events, manifests, serializers, API/MCP responses, and TUI/CLI detail. Record artifact identities and producer/consumer relationships so Milestone 10 can project the graph later without guessing from paths.
- Resume starts at the first unfinished run. It reads the saved detailed plan, reference snapshots, and prior verified handoff. It does not reread the job file, source reference files, or current pair definitions. Verify only the selected pair's copies for each unfinished `r2av` run; a missing copy blocks the run that selects that pair. A missing predecessor handoff blocks the dependent `i2av` run. Record which copy and handoff each run actually consumed, including across resume executions.
- H3 preflight reports FL2VA and Ref2VA checkpoint availability separately. An execution with at least one `r2av` run requires Ref2VA before it starts; all-`i2av` jobs preserve Milestone 03's requirements.

## Planned changes

1. Add `GenerationMode.H3AV` and backend-owned detailed-run capabilities. Keep the established `i2av` parser and run behavior intact while allowing H3AV to determine first-input requirements and successor handoffs from `h3_runs`.
2. Parse and validate an exact `h3_runs` plan and named `reference_pairs`, including per-run pair selection, prompt schedule agreement, unique names/numbers, all structural restrictions, and field-specific diagnostics. Document reference labels as a prompt-writing contract; do not claim to validate natural-language labels automatically.
3. Extend H3 settings, command construction, safe summaries, redaction, persisted settings, and API-limit calculation for every supported H3 control. Map every setting to the exact checked-in CLI spelling, preserve resolved defaults in snapshots without emitting incompatible switches, and validate the `--reuse`/`--core-reuse`, `--ssd-streaming`/`--use-int8-row-fc2`, and BF16-MLP/row-FC2 exclusions before queueing. Characterize defaults, version and executable fingerprint checks, and M5 capability preflight.
4. Add reference confinement, link and regular-file checks, image decoding, audio/video probing, stream and per-pair count/duration limits, bounded private snapshot copying/digests at submission, retention and cleanup, and Ref2VA checkpoint preflight.
5. Adapt run planning, output paths, finisher, queue recovery, and resume so a verified final frame crosses only an `i2av` dependency edge and never enters an `r2av` command.
6. Extend records, events, manifests, serializers, CLI/TUI, HTTP/MCP, the user guide, and architecture for detailed plans, pairs, snapshots, and dependency edges. Leave graph projection to Milestone 10, backed by these recorded facts.
7. Verify all `i2av`/`r2av` transitions, independent consecutive `r2av` runs, all reference kinds and limits, malformed/changing inputs, snapshot submission/retention and resume paths, command order/redaction, control boundaries/defaults and exclusions, changed executable with the same reported version, M5 capability failure, and Draw Things regression behavior. Run `make check` when implementation lands.

## Acceptance criteria

- A single H3AV execution can perform `r2av → i2av → r2av → i2av`; each child receives exactly the conditioning described by its own detailed definition, and each `i2av` run receives the prior verified output frame.
- An `r2av` run receives its selected ordered pair only. It never receives a frame anchor, and an independent following `r2av` run does not consume its output.
- Reference snapshots and resolved controls stay stable through queueing, interruption, recovery, and resume; changing or deleting a submitted source does not change the verified copy. The selected settings produce the exact supported H3 argv, including `--use-int8-row-fc2` when `int8_row_fc2` is enabled. Incompatible setting pairs, unsupported hardware, and a changed H3 executable fail before a child starts. Invalid, linked, out-of-directory, malformed, over-limit, audio-only, missing, or changed assets fail with a field-specific error before H3 receives them.
- Existing all-`i2av` H3 jobs, Draw Things jobs, strict MP4 verification, command redaction, and queue/resume semantics remain intact.
- H3 color characterization, tags, and any required pixel conversion are deferred to [Milestone 06](milestone-06-h3-color.md).
- `make check` passes when implementation lands.
