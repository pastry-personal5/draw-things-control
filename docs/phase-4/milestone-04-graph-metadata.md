# Milestone 04: Graph-based metadata

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** The backend identity and durable run records from [Milestone 02](milestone-02-multiple-backends.md); ordered after [Milestone 03](milestone-03-h3-i2av.md).

## Goal

Produce and consume graph-based metadata for the relationships among jobs, executions, runs, inputs, outputs, handoffs, and backends.

## Scope

In scope:

- A documented graph format with stable identifiers, typed relationships, and a versioning rule.
- Metadata production from completed and resumed chains, including provenance across run boundaries and backend identity.
- A documented consumer path that reads the graph and can navigate or query the recorded relationships.

Out of scope:

- Changing generation behavior or replacing the existing manifest and state store solely to introduce the graph.
- Guessing provenance from files when a run never recorded it.

## Planned changes

- Define the graph's nodes, edges, serialization, and compatibility behavior against the run records in place after Milestones 02 and 03.
- Emit graph metadata from the same recorded events and durable identifiers used by history and resume.
- Add a consumer in the appropriate existing interface, then document how to read and use it. Decide the exact consumer surface when this milestone is specified for implementation.

## Acceptance criteria

- A completed or resumed multi-run job produces a graph that links each run to its backend, input, output, and handoff without losing the original execution boundaries.
- A consumer can read the graph and trace an output back to its inputs and generating run.
- The format and its compatibility rules are documented, and `make check` passes when the implementation lands.
