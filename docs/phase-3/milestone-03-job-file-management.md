# Milestone 03: Job File Management

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 02: HTTP API: read and run](milestone-02-http-api.md)

## Goal

Let an agent create, edit, and delete job files in `data/jobs/`, safely: only when
the owner turned writing on, only inside `data/jobs/`, and always recoverably.

## Scope

In scope:

- The `--allow-write` server option
- Create, replace, and delete endpoints for job files
- Validation before anything is written
- Backups, a trash folder, and refusal while a job is in use

Out of scope:

- Editing `config/global-config.yaml`, `data/params/*.json`, or any file other
  than a job file
- Uploading input images; inputs must already exist in the input directory
- A structured (non-YAML) job schema
- Restoring from `.trash/` or `.backups/` through the API; recovery is by
  hand

## Planned changes

### Enabling writes

- Writes are off by default. `dtc serve --allow-write` turns them on.
- Without the option, the endpoints below return 404 and are absent from
  `GET /capabilities`, and the MCP write tools are not registered
  (Milestone 04). They are not merely rejected: they do not exist.

### Endpoints

| Method and path | Purpose |
|-----------------|---------|
| `PUT /jobs/{name}` | Create a job, or replace it when `?overwrite=1` |
| `DELETE /jobs/{name}` | Move a job to the trash |

The body of `PUT` is the job file as YAML text (`text/plain` or a JSON
`{"yaml": "..."}` field), stored unchanged, comments included.

### Name and path rules

- `name` must match `[a-z0-9-]`, as job names already must. The file is
  always `data/jobs/<name>.yaml`, derived by the server. No slashes, dots, or
  other path parts are accepted, and the resolved path is checked to be
  directly inside the data directory. Symlinks in `data/jobs/` are never
  followed for writing or deleting.
- The job's own `name:` field must equal the file name, so listings and
  outputs stay consistent.

### Validation before writing

- The YAML text is parsed and validated in memory with the same code as
  `validate-job`, including the input-file, mode, cooldown, and size rules,
  and the limits in
  [Milestone 02](milestone-02-http-api.md#limits), using the text loader
  added in [Milestone 01](milestone-01-queue-run-manager.md).
- Referenced input files must exist inside the configured input directory.
  A job that points elsewhere is rejected naming the field.
- Nothing is written when validation fails; the error names the field.
- The write is atomic: the text is written to a temporary file in `data/jobs/`
  and renamed into place.

### Create, overwrite, and backups

- Create fails with 409 if `data/jobs/<name>.yaml` exists and `overwrite` is not
  set.
- With `overwrite`, the previous file is first copied to
  `data/jobs/.backups/<name>/<timestamp>.yaml`, then replaced. Backups are never
  pruned by the server.

### Delete and trash

- Delete moves the file to `data/jobs/.trash/<name>-<timestamp>.yaml`. Nothing is
  permanently deleted by the server.
- Delete and overwrite are refused with 409 when the job is `queued`
  or `running` in the queue, since a snapshot exists but the
  owner should not be surprised by a file that vanishes under a job.

### Directories

`.trash/` and `.backups/` are created on demand under `data/jobs/`. The job
loader, the TUI, the API, and the MCP server all ignore dot-directories
(the TUI already does, from Phase 2, Milestone 03). Git ignores them: they
are added to `.gitignore`.

### Audit

Every create, overwrite, and delete, successful or refused, is recorded in
the `audit_log` table ([Milestone 02](milestone-02-http-api.md#audit-log)).

## Acceptance criteria

- Without `--allow-write`, `PUT` and `DELETE` return 404 and
  `/capabilities` says writes are off.
- With it, a valid job is created as `data/jobs/<name>.yaml` with its text and
  comments unchanged; the same request again returns 409 without
  `overwrite`.
- Overwrite leaves the old file in `.backups/<name>/`; delete leaves the file
  in `.trash/`; nothing else is removed.
- Names like `../x`, `a/b`, `A`, `a.b`, and a name that does not match the
  `name:` field are rejected and no file is touched. A symlinked
  `data/jobs/<name>.yaml` is never written through.
- An input outside the input directory, or missing, is rejected naming the
  field.
- Overwrite and delete of a queued or running job return 409.
- A write that fails validation leaves the data directory unchanged, and a
  failed write leaves no temporary file.
- `make check` passes.
