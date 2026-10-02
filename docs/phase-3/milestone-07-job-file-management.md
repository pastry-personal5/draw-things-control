# Milestone 07: Job File Management

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** done
**Depends on:** [Milestone 02: HTTP API: read and run](milestone-02-http-api.md), and the queue states of
[Milestone 05](milestone-05-park-and-hold.md) and the audit and error handling of
[Milestone 06](milestone-06-delete-executions.md) (both built before this one)

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
- `max_job_file_bytes`, which the API reports today and enforces from here on
- Backups, a trash folder, and refusal while a job is queued or running

Out of scope:

- Editing `config/global-config.yaml`, `data/params/`, or any file other
  than a job file
- Uploading input images; inputs must already exist in the input directory
- A structured (non-YAML) job schema
- Renaming a job file (delete it and create it under the new name)
- Restoring from `.trash/` or `.backups/` through the API; recovery is by
  hand
- `dtc` commands or TUI commands that write job files: people edit job files
  in their own editor, and the TUI already picks up a changed file within 5
  seconds (owner decision)
- An event on the gRPC stream when a job file changes: the TUI's job list
  already notices a change by polling, and agents read `GET /v1/jobs`

## Behavior

### Enabling writes

- Writes are off by default. `dtc serve --allow-write` turns them on.
  `ServeOptions.allow_write`, `ServerContext.allow_write`, and
  `GET /v1/capabilities`' `allow_write` exist already; `serve_command` never
  sets the option.
- Without the option, `PUT` and `DELETE /v1/jobs/{name}` are not registered
  and are left out of the OpenAPI schema, and the MCP write tools are not
  offered ([Milestone 10](milestone-10-mcp-server.md)). They do not exist,
  rather than being refused. Since `GET /v1/jobs/{job}` shares their path,
  Starlette answers them **405** with `Allow: GET`, not 404: that 405
  is how "does not exist" looks for a path that exists for another method
  (owner decision). No audit row is written for them. Routing refuses the
  method before `require_auth` runs, so a request without a token gets 405
  too; it learns nothing but that writes are off.
- Starlette's own 405 and 404 bodies (`{"detail": "Method Not Allowed"}`)
  are put into the API's error shape by a handler in `errors.py`, so a client
  such as the MCP server can report them as it reports any other error: a
  405 is `{"code": "writes_off", "message": ...}`, naming `--allow-write`,
  and a 404 for an unknown path is `not_found`.

### Endpoints

| Method and path | Purpose |
|-----------------|---------|
| `POST /v1/validate` | Validate job text as a write would, and return the resolved job or the error; writes nothing. Always registered, and behind the token like every route but `/v1/health` |
| `PUT /v1/jobs/{name}` | Create a job; returns 201 with the job, its text, its `sha256`, and its job ID. With `?overwrite=1` and the `expected_sha256` of the text it replaces, replace an existing one instead (200) |
| `DELETE /v1/jobs/{name}?expected_sha256=...` | Move a job to the trash, if its text is still the one the caller saw; returns 200 with where it went |

- The body of `POST` and `PUT` is JSON, `{"yaml": "..."}`, as every other
  endpoint's is (owner decision); the text is stored unchanged, comments and
  line endings included. A replace adds `"expected_sha256"`. Another content
  type, a body that is not a JSON object, or an unknown key is 422
  `invalid_input` naming it.
- The route reads the body itself (`await request.body()`), with no FastAPI
  body model, so every refusal of a body happens in the route and is audited
  there; `errors.py`'s `AUDITED_BODY_ACTIONS` needs no new rows.
