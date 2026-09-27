# Milestone 07: Job File Management

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 02: HTTP API: read and run](milestone-02-http-api.md)

## Goal

Let an agent draft, create, edit, and delete job files in `data/jobs/`,
safely: drafts are checked without writing anything, and writes happen only
when the owner turned them on, only inside `data/jobs/`, only for jobs the
API could run, and always recoverably.

## Scope

In scope:

- Validating a draft's text, always available since it writes nothing
- The `--allow-write` server option
- Create, replace, and delete endpoints for job files
- Validation before anything is written, with the API's rules and limits
- Backups, a trash folder, and refusal while a job is queued or running

Out of scope:

- Editing `config/global-config.yaml`, `data/params/`, or any file other
  than a job file
- Uploading input images; inputs must already exist in the input directory
- A structured (non-YAML) job schema
- Renaming a job file (delete it and create it under the new name)
- Restoring from `.trash/` or `.backups/` through the API; recovery is by
  hand

## Planned changes

### Enabling writes

- Writes are off by default. `dtc serve --allow-write` turns them on.
- Without the option, `PUT` and `DELETE` below are not registered (404),
  `GET /capabilities` says writes are off, and the MCP write tools are not
  offered ([Milestone 08](milestone-08-mcp-server.md)). They are not merely
  rejected: they do not exist.

### Endpoints

| Method and path | Purpose |
|-----------------|---------|
| `POST /validate` | Validate job text as a write would, and return the resolved job or the error; writes nothing. Always available |
| `PUT /jobs/{name}` | Create a job, or replace it when `?overwrite=1`; returns the job with its job ID |
| `DELETE /jobs/{name}` | Move a job to the trash |

The body of `POST` and `PUT` is the job file as YAML text (`text/plain`, or
a JSON `{"yaml": "..."}`), stored unchanged, comments included. A body over
`max_job_file_bytes` ([Milestone 02](milestone-02-http-api.md#limits)) is
refused before it is parsed. `POST /validate` takes an optional `name`, to
check it as a write to that name would.

### Name and path rules

- For writes, `{name}` is a job name: 1 to 64 lowercase letters, digits, and
  hyphens, starting and ending with a letter or digit, as the job file's
  `name:` must be (`NAME_PATTERN`). The file is always
  `data/jobs/<name>.yaml`, derived by the server; no slash, dot, or other
  path part is accepted. So the API can write or delete only files it could
  have created, never one the owner named otherwise.
- The job's own `name:` must equal `{name}`, so listings and outputs stay
  consistent.
- Another job file with the same stem, in another letter case or with
  `.yml` (`<name>.yml`, `Walk.yaml` for `walk`), makes create and replace
  fail with 409, naming it: the catalog finds a job by its stem, and macOS
  file systems usually ignore case.
- A `data/jobs/<name>.yaml` that is a symbolic link is never written
  through, replaced, or moved (409).

### Validation before writing

- The text is parsed in memory with `load_job_text`, as `validate-job`
  would, including the input-file, mode, cooldown, and size rules, and then
  checked against the
  [rules for jobs the API runs](milestone-02-http-api.md#rules-for-jobs-the-api-runs):
  `run_timeout_seconds` set, the input inside the input directory, the
  output directory inside the output directory, and the limits.
- Nothing is written when validation fails; the error names the field.
- `POST /validate` runs exactly these checks, so an agent can fix a draft
  before writes are even on.

### Writing

- Writes are made one at a time within the server.
- A write is atomic: the text goes to a temporary dotfile in `data/jobs/`
  (listings skip dotfiles), then is renamed into place. A create never
  replaces a file that appeared in the meantime, and a failed write leaves
  no temporary file.
- A replace whose text equals the current file's writes nothing and makes
  no backup.
- A replaced file keeps its job ID. A deleted file's ID is retired and comes
  back if the same name does, as in Phase 2.

### Backups and trash

- Before a replace, the previous file is copied to
  `data/jobs/.backups/<name>/<timestamp>.yaml` (with a suffix when two land
  in one second). The server never prunes backups.
- Delete moves the file to `data/jobs/.trash/<name>-<timestamp>.yaml`.
  Nothing is permanently deleted by the server.
- Replace and delete are refused with 409 while an entry for that job file
  is `queued` or `running`. The entry runs its snapshot either way, but the
  owner should not see a file change under a job in progress. A later resume
  uses the snapshot, whatever the file became.
- Both directories are under `data/jobs/`, where Phase 2 moved the job
  files, and are created on demand; `ProjectPaths` names them. The job
  loader, the TUI, the API, and the MCP server all skip them, since jobs are
  only the files directly in `data/jobs/`.
- `.gitignore` already ignores `data/**`, but its `!data/**/*example*` line
  would bring back a trashed `example-job-<timestamp>.yaml`, so it gains
  `data/jobs/.trash/` and `data/jobs/.backups/` after that line.

### Audit

Every create, replace, and delete, successful or refused, is recorded in
the `audit_log` table ([Milestone 02](milestone-02-http-api.md#audit-log)).
`POST /validate` writes nothing and is not recorded.

### Documentation

The user guide's server section covers the write flag, the endpoints, and
where backups and trashed jobs are and how to restore one by hand.

## Acceptance criteria

- Without `--allow-write`, `PUT` and `DELETE` return 404 and
  `/capabilities` says writes are off; `POST /validate` works either way and
  writes nothing.
- With it, a valid job is created as `data/jobs/<name>.yaml` with its text
  and comments unchanged and gets a job ID; the same request again returns
  409 without `overwrite`.
- Overwrite leaves the old file in `.backups/<name>/`, and an identical
  overwrite leaves none; delete leaves the file in `.trash/`; nothing else
  is removed.
- Names like `../x`, `a/b`, `A`, `a.b`, `-a`, one of 65 characters, and a
  name that does not match the `name:` field are rejected and no file is
  touched. A symlinked `data/jobs/<name>.yaml` is never written through or
  moved, and a file with the same stem in another case or suffix makes a
  create 409.
- An input outside the input directory or missing, an output directory
  outside the output directory, a missing `run_timeout_seconds`, and a job
  over a limit are each refused naming the field, by `PUT` and by
  `POST /validate`.
- Overwrite and delete of a queued or running job return 409.
- A write that fails validation leaves `data/jobs/` unchanged, and a failed
  write leaves no temporary file.
- `git status` shows nothing from `.trash/` or `.backups/`, even for a
  trashed example job.
- `make check` passes; the user guide documents writing jobs.
