# Milestone 02: H3 text-to-audio-video support

**Phase:** [Phase 5](README.md)
**Status:** planned
**Depends on:** Phase 4 [Milestone 02](../phase-4/milestone-02-multiple-backends.md)'s backend boundary, [Milestone 03](../phase-4/milestone-03-h3-i2av.md)'s H3 runtime and audiovisual finisher, [Milestone 06](../phase-4/milestone-06-h3-color.md)'s H3 color delivery, [Milestone 09](../phase-4/milestone-09-job-definition.md)'s version-2 per-run plans, and Milestones [10](../phase-4/milestone-10-stage-timing.md), [11](../phase-4/milestone-11-time-estimation.md), [14](../phase-4/milestone-14-timing-knobs.md), and [15](../phase-4/milestone-15-timing-gate-and-search.md)'s timing and forecast system.

## Goal

Run H3 text-to-audio-video (`t2va`) as a first-class version-2 pipeline. A T2VA run generates a verified audiovisual clip without an image or reference input, remains independent wherever it appears in a job, and supplies a verified final-frame handoff when the immediately following run takes a visual input.

## Scope

In scope:

- `pipeline: t2va` for `backend: h3-metal-cli` in version-2 defaults and runs, through direct execution, queueing, resume, the CLI and TUI, HTTP, gRPC, MCP, manifests, history, and capabilities.
- H3's existing prompt-only path: the selected positive prompt and ordinary H3 generation controls, with no first-frame or ordered-reference arguments.
- Independent T2VA runs and a conditional final-frame handoff to an immediately following `i2av`, `i2v`, or `i2i` run.
- Existing H3 MP4 verification, color delivery, within-run drift measurement, media facts, cleanup, and artifact recording.
- Stage timing, completion forecasts, evidence-ring retention, provisional learning, held-out validation, and knob files for the new pipeline.
- One standalone T2VA example and one `t2va` to `i2av` example.

Out of scope:

- A version-1 T2VA mode or another legacy top-level job shape.
- Image, video, audio, or ordered-reference conditioning on a T2VA run.
- Prompt rewriting, generated prompt structure, or semantic validation of prompt text.
- Color correction of a T2VA output, joining generated clips, or carrying one run's audio into another.
- A new H3 executable path, model path, checkpoint family, database format solely for T2VA, or required real-host inference run.

## Version-2 contract

- Add `t2va` to the H3 pipelines accepted in `defaults[].pipeline` and `runs[].pipeline`, and advertise it in the version-2 capabilities response. Keep version 1 unchanged: an ordinary version-1 H3 job that names T2VA is invalid.
- A T2VA run selects a positive `prompt_pair` and H3 `settings_profile`. H3's existing negative-prompt rejection remains in force. Framecontrol passes the positive prompt unchanged and makes no claim to validate its language or structure.
- The applicable run knobs are `seconds`, `seed`, and `timeout_seconds`. Resolve requested frames, H3's effective `17n + 5` frame count, and effective seconds exactly as for the existing H3 pipelines. `reference_pair` is forbidden.
- The settings profile uses the existing H3 width, height, steps, rendering, reuse, layer, streaming, quantization, RoPE, compatibility, output, and color keys. Reference-only command options apply only to `r2av`; in particular, a T2VA command never emits `--ref-image-size`.
- A first T2VA run forbids the job's top-level `input`. A later T2VA run also takes no input and does not consume the previous run's output or handoff. It therefore starts a new independent visual and color segment.
- If the next run is `i2av`, `i2v`, or `i2i`, reserve and verify the T2VA run's final-frame handoff and record the next run as consuming it. If the next run takes no visual input or there is no next run, do not create a handoff merely because the run is video.
- Consecutive T2VA runs are independent. Schedule adjacency alone never creates a dependency; only the following visual-input run creates a handoff edge.

## Backend behavior

- Compile each version-2 T2VA run into an adapter-owned one-run view without making T2VA a public version-1 mode. The nested run-view parser may represent the T2VA pipeline internally, but the ordinary legacy parser must continue to reject it.
- Construct the child argument vector with the H3 executable and model, unchanged positive prompt, output path, dimensions, effective frames, steps, seed, and selected performance controls. Emit neither `--first-frame` nor any `--ref-*` argument.
- T2VA uses the FL2VA installation. Preflight requires the FL2VA checkpoint for any unfinished `t2va` or `i2av` run, retains the existing bounded `--info` probe, records the same executable fingerprint and checkpoint inventory, and preserves resume's changed-executable refusal. It does not require Ref2VA unless another unfinished run selects `r2av`.
- Finish the staged MP4 through the existing H3 path: require one H.264 video and one AAC audio stream with the requested media facts, apply the established safe color declaration or conversion policy, measure within-run drift, promote atomically, and clean up partial files on failure. Mark correction `not_applicable` because T2VA has no first-frame input; do not derive a correction anchor from its generated first frame.
- Extract a verified sRGB final-frame PNG only when the compiled plan says that the immediately following run consumes visual input. A missing or unsafe handoff leaves the successful T2VA output intact but blocks its dependent run before that child starts.
- Keep safe command previews and ordinary responses free of installation paths. Records name the backend, `t2va` pipeline, profile, resolved knobs and sources, output media facts, produced output and optional handoff, and the actual consumer relationship.

