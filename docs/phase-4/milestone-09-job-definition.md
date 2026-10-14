# Milestone 09: Unified job definitions and per-run knobs

**Phase:** [Phase 4](README.md)
**Status:** done
**Depends on:** [Milestones 02](milestone-02-multiple-backends.md), [03](milestone-03-h3-i2av.md), [05](milestone-05-h3-r2av.md), and [06](milestone-06-h3-color.md)

## Goal

Replace the backend-wide job shape with one authoritative ordered `runs` list. Each run selects its backend, pipeline, prompt, settings profile, and run-varying knobs. A job may mix H3 and Draw Things while keeping queueing, resume, history, finishing, and visual handoffs deterministic.

Make duration a per-run choice. H3 users specify `seconds`; Framecontrol derives requested and effective frame counts. Draw Things video runs may specify either exact `frames` or `seconds` when the selected model's frame rate can be resolved before queueing. Never guess a Draw Things frame rate: no checked-in source resolves one today (see [Draw Things frame rate](#draw-things)), so `seconds` is rejected for Draw Things until a model's rate is characterized.

Keep repeated values concise and explicit by placing user-defined defaults under one top-level `defaults` key, scoped by backend and pipeline.

## Scope

In scope:

- Job format version 2 with ordered `runs`, named prompt/reference catalogs, named backend settings profiles, and scoped defaults.
- Per-run H3 duration, reference-pair selection, seed, and timeout; per-run Draw Things frame count or resolvable duration, seed, and timeout.
- Mixed H3 and Draw Things chains with backend-owned input preparation, command construction, finishing, and verified visual handoffs.
- A compiled effective run plan, frozen at queue submission and used by execution, resume, records, manifests, APIs, CLI, MCP, TUI, and Milestone 10 provenance.
- A safe cutover from live version-1 job files while retaining already queued/resumable snapshots and historical records.
- Destination-owned normalization of a handoff that crosses a backend or settings-profile boundary, recorded per run.

Out of scope:

- Audio handoff between runs, automatic prompt rewriting, arbitrary CLI arguments, conditional/default expressions, ranges or random sweeps, and dynamic backend plugins.
- Treating `defaults` as an ordered rule engine. Version 2 has at most one exact default block for each backend/pipeline pair.
- Assuming one Draw Things frame rate for all models or inferring it from an output after the run has already started.

## Version 2 job shape

The canonical key order in documentation and generated examples is job identity and initial input, catalogs, `defaults`, `runs`, then job-wide output and cooldown policy. YAML mapping order is not semantically significant.

```yaml
version: 2
name: mixed-story

prompt_pairs:
  establish-subjects:
    positive: "Use the selected references to establish both people."
  continue:
    positive: "Continue from the supplied first frame."

reference_pairs:
  two-subjects:
    references:
      - kind: image
        path: person-1.png
      - kind: image
        path: person-2.png

settings_profiles:
  h3-portrait:
    backend: h3
    settings:
      width: 768
      height: 1344
      steps: 30
      denoise_reuse: 1
      dit_layers: 50
      core_reuse: 1
      token_reduction: false
      ssd_streaming: false
      int8_row_fc2: false
      reference_rope: false
      reference_image_size: match
      compatibility: {}
  wan-portrait:
    backend: draw-things
    settings:
      config_file: image-to-video-wan-2-2.example.yaml
      config_override:
        model: wan_v2.2_a14b_hne_i2v_i8x.ckpt
        steps: 40
        width: 768
        height: 1344
      output:
        extension: mov
        video_format: prores4444

defaults:
  - backend: h3
    pipeline: r2av
    prompt_pair: establish-subjects
    settings_profile: h3-portrait
    knobs:
      seconds: 5
      seed: 42
      timeout_seconds: 3600
      reference_pair: two-subjects
  - backend: h3
    pipeline: i2av
    prompt_pair: continue
    settings_profile: h3-portrait
    knobs:
      seconds: 5
      seed: 42
      timeout_seconds: 3600
  - backend: draw-things
    pipeline: i2v
    prompt_pair: continue
    settings_profile: wan-portrait
    knobs:
      frames: 81
      seed: 42
      timeout_seconds: 3600

runs:
  - backend: h3
    pipeline: r2av
  - backend: h3
    pipeline: i2av
    knobs:
      seconds: 8
  - backend: draw-things
    pipeline: i2v

output:
  directory: mixed-story
cooldown:
  mode: off
```

