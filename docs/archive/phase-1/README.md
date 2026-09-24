# Phase 1: Basic Functionalities

**Status:** done (2026-09-25)

## Goal

Make `draw-things-control` useful for unattended, repeatable generation work:
describe a job once in a file, then let the app run every generation in it
through the existing `draw-things-cli` wrapper.

## Scope

- A global configuration file, `config/global-config.yaml`, with absolute default input and output directories
- Job definition files (YAML) stored under `data/`
- Running a job as a sequence of `draw-things-cli generate` invocations
- Image-to-image, text-to-video, and image-to-video jobs, selected per job
- Multiple positive/negative prompts and a batch count per job
- Resizing the first input image of `i2v` and `i2i` jobs to a requested size (`desired_input_width`, `desired_input_height`)
- An optional cooldown between runs (`cooldown_seconds`, global default with a per-job override), so long chains do not overheat the machine
- Chaining runs: the first input (or, for text-to-video, the prompt alone) seeds run 1, and each run's output seeds the next
- Timestamped output naming (`<job-name>-<timestamp>-<random>.<ext>`) and a readable run log

## Non-goals

- A GUI or web interface
- Parallel or distributed generation
- Resuming a partially completed job (candidate for a later milestone)
- Prompt templating, wildcards, or parameter sweeps

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| 01 | [Job definition and chained batch runs](milestone-01-job-definition-batch.md) | done |
| 02 | [Runner fixes](milestone-02-runner-fixes.md) (done before 01) | done |
| 03 | [First input image resize](milestone-03-input-image-resize.md) | done |
| 04 | [Cooldown between runs](milestone-04-run-cooldown.md) | done |

## Changelog

Decisions and notable changes are recorded in
[phase-1-changelog.md](phase-1-changelog.md).

## Exit criteria

- A user can write a job file under `data/`, preview it with `--dry-run`, and
  run it to completion with one command.
- Invalid job files are rejected before any generation starts, with an error
  naming the offending field.
- `make check` passes.