## Queue, resume, and interfaces

- Freeze the compiled T2VA run in the version-2 queue snapshot like every other run. It has no source input or reference asset to copy. Claim and resume use the saved prompt, profile, knobs, dependency plan, and prior verified handoff rather than rereading the live job file.
- Resume starts at the first unfinished run. Resuming on T2VA needs no predecessor artifact; resuming a visual run after T2VA requires the saved verified handoff from the actual prior execution. Missing or corrupt handoff evidence blocks only the dependent run and is recorded truthfully.
- Include `t2va` in dry-run plans, queue and execution detail, manifests, run events, history rows, serializers, TUI labels, HTTP and gRPC schemas where pipelines are enumerated, MCP results, and the public job-format summary. Preserve the existing generic shapes; the new pipeline string is the additive public value.
- Record durable output and handoff producer/consumer facts in the same form used by other version-2 runs so [Milestone 08](milestone-08-graph-metadata.md) can project T2VA as an input-free producer without guessing from paths or run order.

## Timing and forecasts

- Add `t2va` to every closed pipeline vocabulary used by stage timing, history reconstruction, the evidence ring, calibration and approval files, replay, tuning, and forecast presentation. A T2VA workload records its pipeline, output size, effective frames and seconds, steps, H3 performance controls, an empty reference inventory, and no color-correction feature.
- Until T2VA has its own retained evidence, price it from the existing measured H3 anchor with the same conservative cross-pipeline uncertainty used for an unmeasured H3 pipeline. Do not present borrowed figures as T2VA measurements.
- Completed live T2VA runs enter their own signature and pipeline groups. Provisional checkpoints, owner pins, held-out approval, rollback, retention summaries, and tunables search follow the existing rules and never merge T2VA observations into `i2av` or `r2av` calibration.
- Do not rewrite an existing user timing file merely to add the pipeline. The loader supplies the provisional borrowed baseline when no T2VA section exists; normal successful-run updates create and evolve its section through the guarded writer.

## Planned changes

1. Extend the version-2 pipeline and knob contract, capability summary, compiler, internal H3 run view, input rules, and cross-run edge validation for independent T2VA runs and conditional visual handoffs. Keep the version-1 contract closed.
2. Split H3 command preparation and preflight into explicit `t2va`, `i2av`, and `r2av` behavior so T2VA receives no conditioning flag and requires FL2VA. Preserve all existing controls, redaction, fingerprint, and resume checks.
3. Extend H3 finishing and planning for measure-only T2VA color handling, verified audiovisual promotion, and handoff extraction only for a following visual consumer.
4. Carry the new pipeline through queue snapshots, resume, events, manifests, history, serializers, CLI/TUI, HTTP, gRPC, MCP, and capabilities without introducing a T2VA-specific wire shape.
5. Add T2VA to timing vocabularies, retained evidence, cold-start forecasts, independent learning, validation, tuning, and presentation. Preserve old timing files and their owner pins.
6. Add standalone and `t2va` to `i2av` version-2 examples. Update the user guide, architecture, HTTP/MCP guide, job-format documentation, and Phase 5 changelog when implementation lands.
7. Verify the parser, exact argv, independent-run semantics, mixed-backend handoffs, media finishing, queue/resume behavior, interface serialization, forecasts, learning, backward compatibility, and documentation examples. Run `make check` when implementation lands.

## Acceptance criteria

- A version-2 H3 T2VA run launches with the unchanged positive prompt and ordinary H3 generation controls, with no first-frame or reference argument, and produces a verified H.264/AAC MP4 with recorded video and audio facts.
- T2VA is input-free wherever it appears. A preceding run is never recorded as its source. An immediately following `i2av`, `i2v`, or `i2i` consumes its verified handoff; a following independent pipeline does not.
- Standalone and consecutive T2VA runs create no unnecessary handoff. A handoff failure cannot erase a valid promoted MP4 and prevents a dependent child from starting with missing input.
- T2VA uses measure-only color handling and never runs correction. Existing `i2av` correction, `r2av` references, all H3 settings, Draw Things behavior, and version-1 parsing remain unchanged.
- Direct, queued, interrupted, resumed, and mixed-backend executions expose the same `t2va` pipeline and durable facts through every front end and history surface. Queue and resume never acquire a live input or reference for T2VA.
- A T2VA run receives a conservative completion forecast before pipeline-specific evidence exists, then contributes only to its own retained calibration and guarded updates.
- Version-1 T2VA, a first-run input, a T2VA `reference_pair`, a negative prompt, a missing FL2VA checkpoint, and malformed settings fail before a child starts with a field-specific error.
- Both checked-in examples parse and dry-run as documented, and `make check` passes when the implementation lands. No real Metal inference run is a completion gate.