List position is the run number, starting at 1. A `run` number inside an item would duplicate list order and is therefore rejected. `runs` is the sole authority for count, order, backend, and pipeline; remove top-level `backend`, `mode`, and `run_count`, and remove `h3_runs`.

A run mapping is closed: `backend` and `pipeline` (required), `prompt_pair`, `settings_profile`, and `knobs`. A `version: 2` file that still contains a removed version-1 key fails at that key with a message naming its version-2 home: `mode` and `run_count` (use `runs`), `backend` and `h3_runs` (use `runs[].backend` and `runs[].pipeline`), `backend_settings`, `config_file`, `config_override`, and `color` (use a settings profile), `run_timeout_seconds` (use `knobs.timeout_seconds`), `desired_input_width`, `desired_input_height`, and `max_input_crop_percent` (use the Draw Things profile's `input_resize`), `output.extension` and `output.video_format` (use the profile's `output`), and a `run` number inside a run item.

Profile `settings` are closed per backend:

- H3: `width`, `height`, `steps`, `render_width`, `render_height`, `denoise_reuse`, `dit_layers`, `core_reuse`, `token_reduction`, `ssd_streaming`, `int8_row_fc2`, `reference_rope`, `reference_image_size`, `compatibility`, `color`, and `output` (only `extension: mp4`). `frames`, `seconds`, and `seed` are run knobs and are rejected here.
- Draw Things: `config_file` (required), `config_override`, `output` (`extension`, `video_format`), `input_resize`, and `color`. `config_override.frame_count` and `config_override.seed` are rejected.

## Catalogs and moved keys

- `prompt_pairs` becomes a name-keyed closed mapping. Each value contains `positive` and optional `negative`; remove `name`, `runs`, and `default` from its entries. A run selects one name directly or inherits one from its matching default block. H3 still rejects a selected prompt containing `negative`.
- `reference_pairs` becomes a name-keyed mapping while retaining Milestone 05's ordered `references` values, confinement, media validation, copying, limits, and label contract. Selection moves to `knobs.reference_pair`; it is required for `h3`/`r2av` and forbidden everywhere else after defaults are resolved.
- `settings_profiles` is a name-keyed closed mapping. Every profile declares one `backend` and a closed backend-owned `settings` mapping. A profile may be reused by compatible runs, must be selected at least once, and cannot be selected by another backend.
- The singular top-level `backend_settings` is removed. H3 `width`, `height`, `steps`, performance controls, compatibility controls, and color/output policy move into an H3 settings profile. H3 `seconds`, `frames`, and `seed` do not: `seconds` and `seed` are run knobs, while frame counts are derived.
- Draw Things `config_file`, `config_override`, output extension/video format, input-resize policy, and color policy move into a Draw Things settings profile. `config_override.frame_count` and `config_override.seed` are rejected in version 2 because temporal control and seed are run knobs. A selected profile's resolved base configuration is snapshotted once and is not reread between runs.
- The job-wide `output` mapping retains only `directory`. A run's selected profile owns its extension and video format, allowing image and video artifacts in one job without a contradictory job-wide extension.
- Top-level `desired_input_width`, `desired_input_height`, and `max_input_crop_percent` move one-for-one into the Draw Things profile's `input_resize` mapping as `desired_width`, `desired_height`, and `max_crop_percent`, with the version-1 rules unchanged: each is an integer or number in its existing range, `max_crop_percent` requires exactly one of the desired sizes, and the existing crop/letterbox plan applies. Without `input_resize`, the input must already match the profile's width and height exactly, as before. The destination run's profile owns normalization of the initial image and of a handoff that crosses a backend or profile boundary. H3 continues to centre-crop to its profile's `width` and `height`.
- Top-level `run_timeout_seconds` moves to `knobs.timeout_seconds`, so a short Draw Things run and a long H3 run need not share one limit. `cooldown` remains job-wide in version 2 because it describes scheduling between runs, not generator configuration.

Keep catalog definitions before `defaults` and `runs` in examples so every selector reads downward from a declared name. Parsers must not depend on that presentation order.

Settings-profile defaults stay backend-owned and are resolved into every selected run. For H3, omitted optional controls retain the established values: native render size, `denoise_reuse: 1`, `dit_layers: 50`, `core_reuse: 1`, `reference_image_size: match`, false Boolean switches, false compatibility switches, MP4 output, and no color correction. For Draw Things, the selected base configuration is resolved first, then `config_override`; omitted output values retain PNG for `i2i`, MOV for video, and `prores4444` for MOV video. These are implementation defaults, not catalog-selection shortcuts, and snapshots store their resolved values and sources. A change to an implementation default after submission cannot alter queued or resumed work.

