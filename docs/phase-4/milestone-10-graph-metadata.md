# Milestone 10: Graph-based metadata

**Phase:** [Phase 4](README.md)
**Status:** planned
**Depends on:** [Milestone 02](milestone-02-multiple-backends.md)'s backend identity and durable run records, and [Milestone 03](milestone-03-h3-i2av.md)'s audio/video output facts. Implementation follows Milestone 03; the graph contract also covers Draw Things history.

## Goal

Export and read trustworthy provenance for a job's executions and runs. Starting from an output, a person or tool can follow the recorded producer, its input or handoff, earlier successful runs across resumes, the immutable job snapshot, and the selected backend.

## Scope

In scope:

- A DTC-owned, versioned JSON node/edge format, with a checked-in schema and an offline reader.
- A graph projected on request from the SQLite execution history, covering the selected job execution and its recorded resume ancestors. The same projection powers an authenticated HTTP read and a CLI JSON export.
- An offline CLI trace that consumes an exported file and walks backward from a selected artifact to its recorded inputs and producer. It does not require `dtc serve` or the original database.
- Successful and unsuccessful runs, partial outputs, input preparation, video handoffs, corrected copies, H3 audio/video facts, and explicitly missing provenance.

Out of scope:

- Graph editing, visual layout, a graph database, automatic sidecar files, and replacing the queue snapshot, manifest, or SQLite store with the graph.
- One-off `generate` requests and jobs that have not started an execution.
- Reconstructing unrecorded history by scanning media files, parsing saved commands, or matching a filename alone.
- Downloading media or including prompt text, commands, job YAML, credentials, or file contents in the graph.

## Graph contract

- Publish a normative `dtc-provenance-graph` version 1 JSON schema and examples in `docs/graph-format.md`, with a machine-readable schema under `schemas/`. A document contains `schema`, `version`, a persistent `store_id`, `root` (for example, `execution:E0012`), `complete`, sorted `nodes`, sorted `edges`, and sorted `unresolved` references. Each unresolved entry names its known source node, intended relationship, unavailable reference, and reason; `complete` is true exactly when that list is empty. Its IDs are stable within a store; `store_id` distinguishes two databases that both have an `E0012`. Generate and persist the store ID in a forward-only state migration, not during a read.
- Node types are `job_snapshot`, `backend`, `execution`, `run`, and `artifact`. A job snapshot is scoped to an execution's stored job text or settings; a live `J...` file is not an immutable definition. A run ID includes both execution ID and run number, so resumed run 3 in `E0013` cannot collide with run 3 in `E0012`. Artifact IDs identify a recorded role and producer, such as `artifact:E0012:run:2:output` or `artifact:E0012:run:2:handoff`; a source input is scoped to the first execution that recorded it. Paths are properties, never IDs.
- Typed directed edges are `uses_job_snapshot` (execution to snapshot), `uses_backend` (execution to backend), `contains` (execution to run), `consumes` (run to artifact), `produces` (run to artifact), `derived_from` (artifact to artifact), `follows` (run to its verified predecessor), and `resumes_from` (execution to the execution holding its resume point). An edge must join nodes present in the document. The offline reader validates IDs, types, endpoints, and schema version before tracing.
- Include only recorded facts: execution/run status, run number and prompt-pair name, backend ID, artifact role and recorded path, whether the producing run succeeded, measured video properties, and M03's measured audio properties when available. A recorded path is not proof that a file still exists; export does not read or hash media. Failed runs may have incomplete output artifacts but cannot acquire a successful handoff edge. No output node is invented for a run that recorded none. An imported execution without saved job text has no invented `job_snapshot` node; its missing definition is explicit in `unresolved`.
- Run 1's original input and any recorded temporary normalized copy are separate artifacts connected by `derived_from`; the run consumes the file it actually received when recorded. An `i2i` successor consumes its predecessor's output; a video successor consumes its predecessor's verified handoff. A corrected video copy and its handoff are separate artifacts derived from their recorded source. For `t2v` run 1 there is no input artifact. A retained first image or color anchor is represented only when recorded, with its source relationship when that source is recorded.
- A cross-run or cross-execution `consumes`/`follows` edge requires both the recorded resume or run order and matching recorded handoff/output identity. Path equality by itself never joins unrelated runs. If the parent execution, source artifact, or necessary identity is absent or inconsistent, keep the known nodes and put a reasoned reference in `unresolved`; never fabricate an edge. The graph can therefore be partial while still valid.
- A graph for `E0013` includes that execution and the chain of `resumes` ancestors needed to trace its inputs; it does not speculate about future descendants. The document is a snapshot of history at one consistent SQLite read transaction. Canonical ordering makes two exports of unchanged history byte-identical. Deleting or pruning an ancestor can make a later export partial; an already saved JSON artifact remains a self-contained record of what was known at export time.
- Version 1 readers reject unknown major versions and malformed documents with a field-specific error. New optional properties may be added within version 1 only when old readers can ignore them safely; changing node identity, edge meaning, or required structure requires version 2. M10 need not emulate an earlier graph format, because none exists. Existing history and manifests are represented with gaps where their recorded facts are absent.

