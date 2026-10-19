# Milestone 16: Idle, parallel validation and canonical job filenames

**Status:** done

## Goal

Keep the TUI responsive while giving loopback submissions a fresh, local fast-validation gate.

## Delivered scope

- A job's exact filename stem is its `name`; the comparison is case-sensitive and has no separator normalization.
- Every normal parser entry point enforces that rule. Queue snapshots predating version 8 retain their historical names for resume compatibility; new version-2 snapshots use version 8.
- H3 fast validation visits every selected `r2av` reference, including paired audio, and checks confinement, regular-file status, link safety, sizes, headers, and durations without decoding media.
- The Jobs list starts its background read after the initial render. Loopback TUI submission logs `Validating …`, validates immediately with `decode_input=False`, retries one detected file/reference race, and refuses locally without an HTTP request on failure. Remote URLs show `Local validation off` and use server-only validation.

## Acceptance

The parser, queue resume path, examples, Jobs-list presentation, and TUI submission path all share the canonical-name and fast-reference preflight rules.