## Defaults and knob resolution

`defaults` is the only place for user-configured default run values. It is a list of closed mappings keyed by the exact `(backend, pipeline)` pair. Valid pairs are:

- Draw Things: `i2i`, `t2v`, `i2v`
- H3: `i2av`, `r2av`

For each run:

1. `backend` and `pipeline` are required in the run and select at most one exact default block.
2. `prompt_pair` and `settings_profile` resolve from the run, then its default block. Missing values are errors; Framecontrol never chooses the first catalog entry.
3. `knobs` resolves per key: an explicit run value replaces the same key in the matching default block. Omission inherits; explicit `null` is invalid and never means inherit, clear, or auto. `seconds` and `frames` are one temporal choice, so a run's own value of either replaces the default's value of both.
4. Backend defaults apply only after that merge. They may fill documented implementation controls such as false H3 switches and the compatible Draw Things profile's resolved frame count, but cannot invent a prompt, settings profile, reference pair, H3 duration, timeout, or model.

There is no inheritance from the previous run and no fallback from a pipeline-specific block to a backend-wide or global block. Reject duplicate default selectors, unknown or inapplicable keys, unused default blocks, and invalid default values at their `defaults[...]` field even if every run overrides them. "Invalid" covers every check that needs no other run: an undeclared `prompt_pair`, `settings_profile`, or `knobs.reference_pair`; a profile that belongs to another backend; and a knob outside its backend limits, such as H3 `seconds` outside 1–15. Checks that need the merged result, such as a missing `timeout_seconds`, fail at the run. Reject unused prompt, reference, and settings-profile catalog entries so misspelled selectors do not leave dead configuration.

