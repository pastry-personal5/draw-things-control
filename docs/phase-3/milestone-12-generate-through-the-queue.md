# Milestone 12: Generate Through the Queue

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 03: Queue for people](milestone-03-queue-for-people.md), [Milestone 11: Safety hardening](milestone-11-safety-hardening.md)

This milestone is named and not yet planned: it records an owner decision
(2026-10-03) so the phase document lists everything the phase will build.

## Goal

`dtc generate` runs through the queue like every job, so `dtc serve`'s worker
is the only thing that ever starts `draw-things-cli`, as the phase's exit
criteria say. Today `dtc generate` starts it directly for one image or video,
holding the run lock the server holds while it runs, so it never overlaps a
job. [Milestone 11](milestone-11-safety-hardening.md) documents it as the one
exception until this milestone is built.

## Open design questions

It needs a design of its own, since a queue entry is a snapshot of a job file
and a one-off has none:

- What the entry snapshots, and what shows in the queue, the history, and the
  audit log for a run that came from a command line
- Which of `generate`'s options a job file can express, and which the
  API's rules and limits (a run timeout, the input and output directories)
  must bound, since `generate` today accepts any path and any option,
  credentials included
- Whether MCP gets it (the first plan says agents express only what a job file
  can express)
- What `dtc generate` does while the server is down, once the worker is the
  only thing that starts `draw-things-cli`

## Acceptance criteria

To be written when the milestone is planned.
