# Milestone 08: Unified job definitions

**Phase:** [Phase 4](README.md)  
**Status:** planned  
**Depends on:** [Milestones 02](milestone-02-multiple-backends.md), [03](milestone-03-h3-i2av.md), and [05](milestone-05-h3-r2av.md)

## Goal

Refactor job definitions around one authoritative `runs` list. Each run declares its backend, pipeline, prompt pair, settings profile, and any backend-specific conditioning.

Mixed H3 and Draw Things chains are supported through visual frame handoffs. Audio may be regenerated, muted, or dropped between runs.

## Planned changes

- Replace `h3_runs` and future backend-specific run lists with a unified `runs` list:

  ```yaml
  runs:
    - run: 1
      backend: h3
      pipeline: r2av
      prompt_pair: establish-subjects
      backend_settings: default
      reference_pair: two-subjects
  ```

- Make `runs` authoritative for run count, ordering, backend identity, pipeline, and prompt selection.
- Keep named top-level `prompt_pairs`, but remove their `runs` and `default` assignment fields. Every run selects a catalog entry through `prompt_pair`.
- Use per-run `backend_settings` as a selector for named top-level backend settings profiles.
- Rename the job backend identifier from `draw-things-cli` to `draw-things`; retain `draw-things-cli` as the executable name.
- Remove top-level `mode` and `run_count`. Derive job capabilities and the total run count from `runs`.
- Validate pipeline values by backend:
  - Draw Things: `i2i`, `t2v`, `i2v`
  - H3: `i2av`, `r2av`
- Require `reference_pair` for H3 `r2av` runs and reject it for other runs.
- Refactor parsing, planning, backend selection, execution, finishing, queue snapshots, resume, manifests, records, serializers, HTTP/MCP responses, CLI output, and TUI displays to use per-run definitions.
- Preflight every backend and settings profile selected by the job.
- Carry verified final video frames across backend boundaries. Do not require audio continuity; preserve and record audio when a backend produces it.
- Update examples and tests to the new format. Reject the old `h3_runs`, backend-specific run lists, top-level `mode`, and prompt-pair scheduling fields with field-specific errors.

## Acceptance criteria

- Draw Things-only jobs use the unified `runs` format and retain existing behavior.
- H3 `i2av` and `r2av` jobs use the unified format and retain existing conditioning, reference validation, finishing, and resume behavior.
- Mixed H3/Draw Things chains execute with correct per-run backend selection and visual handoffs.
- Audio may be regenerated, muted, or absent without invalidating a visual handoff.
- Queue, resume, manifests, APIs, CLI, MCP, and TUI expose each run's backend, pipeline, prompt pair, and settings profile.
- Old `h3_runs`, backend-specific run lists, top-level `mode`, and prompt-pair scheduling fields are rejected.
- Documentation, examples, focused tests, and `make check` are updated.

## Documentation changes

Create this milestone document, link it from the Phase 4 README, and add the design decision to the Phase 4 changelog.