Every resolved knob records its source: `run`, `defaults`, `settings_profile` (a Draw Things frame count taken from the base configuration), `derived` (computed from another knob), `backend` (a backend limit applied, such as the H3 grid), or `execution_seed`. A run with no `seed` knob uses the execution seed: the planner draws it once per execution (or the resumed execution's seed), shares it with every such run, and the run's record stores the actual value with source `execution_seed`. The frozen plan therefore has no seed value for those runs, and a test pins that the shared seed, not a placeholder, reaches each command.

The initial closed knob vocabulary is:

| Knob | Applies to | Rule |
|------|------------|------|
| `seconds` | H3 `i2av`/`r2av`; Draw Things `t2v`/`i2v` when FPS is resolvable | Finite positive requested duration within the backend's limits |
| `frames` | Draw Things `t2v`/`i2v` | Positive exact requested frame count; mutually exclusive with `seconds` |
| `seed` | Every run | Integer `0..2**32 - 1`; omission uses one execution seed shared by all runs that do not override it |
| `timeout_seconds` | Every run | Positive finite child-process limit; required after default resolution |
| `reference_pair` | H3 `r2av` only | Name of one declared pair; required after default resolution |

An `i2i` run rejects both `seconds` and `frames`. Backend settings that vary as a bundle use another named profile rather than expanding `knobs` into an arbitrary command mapping.

## Temporal contract

### H3

- `seconds` is required after defaults resolve and remains in the existing 1–15-second range. The user never writes `frames` for H3 version-2 runs or H3 profiles.
- Compute requested frames as `llround(seconds * 24)`, using the existing halfway-away-from-zero rule. Apply the existing nearest supported `5 + 17*n` grid and cap to obtain effective frames. Pass only the effective `--frames` value to h3.c.
- Validate every run independently. Different `i2av` and `r2av` runs in one job may request different seconds, and an `r2av` reference with audio is checked against that run's effective frame count.
- Record requested seconds, derived requested frames, effective frames, effective seconds, and measured output facts per run. Snapshots and resume use those frozen values, never a later formula or profile edit.

<a id="draw-things"></a>
### Draw Things

- Move exact video frame control from profile-wide `config_override.frame_count` to per-run `knobs.frames`.
- Also accept per-run `knobs.seconds` as a convenience when the selected model exposes a deterministic nominal FPS before queue insertion. Compute requested frames as `llround(seconds * FPS)`, using the same halfway-away-from-zero rule as H3, and record the FPS value and its source (a citation to the research note that established it, not one of the source words above). `seconds` and `frames` are mutually exclusive.
- Do not assume the 16 fps observed for one Wan configuration or derive a planned frame count from a finished file. If the adapter cannot resolve a selected model's FPS authoritatively at submission, reject that run's `seconds` with a field-specific message directing the user to `frames`. This follows the checked-in [Draw Things CLI help](../research/draw-things-cli-generate-help.txt), which defines duration as frames divided by model FPS but exposes `--frames`, not a seconds flag.
- **Characterization result (2026-10-14).** Neither the checked-in configurations under `data/params/` nor the CLI help expose a frame rate, and this project never runs the real CLI in tests or reads its binary. The authoritative source is therefore a table of model file name to `(fps, source)` in the Draw Things adapter that ships empty. An entry may be added only with a research note that records how the rate was measured. Until then every Draw Things `seconds` request fails with the field-specific message above. A test injects a resolver to prove the path: it records `nominal_fps` and its source, derives the frame count, and a rate-less model still fails. The adapter must never read an `fps`-like key from a base configuration, because Draw Things configurations do not define one.
- If neither temporal knob is present, a Draw Things video run may retain the selected profile/base configuration's resolved frame count for compatibility. Record its source as `settings_profile`. When the base configuration has none either, the count is left to `draw-things-cli`, as in version 1, and nothing is recorded. A default `frames` or resolvable default `seconds` overrides it. Still-image runs have no temporal value.
- Accepted frame counts have no model lattice that Framecontrol can verify. `frames` is passed exactly; the child's actual count is a measured output fact (`output_frames`), recorded separately from the request, and a mismatch is a recorded fact, not an assumed rule.

## Run inputs, outputs, and handoffs

- Top-level `input` is the initial visual source. It is required when the first run is H3 `i2av` or Draw Things `i2i`/`i2v`; it is forbidden when the first run is independent H3 `r2av` or Draw Things `t2v` so an unused file is never implied to be conditioning.
- H3 `r2av` consumes only its selected frozen reference pair. Draw Things `t2v` has no visual input. Both start an independent visual segment.
- A later H3 `i2av` or Draw Things `i2i`/`i2v` consumes the immediately preceding successful run's verified visual artifact. An image output can be used directly after destination-owned normalization; a video output supplies its verified final-frame handoff. No run consumes a path merely because it was predicted.
- If the preceding independent or generated run cannot provide the required verified visual artifact, stop before launching the dependent run. An independent later `r2av` or `t2v` run does not need that handoff.
- Each backend validates and prepares the input for the selected destination profile. Record original artifact, prepared copy, crop/scale plan, producer, consumer, and whether audio was present. Audio may be regenerated, muted, or absent and is never carried as implicit conditioning.
- **Normalization at a boundary.** A run that consumes a handoff from a run with a different backend or a different settings profile gets a temporary prepared copy built from the actual artifact by the destination's own rule: the Draw Things profile's `input_resize` plan or its exact-size check, or H3's centre crop to its `width` and `height`. A handoff between two runs of the same backend and profile is used as is, exactly as in a version-1 chain. The prepared copy is recorded in the run's `resized_input`, removed after the run, and a size the destination cannot accept stops the job before the dependent run launches, with a message naming both runs. The record names the producing run and role (`input_run`, `input_role`: `initial`, `output`, or `handoff`; both null for an independent run).
- **Color anchors across runs.** Each run's profile owns its `color` policy. The chain's first image follows run 1 as in version 1: the input copy for an image-conditioned first run, run 1's last frame for `t2v`, none for `r2av`. A run that starts an independent segment (`r2av` or `t2v`, other than run 1) resets the anchor, so a later `first` or `blend` anchor holds to the input of the first handoff-consuming run of that segment, as H3 already does after `r2av`. An anchor is a file path, so it crosses a backend boundary; a run with no usable anchor measures drift only and records why.
- Plan output extension, finishing cost, storage bound, correction policy, and handoff requirement per run. The preview reserves every run's names so none collides, including when extensions differ; each run takes its names when it starts, as in version 1, and nothing is overwritten.
- **Execution-level fields.** A version-2 execution has no single backend, mode, model, base configuration, or timeout. Its queue row, execution row, manifest, and `JobStarted` carry `backend` (the one backend ID when every run shares it, else `mixed`), `mode` (the one pipeline when uniform, else `mixed`), an empty `model` and `config_file` unless uniform, and `backend_settings` holding `{"format_version": 2, "runs": [...]}`, the frozen plan. Shared code never uses these for a decision: each decision reads the run's own backend and pipeline.

## Validation, persistence, and resume

- Compile source YAML into an immutable effective run plan before queue insertion. Each compiled run contains its number, backend, pipeline, selected catalog names, fully resolved knobs and their sources (see above), safe effective backend settings, expected artifacts, input dependency, and resource bounds.
- Validate every selected backend/profile/pipeline combination and every cross-run edge before copying references or inserting the queue entry.
- Freeze the job text, compiled plan, every selected Draw Things base configuration, H3 reference copies, effective temporal calculations, and safe settings in the queue snapshot. Resume reads that snapshot and the last verified dependency; it does not reparse a live job, profile, parameter file, or reference source.
- **Snapshot version 7** is the version-2 snapshot. `job_text` is the version-2 source; `config_text` is a JSON object mapping each selected Draw Things profile name to its base-configuration text (H3 profiles have none); `effective_plan` is the compiled plan, compared on every read with the plan the frozen text recompiles to, and a difference makes the entry non-runnable with a diagnostic. Snapshots 1 to 6 keep their meaning.
- Refactor planning and execution around a per-run backend adapter. Remove shared assumptions that one `JobDefinition.backend`, mode, extension, seed, settings object, or finisher applies to the whole chain. Shared code owns order, cooldown, cancellation, run-boundary resume, events, and record coordination. Each compiled run holds a `backend_job`, a one-run job view that its own adapter parsed and validated. The adapters execute that view unchanged, so the version-1 command, finishing, and color code is reused without a second code path. Version 1 never appears in a file; the view is an internal shape.
- Every shared-code read of a job-wide field goes through uniform per-run accessors (`run_backend`, `run_needs_visual_input`, `run_checks_input`, `job_for_run`, `mode_label`) that answer for a version-1 job from its own fields and for a version-2 job from its compiled run. Version-1 behavior stays as before; the characterization goldens change only by the new nullable run columns and the new `RunStarted` keys, which are null or empty for version 1.
- Persist backend, pipeline, prompt-pair name, settings-profile name, requested/effective knobs and their sources, and the consumed artifact's producer run and role with each run; the produced roles are the run's existing `output`, `last_frame`, and `corrected_output` columns. Milestone 10 adds the dedicated artifact, reference-copy, and retained-copy records it needs. Queue/API limits sum each run's own timeout, finishing bound, output estimate, and applicable cooldown rather than multiplying one job-wide estimate.
- Preflight tools and model assets, at queue claim and at execution start, for each backend selected by a run that has not finished; a version-2 job with only Draw Things runs never needs H3 installed, and the reverse.
- Keep commands backend-redacted and settings safe. Public records expose catalog names and effective non-secret values, not private reference snapshot paths or installation paths.

## Compatibility and migration

- New and replaced job files use `version: 2`. A version-1 file submitted after the cutover fails with one actionable message pointing to version 2; individual removed keys still have field-specific diagnostics when version 2 contains them. Do not reinterpret an unversioned or version-1 mapping as version 2.
- **Where the cutover applies.** The parser takes an `allow_version_1` option that is false by default. Every live entry point leaves it false: `validate-job`, the TUI's job list and plan, queue submission, and the HTTP and MCP validate, preview, create, replace, and submit calls. Only the queue-snapshot reader sets it, and only for snapshots of version 1 to 6. A version-7 snapshot must hold version-2 text. The per-run views the adapters parse are built from mappings, not text, so they do not pass through the file check.
- Continue reading already queued, running, parked, or resumable version-1 snapshots **unchanged on their existing path**. They are not rewritten, converted, or given a compiled plan in storage: a snapshot is the evidence of what was queued, so it keeps its original source text, backend ID, prompt schedule, settings, timing, seed, references, and handoff behavior. The uniform per-run accessors derive any per-run view that a front end shows when it is read, and nothing derived is written back. A version-1 snapshot that no longer parses stays non-runnable with a diagnostic, as before. This replaces the earlier idea of migrating each snapshot once: a one-time rewrite adds a failure mode and no behavior the accessors do not already give.
- Historical manifests and execution rows remain readable. Preserve their recorded backend spelling and missing per-run facts. New runs use `draw-things`; `draw-things-cli` remains the executable name and the historical backend value for older records, not an accepted version-2 backend selector.
- Update all checked-in job examples to version 2 when implementation lands. Do not edit user parameter files or `config/global-config.yaml`.

## Implementation order

1. Restore a green baseline and keep it green: the version-1 characterization goldens, event wire round trip, and H3 finishing tests are the compatibility contract, and capture queue snapshots, resume, manifests, and displays of version 1 in fixtures. Record the Draw Things frame-rate characterization in research.
2. Add typed catalogs, scoped defaults, closed knob schemas, exact resolution/source tracking, and the immutable effective run model. Parse version 2 without changing execution first, and prove it against the example jobs.
3. Move H3 temporal and seed values and Draw Things temporal/seed/configuration values to their new owners. Add per-run argument construction and validate H3 duration derivation plus Draw Things `frames` and injected-resolver `seconds` behavior.
4. Generalize planning, preflight, artifact reservation, finishing, resource limits, events, recording, queue claim/recovery, and resume to the compiled per-run backend plan and cross-backend visual dependencies, with boundary normalization and per-segment anchors.
5. Add the version-7 snapshot reader and writer and the schema migration. Make per-run backend, pipeline, catalog selections, effective knobs, and the consumed producer run and role durable for Milestone 10. Cut over live version-1 files at the entry points.
6. Update serializers, HTTP/MCP responses, CLI/TUI summaries and details, architecture, user guide, HTTP examples, and every checked-in example. Remove obsolete version-1 authoring paths only after compatibility fixtures pass.
7. Test default precedence and diagnostics; every backend/pipeline transition; differing durations and settings profiles; independent runs; missing handoffs; reference snapshots; interruption, recovery, and resume; old snapshots/history; resource limits; redaction; and Draw Things/H3 regression behavior. Run `make check`.

## Acceptance criteria

- One job can run `h3/r2av → h3/i2av → draw-things/i2v → h3/i2av`, with each run using its own prompt, profile, seed, timeout, duration/frame request, and conditioning.
- Two H3 runs can request different `seconds`; neither accepts a user `frames` value. Their requested frames, effective grid counts, commands, records, and resume behavior are derived and frozen independently.
- Draw Things video runs can request different exact `frames`. A `seconds` request works only with an authoritatively resolved model FPS and records that FPS/source; otherwise validation fails before queueing without guessing. Because the authoritative table ships empty, every real Draw Things `seconds` request is rejected today, and a test with an injected resolver proves the accepted path.
- An H3 `r2av` default can provide `reference_pair` and `seconds`, an H3 `i2av` default can provide a different `seconds`, and a run override changes only the named knob. Duplicate, unused, unknown, inapplicable, null, or invalid defaults fail at their exact fields.
- The effective plan has no dependence on YAML ordering, previous-run configuration inheritance, live catalog/config edits, or unrecorded backend defaults. Queue recovery and resume reproduce the frozen plan.
- Mixed image/video outputs and backend transitions consume only verified artifacts. Audio continuity is never implied, and an independent run can proceed without an unavailable prior handoff.
- New records and all front ends expose per-run backend, pipeline, prompt pair, settings profile, effective knobs with sources, and measured output facts. Milestone 10 can build causal edges without parsing commands or filenames.
- Version-1 live job files are rejected after cutover with migration guidance, while pre-cutover queued/resumable snapshots and historical records remain readable and do not silently change behavior. The version-1 characterization goldens differ from before only by the new nullable run columns and the new `RunStarted` event keys (`backend`, `pipeline`, `settings_profile`, `knobs`, `knob_sources`, `input_run`, `input_role`), which are null or empty for version 1.
- A run that consumes a handoff across a backend or profile boundary uses a destination-normalized copy, and a size the destination cannot take stops the job before that run launches.
- Documentation, examples, schema/error tests, migration fixtures, focused integration tests, and `make check` are updated and pass when implementation lands.

## Documentation changes

When implementation lands, update the Phase 4 README, user guide, architecture, HTTP/MCP examples, all checked-in job examples, and Phase 4 changelog. Document version 2 as the only authoring format, with one full mixed example and small examples for H3 per-run seconds, H3 `r2av` defaults, and Draw Things per-run frames. Draw Things `seconds` is documented as rejected until a model's rate is characterized; its accepted form is exercised only by an injected-resolver test. User job files under `data/jobs/` that are not checked in are the owner's: they are not rewritten, and the cutover message tells the owner how to convert them.