## Production and consumption

- Add a shared `services` graph projection over `state` rows, with pure graph construction/validation in `jobs` or `core` as the dependency rule allows. Graph reads never start a backend, mutate history, or require the run lock. A target execution that is absent or is a one-off generation returns a clear error. A terminal failed, interrupted, or parked job is exportable; a running target is refused until its recorded state is settled.
- Add authenticated `GET /v1/executions/{id}/graph` for the full lineage document, and `uv run dtc history graph E0012 --output provenance.json` to write exactly that JSON. Export refuses an existing destination by default and writes atomically. Cap a document at 10,000 nodes, 30,000 edges, and 8 MiB serialized; a larger lineage returns a limit error rather than a truncated graph. An `execution` scope on the endpoint and export gives one execution plus unresolved references to its parents as a bounded fallback. The CLI can also print the JSON to stdout when `--output` is omitted.
- Add `uv run dtc graph trace provenance.json --from artifact:E0012:run:2:output`. It reads the bounded JSON file, validates the schema, and prints the deterministic backward provenance path, including an explicit unresolved stop when ancestry is missing. It does not resolve paths on disk. Document direct API access and offline tracing in the user guide; the TUI need not draw a graph in this milestone.
- Keep the graph free of free-form prompts, commands, YAML, environment values, and media-check notes. The HTTP route uses the existing bearer authentication; the CLI export warns through ordinary documentation that recorded file paths may identify local directories. No graph file is written automatically or hidden under the output directory.

## Data integrity and lifecycle

- Use the execution's durable `resumes` link as the only cross-execution lineage authority. Imported manifests can carry an old `resumes_execution` ID while the importer assigns a different local `E...` ID. Resolve unambiguous links through a two-pass import keyed by the manifest's original ID and rewritten to the new local ID; preserve an explicit unresolved external reference for missing or ambiguous ancestors. Audit already imported rows during migration using their saved manifest when safely readable; clear an unverified local `resumes` link and retain its original reference as unresolved. Never treat an unverified old ID as the local row of the same number. This may revise import storage/behavior; no false link is acceptable for compatibility's sake.
- Build from execution rows and their per-run records in a consistent read transaction. A separate graph table or event replay is unnecessary. Existing manifests are optional historical input to `import-history`, not a second live source that can override the database. Missing old fields, pruned rows, deleted rows, and interrupted/partial runs must not crash projection or imply success.
- The graph endpoint stops finding a deleted/pruned target when its history row disappears. Saved exports are user-owned files and are not changed by history deletion or retention. Their validity is syntactic and evidentiary: they record what the database knew when exported, not that the current media still exists.

## Implementation order

1. Characterize current run records, source input copies, `i2i` output handoffs, video last frames, corrected copies, H3 stream facts, resumes across failed attempts, history import, deletion, and retention with focused fixtures. Identify which facts are authoritative and which older rows lack.
2. Define the version 1 schema, stable IDs, edge directions, completeness rules, deterministic serialization, and small examples. Implement a pure validator and backward-trace reader before wiring front ends.
3. Add the store ID migration and a bounded, consistent history read for the selected execution and its verified ancestors. Remap imported resume IDs where unambiguous and mark the rest unresolved. Build the graph from those rows, including partial runs and missing ancestors.
4. Expose the authenticated HTTP graph route and CLI export; then add the offline CLI trace against the exported format. Document the JSON contract, commands, retention/deletion effect, and limits in the user guide and architecture document when implementation lands.
5. Verify Draw Things and H3 multi-run chains, a chain spanning at least two resumes, imported/missing ancestors, incomplete output, corrected and H3 artifacts, schema/version rejection, deterministic export, API authentication, offline trace, and the module dependency rule. Run `make check`.

## Acceptance criteria

- For a completed or resumed multi-run job, API and CLI export produce the same version 1 graph. Its run and artifact IDs are unique across executions; each output traces through the recorded input/handoff to its producer, job snapshot, and backend without merging unrelated files that share a name.
- A failed/interrupted run has no successful handoff edge. Pruned, deleted, or ambiguous imported ancestors are explicit unresolved references; a trace stops there without claiming provenance that was not recorded.
- The offline trace reads the exported JSON without a server or database. A malformed file, unknown version, invalid edge, or oversized input is refused with a useful error.
- Graph export reads history without changing it; no graph sidecar appears unless requested. Existing Draw Things records and M03 H3 media facts are represented where recorded. Authentication and history retention/deletion semantics remain coherent.
- `make check` passes when the implementation lands.