- `max_job_file_bytes` ([Milestone 02](milestone-02-http-api.md#limits))
  limits the job text's UTF-8 size. It is checked on the decoded text, before
  YAML is parsed, and refused as `limit_exceeded` (422) with `key`,
  `limit`, and `value`, like every other limit. The raw body is refused the
  same way, before its JSON is parsed, once it is over 8 times the limit
  (JSON escaping can grow text up to 6 times), so a huge body is never held
  in memory in full.
- `POST /v1/validate` takes an optional `?name=`, to check the text as a
  write to that name would: the name rules below, and that the file's
  `name:` matches it. Without it, the text is parsed as
  `data/jobs/<draft>.yaml`, which appears only in messages; a job's `input`
  and `output.directory` resolve against the global configuration's
  directories, never against the job file's own place.
- `?overwrite=1` on a name with no file is 404 `not_found`, so a replace
  never creates a file by mistake; without it, an existing file is 409
  `conflict`.
- The response of `PUT` is `GET /v1/jobs/{job}`'s (`job_detail`) with the
  job ID. The ID is given by a full `JobCatalog.read(fresh=True)` after the
  rename, as for any new file in the listing. A replace keeps the file's ID;
  a delete leaves it to the next listing to retire, and it comes back if the
  same name does, as in Phase 2.
- The response of `DELETE` names the file's place under `.trash/`, relative
  to the project root.

### Changed since it was read

A replace or a delete must prove it saw the file's current text, so an
agent never overwrites or trashes a change a person made in their editor
after the agent read the file (owner decision):

- `GET /v1/jobs/{job}` gains `sha256`, the hex SHA-256 of the file's bytes,
  for a valid job and an invalid one alike; `PUT`'s response carries the new
  file's.
- `PUT ?overwrite=1` requires `expected_sha256` in its body, and `DELETE`
  requires `?expected_sha256=`: missing or not 64 hex digits is 422
  `invalid_input` naming it. A create that sends one is 422 too, since there
  is nothing to match.
- The file's bytes are read and hashed under `submission_lock`, after the
  in-use check and just before the backup and the rename (or the move to the
  trash). A mismatch is 409 `conflict` with `current_sha256`, and nothing is
  written; the caller reads the job again and redoes its edit. The current
  text is not returned (owner decision): the refusal stays small, and a
  body with YAML in it could not go in the audit row anyway.
- A person's editor does not take the server's lock, so a save landing
  between the hash and the rename is still replaced. It is then the backup
  that keeps it, as for every replace.
- An identical replace still needs the right hash, and then writes nothing.

### Name and path rules

- For writes, `{name}` is a job name: 1 to 64 lowercase letters, digits, and
  hyphens, starting and ending with a letter or digit, as the job file's
  `name:` must be (`NAME_PATTERN`, `jobs/prompt_pairs.py`). The file is
  always `data/jobs/<name>.yaml`, derived by the server; no slash, dot, or
  other path part is accepted (422 `invalid_input`, field `name`). So the API
  can write or delete only files it could have created, never one the owner
  named otherwise: a `DELETE` of `walk` when the only file is `walk.yml` or
  `Walk.yaml` is 404.
- The job's own `name:` must equal `{name}` (422 `invalid_input`, field
  `name`), so listings and outputs stay consistent.
- Another job file with the same stem, in another letter case or with
  `.yml` (`<name>.yml`, `Walk.yaml` for `walk`), makes create and replace
  fail with 409 `conflict`, naming it: the catalog finds a job by its stem,
  and macOS file systems usually ignore case.
- Nothing is written through, replaced, or moved through a symbolic link
  (409 `conflict`): not `data/jobs/<name>.yaml`, and not `.trash/`,
  `.backups/`, or `.backups/<name>/` either, since a linked directory would
  put a trashed or backed-up file outside `data/jobs/`. The server creates
  those directories itself, with `mkdir`, never following a link.

### Validation before writing

In the order `submit_job` checks a submission, so a job is refused before an
image it names is decoded:

1. The size limit, on the text.
2. The name rules for `{name}` (or `?name=`), above.
3. `load_job_text` with `decode_input=False`: everything `validate-job`
   checks but the input image itself; then the job's `name:` against
   `{name}`.
4. `check_job_rules` and `check_job_limits` (`services/api_rules.py`):
   `run_timeout_seconds` set, the input inside the input directory, the
   output directory inside the output directory, `max_job_runs`, and
   `max_job_seconds`. Not `check_api_rules`, whose `max_queued_jobs` is about
   the queue now, not the job: a full queue does not stop a write.
5. `load_job_text` again with `decode_input=True`, so the input image is
   read and its size rules checked.

- Nothing is written when validation fails; the error names the field.
- `POST /v1/validate` runs exactly these checks, and returns the same body a
  refusal would, or `job_detail` for a valid job, so an agent can fix a draft
  before writes are even on.

### Writing

- Writes, and the checks just before them, run under
  `ServerContext.submission_lock`, one at a time within the server. A
  submission holds the same lock from reading the job file to inserting its
  entry, so a replace or a delete can never land between the two, and the
  in-use check below can never miss a submission in progress.
- A write is atomic: the text goes to a temporary dotfile in `data/jobs/`
  (listings skip dotfiles), created with `O_EXCL`, flushed and `fsync`ed,
  then renamed into place. A create links it into place instead
  (`os.link`, then unlink the temporary file), so a file that appeared in
  the meantime is never replaced (409 `conflict`). A failed write leaves no
  temporary file.
- A new file gets the mode a file created in `data/jobs/` gets (the
  process's umask); a replaced file keeps the previous file's mode.
- A replace whose text equals the current file's byte for byte writes
  nothing and makes no backup.

### Backups and trash

- Before a replace, the previous file is copied to
  `data/jobs/.backups/<name>/<timestamp>.yaml` (`YYYYMMDD-HHMMSS`, local
  time, with `-2`, `-3`, ... when two land in one second). The server never
  prunes backups.
- Delete moves the file to `data/jobs/.trash/<name>-<timestamp>.yaml`, with
  the same `-2`, `-3`, ... suffixes, by a link into `.trash/` and an unlink
  within `data/jobs/`, never a rename: a rename onto a name already taken
  would silently replace an earlier trashed copy. Backups are created with
  `O_EXCL` for the same reason. So a create, a delete, a create, and a
  delete of one name within one second leave two files in `.trash/`. No job file is permanently deleted by the
  server. (Deleting an execution from the history,
  [Milestone 06](milestone-06-delete-executions.md), is final.)
- Replace and delete are refused with 409 `conflict` while an entry whose
  `job_path` is this file is `queued` or `running` (`running` includes one
  being parked). The entry runs its snapshot either way, but the owner
  should not see a file change under a job in progress. A `parked`,
  `failed`, or `interrupted` entry does not stop a write: its resume uses
  the snapshot, whatever the file became. The queue's `job_path` is stored
  resolved, so it is compared with the resolved target.
- Both directories are under `data/jobs/`, and are created on demand;
  `ProjectPaths` names them (`jobs_trash`, `jobs_backups`). The job loader,
  the TUI, the API, and the MCP server all skip them, since jobs are only the
  files directly in `data/jobs/` (`job_files` skips dotfiles and
  directories).
- `.gitignore` already ignores `data/**`, but its `!data/**/*example*` line
  would bring back a trashed `example-job-<timestamp>.yaml`, so it gains
  `data/jobs/.trash/` and `data/jobs/.backups/` after that line.

### Audit

Every create, replace, and delete, successful or refused, is recorded in the
`audit_log` table ([Milestone 02](milestone-02-http-api.md#audit-log)) as
`create_job`, `replace_job`, or `delete_job`, with `{name}` as the target
(none when `{name}` itself is refused, since it may be anything), after the
`delete_execution` style of Milestone 06: the verbs `submit` and `delete`
alone would not say what was acted on. `PUT` is recorded as `create_job`
without `overwrite` and `replace_job` with it. `POST /v1/validate` writes
nothing and is not recorded. The caller comes from `X-Dtc-Caller`, as for the
queue routes.

## Planned changes

### `core/`

- `errors.py`: `ConflictError(DtcError)`, code `conflict`, for a file that
  exists, a stem taken in another case or suffix, a symbolic link, and a job
  in use. `invalid_state` is about a queue entry's state, not a file's.
- `exit_codes.py`: `conflict` maps to `EXIT_INVALID_INPUT` in
  `EXIT_CODES_BY_ERROR_CODE`, beside `invalid_state`, so a future `dtc`
  client reports it as the others.
- `paths.py`: `ProjectPaths.jobs_trash` and `jobs_backups`.

### `services/`

- `services/job_files_write.py` (new): `validate_job_text(text, *, name,
  global_config, paths) -> JobDefinition`, the checks above in order;
  `create_job`, `replace_job`, and `delete_job` (the last two with
  `expected_sha256`), each taking the store for the in-use check and
  returning what they did (the path written, the
  backup or trash path, or that nothing changed). Nothing here knows HTTP or
  takes a lock; the route passes the lock, as `delete_executions` takes
  `lock=`.
- The in-use check is one query on the queue for `queued` or `running`
  entries with this `job_path` (`QueueRepository.active_for_path`, or a
  filter on its existing `list`), so it does not parse every snapshot.

### `server/`

- `routes_job_files.py` (new): `POST /v1/validate` in a router registered
  always; `PUT` and `DELETE /v1/jobs/{name}` in a second router that
  `create_app` includes only when `context.allow_write` is on. Both read the
  body themselves and wrap each write in `audited(...)`.
- `serializers.py`: `job_detail`, and the invalid job's answer in
  `routes_jobs.py`, gain `sha256`.
- `errors.py`: `conflict` maps to 409 in `STATUS_BY_ERROR_CODE`, and a
  handler for Starlette's `HTTPException` gives its 404 and 405 the API's
  error shape (`writes_off` for 405).
- `routes_health.py`: `GET /v1/capabilities` is unchanged; it already
  reports `allow_write` and `max_job_file_bytes`.

### `cli/`

- `app.py`: `dtc serve --allow-write`, passed through `ServeOptions`, and
  logged at start (`Writes to data/jobs/ are on`) so a person sees it.

### Repository

- `.gitignore`: `data/jobs/.trash/` and `data/jobs/.backups/` after
  `!data/**/*example*`.

### Tests

In `tests/services/` for the rules and the files, over a temporary project
(`ProjectPaths`), and in `tests/server/` through `create_app` and FastAPI's
`TestClient`, as `test_read_routes.py` does, never starting
`draw-things-cli`:

- Each acceptance criterion below.
- A replace and a delete with a stale, a missing, and a malformed
  `expected_sha256`, each writing nothing.
- A write racing a submission: a replace blocked while a submission of the
  same file holds `submission_lock`, and refused once it is queued.
- A failed rename (a read-only `data/jobs/`) leaves no temporary file.

### Documentation, when it lands

- The user guide's server section: `--allow-write`, the three endpoints, the
  name rules, the limit on text size (the limits table stops saying "from
  Milestone 07"), where backups and trashed jobs are, and how to restore one
  by hand.
- `docs/architecture.md`: the write path (checks, lock, rename), the
  `conflict` code, and the two directories.
- [Milestone 10](milestone-10-mcp-server.md)'s tool table uses the `/v1/`
  paths.
- The phase document marks the milestone done, and the changelog records
  what was built against this plan.

## Acceptance criteria

- Without `--allow-write`, `PUT` and `DELETE /v1/jobs/{name}` answer 405
  `writes_off` in the API's error shape (the path exists for `GET`), are
  absent from the OpenAPI schema, and write no audit row; `/v1/capabilities` says writes are off; `POST /v1/validate`
  works either way and writes nothing.
- With it, a valid job is created as `data/jobs/<name>.yaml` with its text,
  comments, and line endings unchanged, and gets a job ID; the same request
  again returns 409 without `overwrite`, and `overwrite=1` on a missing name
  returns 404.
- Overwrite leaves the old file in `.backups/<name>/` and keeps the job ID,
  and an identical overwrite leaves none; delete leaves the file in
  `.trash/`, and two deletes of one name in one second leave two files there;
  nothing else is removed.
- Names like `../x`, `a/b`, `A`, `a.b`, `-a`, one of 65 characters, and a
  name that does not match the `name:` field are rejected and no file is
  touched. A symlinked `data/jobs/<name>.yaml`, `.trash/`, or `.backups/` is
  never written through or moved into, and a file with the same stem in
  another case or suffix makes a create 409.
- An input outside the input directory or missing, an output directory
  outside the output directory, a missing `run_timeout_seconds`, a job over
  `max_job_runs` or `max_job_seconds`, and text over `max_job_file_bytes`
  are each refused naming the field or the limit, by `PUT` and by
  `POST /v1/validate`. A full queue refuses neither.
- A replace or delete whose `expected_sha256` is not the file's current
  hash returns 409 `conflict` with `current_sha256` and changes nothing; a
  missing one is 422.
- Overwrite and delete of a `queued` or `running` job return 409; of a
  `parked` one, they succeed.
- Every create, replace, and delete, refused ones included, has one audit
  row with its action, target, outcome, and caller.
- A write that fails validation leaves `data/jobs/` unchanged, and a failed
  write leaves no temporary file.
- `git status` shows nothing from `.trash/` or `.backups/`, even for a
  trashed example job.
- `make check` passes; the user guide documents writing jobs.
