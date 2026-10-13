# Milestone 06: H3 video color tags and correction

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 03](milestone-03-h3-i2av.md)'s H3 `i2av` output, finishing and handoff contract, and [Milestone 05](milestone-05-h3-r2av.md)'s mixed `i2av`/`r2av` pipeline.

## Goal

Make H3-generated MP4 color interpretation explicit and give `i2av` jobs optional, bounded controls for measuring and correcting color drift across chained runs.

## Scope

In scope:

- Characterize H3's encoded pixel range, matrix, transfer, and primaries, then ensure every generated MP4 is tagged consistently with its actual pixels. The delivery target is SDR Rec.709: BT.709 primaries, BT.709 transfer, BT.709 matrix, and limited/TV range, provided characterization confirms that encoding or a finishing conversion makes those tags truthful.
- Add H3 color drift measurement and opt-in color-correction controls for the existing `i2av` job contract. Keep the default correction off so jobs without color settings retain their current image-generation behavior.
- Apply the selected correction consistently to the retained corrected video and the final-frame handoff used by the next `i2av` run. Preserve the original generated MP4 as an unmodified artifact when correction is enabled.
- Persist validated policy and per-run results through queue snapshots, resume, execution settings, media checks, manifests, API/MCP, and CLI/TUI history as appropriate.

Out of scope:

- Adding color-correction settings to Ref2VA or changing Milestone 05's `r2av` input behavior. M06 tags all H3-generated videos.
- Changing H3 inference prompts, model weights, audio, frame timing, or generated content to compensate for color drift.
- Automatically correcting existing outputs or rewriting historic MP4s.
- Reusing Draw Things' pixel corrections blindly. H3's source pixels, stream interpretation, and handoff path must be characterized first; share only backend-neutral policy, reporting, or media primitives where their assumptions match.

## Color contract

- Keep the job's color policy in the existing top-level `color` block so the established anchor vocabulary and front-end representation remain familiar. H3 validates that block with an H3-supported closed schema; Draw Things behavior and defaults do not change. H3 jobs continue to reject Draw Things-only controls and unsupported policy fields with field-specific validation.
- Proposed H3 controls: `anchor` (`none`, `previous`, `first`, or `blend`), `strength` (0–1), `first_weight` (0–1, only for `blend`), `reanchor` (`prompt_pair` or `never` for anchor-based policies), and `regions` (boolean). Defaults are equivalent to `anchor: none`; dependent keys are accepted only when relevant. Final bounds and semantics are fixed during implementation characterization before the schema is released.
- `none` performs measurement and tagging only. An enabled correction measures every run against its input and the selected chain anchor, then applies the bounded correction to the video copy and to the handoff. The source generated MP4 remains byte-identical. A failed optional correction records a warning and uses the verified uncorrected handoff, following the established color-correction failure contract.
- Prompt-pair re-anchoring follows the existing policy: when enabled, a run whose prompt pair changes uses that run's input as the new anchor. `first` holds to the first chain image; `previous` limits correction to each run's input; `blend` applies the configured share toward the chain anchor. No setting means no pixel correction.
- Before tagging, establish the actual encoded range and color transform from H3 output and the exact FFmpeg finishing path. Do not add tags that contradict encoded pixels. If H3 output differs from the SDR Rec.709 delivery target, convert staged pixels to the target before tagging, or document a verified compatible target. Limited-range signaling is required only when the samples are actually limited-range; full-range samples must be converted or signaled as full range rather than mislabeled.
- Tagging and correction remain separate operations: tagging does not alter pixels, while color correction does. H3 final-frame extraction and drift measurement decode according to verified stream metadata, including the newly written tags.
- Correction is bounded by a per-run finishing deadline and included in API worst-case duration estimates. Cancellation is honored before promotion; parking remains at the established run boundary. A correction failure does not invalidate a successfully generated H3 run.

## Planned changes

1. Characterize representative H3 `i2av` outputs and the H3/FFmpeg encode path. Determine whether the stream samples are limited or full range and establish the correct matrix, transfer, and primaries. Compare decoded frames and handoffs before and after tagging; decide whether a staged pixel conversion is required for the Rec.709 target.
2. Reuse or extend the MP4 tagging utility so the H3 finisher verifies the existing tags, inserts truthful tags without changing media payload/timestamps where supported, and rejects or safely handles an output that cannot be tagged. Verify the final MP4 and decoded frame metadata with FFprobe.
3. Add the closed H3 `color` policy, validation, resolved settings and queue snapshot representation. Preserve old H3 jobs as `anchor: none`; reject unsupported keys before queueing.
4. Add H3 drift measurement and optional correction in the H3-owned finishing path. Correct the video copy and handoff from one consistent transform, retain the raw generated output, and make the corrected handoff the next run's `--first-frame` only after successful verification.
5. Record tag facts, color drift, correction policy/outcome and relevant artifact paths in media checks and execution history. Surface the policy/results consistently in job previews, queue snapshots, resume, manifests, API/MCP, and CLI/TUI details without leaking machine paths.
6. Characterize boundaries and failure paths with synthetic media and fake runners: tagged and untagged inputs, truthful range/matrix/transfer tags, zero-strength identity, anchor modes, re-anchoring, drift thresholds, clipping and correction caps, failed correction fallback, timeout/cancellation, atomic promotion, resume from a corrected handoff, and Draw Things isolation. A human verifies an actual H3 chain on Apple Silicon.

## Acceptance criteria

- New H3 MP4 outputs have explicit color tags consistent with their encoded samples. At the SDR Rec.709 target, FFprobe reports BT.709 primaries, BT.709 transfer, BT.709 matrix, and the verified range; decoded frames and handoffs agree with those tags.
- Milestone 06 applies the generated-video tagging contract to both `i2av` and `r2av` runs, including an `r2av` run whose output is handed to a later `i2av` run.
- H3 color correction is opt-in. Existing H3 jobs without a `color` block keep the same generated pixels and handoff semantics apart from truthful metadata tags.
- Every H3 run reports color drift. When correction is enabled, the corrected video copy and next-run handoff use the same bounded transform; the original generated MP4 is preserved unchanged.
- Invalid or unsupported H3 color settings fail during job validation with field-specific errors. Resolved settings survive queueing and resume, and correction outcomes are visible in execution history.
- A failed optional correction leaves the run successful, reports a warning, and provides a verified uncorrected handoff. A failed tag verification or required media conversion prevents promotion and handoff.
- Draw Things color behavior is unchanged, and no H3 path invokes Draw Things-only half-level restoration, ProRes handling, or implicit color correction.
- `make check` passes when implementation lands; a human validates a real H3 chained run and the resulting tags on the Apple Silicon host.
