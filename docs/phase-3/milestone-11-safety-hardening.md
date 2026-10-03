# Milestone 11: Safety Hardening

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** every agent-facing milestone, all built before this one:
[02 HTTP API](milestone-02-http-api.md),
[03 Queue for people](milestone-03-queue-for-people.md),
[05 Park and hold](milestone-05-park-and-hold.md),
[06 Delete executions](milestone-06-delete-executions.md),
[07 Job file management](milestone-07-job-file-management.md),
[09 Color preservation](milestone-09-color-preservation.md), and
[10 MCP server](milestone-10-mcp-server.md)

## Goal

Make the agent-facing surface safe by construction, and show it: bounded
work, confined paths, a stable error for every refusal, no secret in any
output, and a record of everything a caller did. Each earlier milestone
tested its own rules. This one reviewed them together, from the caller's
side, against the code as built (2026-10-03), and found that several of its
own acceptance criteria fail today. So the milestone is first a set of fixes,
and then the suite that keeps them fixed. The first plan of this milestone
(written before Milestones 07 to 10 were built) assumed the rules held and
only needed tests.

## What the review found

Every input was traced from where it enters (path, query, body, header, MCP
argument, each key of a job's text, a front end's own option) to where it
ends (a file read or written, a directory made, a process argument, a SQL
statement, a log line, a response). Each finding is marked *probed*
(reproduced through the real app and services on a throwaway project, with
no `draw-things-cli`) or *read* (found in the code; the first test of its fix
confirms it before the fix goes in). The fix of each is under
[Planned changes](#planned-changes).

**Defects, probed**

- **F1. An ordinary invalid job answers HTTP 500.** A YAML syntax error, a
  tab indent, an empty file, a text that is not a mapping, a duplicate key,
  an octal number (`0777`), an input image of the wrong size, and an input
  that is not a readable image each end as a bare `Internal Server Error`
  (plain text, no `code`) from `POST /v1/validate`, `PUT /v1/jobs/{name}`,
  and `POST /v1/queue`. An unknown key, a missing input, a missing
  `run_timeout_seconds`, and a bad `config_file` are answered correctly
  (422), so the gap is exactly the errors the loaders raise as a plain
  `ValueError`. NUL in `input` or `output.directory`, and YAML nested deeper
  than the parser allows, end the same way. Cause: the loaders' contract is
  `ValueError` (`read_job` says so), the CLI's `errors_exit()` catches it, and
  no route does. Effect: `dtc queue add`, the TUI's `/apply`, and MCP's
  `validate_job_text` and `submit_job` show "Internal Server Error" (MCP:
  `{"code":"error","message":"dtc serve answered HTTP 500"}`) for the commonest
  mistake there is, a typo in YAML. No existing test sends a malformed job
  through the API.
- **F2. A 500 is not audited, and not every refusal has the error shape.**
  `audited()` records a `DtcError` only; a job file that breaks the parser,
  submitted, left no audit row. A JSON body nested 200,000 deep answers 500
  from `/v1/validate` (`_yaml_body` catches `UnicodeDecodeError` and
  `JSONDecodeError` only) and `400 {"detail": ...}` from `/v1/queue`, which is
  outside the error shape and skips `_audit_unparsed_body`. A 401 answers
  `{"detail":"Unauthorized"}`. Together they break "one audit row for every
  submission and write, refused ones included".
- **F3. The containment check runs after the file is touched.** A job's
  `input` is resolved and must be a file (`is_file()`), and its header is read
  (`read_image_info`), before `check_job_rules` compares it with the input
  directory, so a caller who names a path outside it gets three different
  answers: `outside_directory` (an image of the job's size), `invalid_input`
  "file does not exist" (a missing path, and `~/x.png`, which expands to the
  owner's home), and a 500 (an image of another size, whose message would
  give its width and height, or a file that is not an image, `/etc/hosts`
  among them). That is an oracle for whether any path the server can read
  exists, and for the pixel size of any image there. No content leaves, and
  nothing outside is written, but "touches nothing" and "no known path leads
  from agent input to a file outside the input directory" are false as
  written. `output.directory` is stat-ed (`exists()`) before its rule as well.
- **F4. The audit log's `target` is caller-chosen text with no limit.**
  `POST /v1/queue` records the `job` string as sent, and every queue action
  records the `{queue_id}` path segment as sent; a 5 MB string was stored in
  full, in a table nothing prunes and `GET /v1/audit` pages over. A prompt, a
  YAML text, or a credential an agent puts there stays in the log for good.
  `PUT /v1/jobs/{name}` already records a target only when it matches
  `NAME_PATTERN`.
- **F5. No request body limit but one.** `_yaml_body` refuses a body of over
  8 times `max_job_file_bytes`; `POST /v1/queue` and `POST /v1/executions/delete`
  parse whatever arrives (the 5 MB `job` of F4 was accepted), and the 200 IDs
  `executions` may name are counted after the whole list is parsed.
- **F6. NUL in a prompt, a model name, or any other text is accepted.** It
  is stored and queued, and can only fail when the run starts (*read*: a
  NUL in a process argument is refused then; the first test of the fix
  confirms what the entry and its execution show).

**Defects, read**

- **F7. The same `ValueError` reaches reads.** `preview_resume` and
  `resume_entry` parse the entry's stored job again and catch `InputError`
  only, so an input image a person replaced after submission (another size,
  or not an image) turns `GET /v1/queue/{id}`, which MCP's
  `get_queue_entry` reads, and `POST /v1/queue/{id}/resume` into 500s for that
  entry. A fix at each route would miss this; a fix where the job text is
  parsed does not.
- **F8. Blocking work runs on the event loop that serves HTTP, SSE, and
  gRPC.** `dtc serve` runs uvicorn and `grpc.aio` on one loop. gRPC's
  `WatchQueueEntry` reads each snapshot on it (the SSE watch moved it to a
  thread in Milestone 10 because it takes the worker's lock, "which a deletion
  can hold a while"), the `async def` route `PUT /v1/jobs/{name}` waits on
  `submission_lock` (a `threading.Lock`) and does file I/O on it, and `POST
  /v1/validate` decodes an image on it. A long deletion of up to 200
  executions, or a slow submission, can stall every request and every watch,
  the `/v1/health` liveness check included.
- **F9. Loguru prints variable values in tracebacks.** `diagnose=True` is its
  default ([Loguru's documentation](https://loguru.readthedocs.io/en/stable/overview.html)
  warns it "may leak sensitive data in prod"), and no sink of this project
  turns it off. Each failing line's expressions are printed with their
  values, so a line that handles the token, or a job's text, prints it, in
  the terminal or a log file. The fix for F1 and F2 adds a catch-all that
  logs tracebacks, which makes it matter.
- **F10. A 405 always says `writes_off`.** `DELETE /v1/queue`, with writes on,
  answers "Writes to data/jobs/ require dtc serve --allow-write"; and a 405
  is answered before the token is checked, so an unauthenticated caller
  learns whether writes are on (*probed*).
- **F11. A front end builds URLs from unchecked IDs.** `dtc queue` and the
  TUI's client put the typed queue ID into the path as it is
  (`/v1/queue/{queue_id}/cancel`); `Q0001/watch` reaches the watch. MCP's
  `path_segment` refuses it. Only the person typing is affected.
- **F12. Documentation drift** (*probed*, by grep): the root `README.md`'s
  quick start runs `dtc run-job`, which Milestone 03 removed;
  `docs/user-guide.md` documents `run-job` in its command table and
  examples and says, in the server section, that "`run-job` and the TUI cannot
  start a job while a server is up"; `docs/architecture.md` still has a
  "Milestones 3 onward (planned)" section. And `dtc generate` starts
  `draw-things-cli` itself (under the run lock) while the phase document,
  `AGENTS.md`, and `architecture.md` say the server's worker is the only thing
  that ever does ([owner decision 2](#owner-decisions): it moves onto the
  queue in a Milestone 12, so until then the documents name it as the
  exception).

**Accepted as they are, each with a reason** (recorded in the changelog)

- **A loopback `Host` header is accepted on any port** (*probed*). Only a
  non-browser client can send a port that is not its URL's, and an SSH tunnel
  on another local port needs it. The docstring is corrected to say so.
- **`GET /v1/health` needs no token**: it says the version, whether the worker
  lives, and the gRPC port, which every client derives its gRPC target from.
- **Growth a token holder can cause**: `.backups/` and `.trash/` keep every
  replaced or deleted job file, `data/jobs/` has no file count limit, the
  audit log is never pruned (owner decision, Milestone 02), and watches are
  not counted. Each write is bounded (`max_job_file_bytes`, and F5's body
  limit once built), and a caller needs the token; the milestone adds no
  quotas unless the owner asks for them.
- **The `mcp` caller names itself.** An agent that can read
  `config/server-token` can call the API as `cli` or `tui`, past Milestone 10's
  guard on people's entries and past MCP's own gate on `delete_executions`.
  That was decided in Milestone 10 (one level of access; deleting a person's
  executions is not covered), and `run-server.sh` now starts the server with
  `--allow-write`. This milestone does not change it. It says it plainly in the
  user guide, in one place, with what an agent holding the token can do
  ([Documentation](#d-documentation-f12)).

## Scope

In scope:

- The fixes for F1 to F11, and the documentation corrections of F12
- A security suite that pins them and every other rule at the boundary
  where the rule lives, and the checks each front end owns
- A test that every action is audited, accepted and refused, with the shape
  its row may have
- A check that the documents describe what was built, and cannot drift
  back to a removed command

Out of scope:

- Network hardening (TLS, rate limiting per client): the server is loopback
  only unless the owner passes `--allow-remote-bind`
- More than one level of access, or a second token for agents
  ([Milestone 10](milestone-10-mcp-server.md) non-goal)
- Quotas on stored files, the audit log, or open watches (see above)
- Sandboxing `draw-things-cli` itself
- Retiring gRPC for SSE: both stay for good
  ([owner decision 3](#owner-decisions)), so both are hardened
- Moving `dtc generate` onto the queue: [Milestone 12](milestone-12-generate-through-the-queue.md)
  ([owner decision 2](#owner-decisions))

## Planned changes

Built in four increments, each ending with `make check` and a code review, as
Milestone 10 was. A comes first because the audit and no-500 tests of the
later ones depend on typed errors and `internal_error`; B and C change
behavior the suite must pin as it ends; D documents what shipped.

### A. Typed errors, and a record of every refusal (F1, F2, F6, F7, F9, F10)

- **Convert where the job text is parsed.** `jobs/parsing.py`'s three entry
  points (`load_job`, `load_job_text`, `JobParser.parse`) turn any `ValueError`
  that is not already a `DtcError`, and a `RecursionError`, into an
  `InputError` with the same text, its `path`, and its `field` when it has
  one. Every consumer gets it at once: the validate, create, replace, and
  submit routes, `preview_resume`, `resume_entry`, the worker, the CLI, and the
  TUI's listing. The loaders' own contract does not change (an `InputError`
  is a `ValueError`, so code that catches one still does).
- **Refuse NUL anywhere in a job's text.** After the YAML is read, every
  string in the mapping is checked, naming its key path (`prompt_pairs[0].positive`),
  walking with a set of the nodes seen so an alias cannot make it repeat
  itself. The text is already bounded by `max_job_file_bytes`.
- **Answer any other exception in the error shape.** A catch-all in
  `server/errors.py` answers `500 {"code": "internal_error", "message": "..."}`
  with no detail beyond "see the server's log", and logs the traceback
  without the request's body. `audited()` records `internal_error` for any
  `Exception` before it re-raises, so a crash is a row (not for a
  `BaseException`: a client's disconnect cancels an `async def` route, and is
  not a crash). `internal_error` gets
  its status in `STATUS_BY_ERROR_CODE` and its own exit code in
  `core/exit_codes.py` (not `invalid_input`'s).
- **One error shape for every status.** `_yaml_body` catches `RecursionError`
  and any other decode failure as `invalid_input`. A body FastAPI itself
  cannot parse answers `{code, message}` and, on the two audited POSTs, takes
  the `_audit_unparsed_body` path. A 401 answers `{"code": "unauthorized",
  "message": ...}` (the clients already key on the status). A 405 says
  `writes_off` only for `PUT` or `DELETE` on `/v1/jobs/{name}`, and
  `method_not_allowed` otherwise. The unauthenticated 405 stays (see
  [Decisions](#decisions-taken-in-this-plan), which says why).
- **No traceback leaks a value.** Every Loguru sink the project adds
  (`cli/app.py`'s two, `dtc mcp`'s, the job log's in `jobs/records.py`) sets
  `diagnose=False` and `backtrace=False`. A test raises from a frame whose
  line holds the token and a YAML text and reads both sinks and the job log:
  neither appears.
- **The no-500 guard.** A table of malformed inputs against every route that
  takes a body, a query, or a path segment, run with a test client that
  reports a 500 instead of re-raising it (`raise_server_exceptions=False`, as
  the existing tests do not), asserts that the status is not 500 and the body
  has `code` and `message`. The table holds every case F1 lists, NUL, nesting
  in the text and in the body, an alias bomb (it must finish quickly), a
  YAML key of 70,000 characters, and an integer of 5,000 digits. NUL in a
  path segment goes through a raw ASGI scope, since the client refuses it.

### B. Confinement before access, and what the audit log may hold (F3, F4, F5)

- **Check containment before any file is opened or read.** `input` and
  `output.directory` are compared with the input and output directories in two
  steps. First lexically, with no filesystem call: the caller's text, `~`
  expanded, joined to the directory and normalized (`..` folded); one that
  falls outside is refused at once. Then, for one that passes, resolved (which
  follows links) and compared again, which refuses a symbolic link inside the
  directory that points out. Only after both does anything call `is_file()`
  or `exists()`, or read a header. A path outside answers one thing,
  `outside_directory` naming the field, whether it exists or not, and without
  the resolved target (the message gives the caller's own text, so a link's
  target and `~`'s expansion are not echoed). The check lives in `JobParser`
  and serves every consumer ([owner decision 1](#owner-decisions)):
  `check_job_rules` keeps only the `run_timeout_seconds` rule, `dtc
  validate-job` and the TUI's job list show a job whose `input` or
  `output.directory` is outside as invalid, and the tests that name an
  outside input change. The build's first step lists which job files in
  `data/jobs/` would change: the owner's own name their inputs by relative
  paths without `..`, so the lexical step passes them, and the listing says
  whether a link among them points out. The tests: `input` as `/etc/hosts`,
  `~/x.png`, `../x.png`, a missing absolute path, an image of another size,
  and a link out all answer the same `outside_directory`, and a test that
  makes `open`, `Path.stat`, `is_file`, `exists`, and the image reader fail
  proves that a lexically outside path calls none of them.
- **A target is recorded only once it is known.** The audit row's `target` is
  the job file's name once the reference resolves to a file (for a file
  being created, its name once it matches `NAME_PATTERN`), a queue or
  execution ID in canonical form once it parses (bounded in length), and
  null otherwise, with the refusal's outcome (`not_found`, `invalid_input`)
  saying what happened. `audited()` takes its target after resolution, not
  before. `AuditRepository.record` also cuts a target at 256 characters, as
  a backstop. Rows already written are not rewritten (the log is
  append-only).
- **One body limit, before any parsing.** A pure ASGI middleware counts the
  bytes of every request body as they arrive, and refuses one of over 8
  times `max_job_file_bytes` (the figure `_yaml_body` uses today, which moves
  into it) with HTTP 413 and the `limit_exceeded` body (`limit`, `value`).
  `Content-Length` is checked first, and a chunked body is counted as it
  streams.

### C. The rest of the review, and the security suite (F8, F11, and the rest)

- **Take blocking work off the event loop.** The two `async def` job-file
  routes read the body, then do their work in the threadpool (the lock, the
  file I/O, the image decode); gRPC's `WatchQueueEntry` reads its snapshot
  with `run_in_executor`, as the SSE watch does. First, the test that
  confirms F8: a thread holds `submission_lock` and the worker's lock, a
  `PUT`, a `validate`, and a gRPC watch are in flight, and `GET /v1/health`
  must answer within a second.
- **Front ends check what they own.** `dtc queue`, `dtc history`, and the
  TUI's client check an ID's shape (`parse_typed_id`) before they build a
  path (F11). Each front end's own tests, and only these, cover: the caller
  header it sends (`cli`, `tui`, `mcp`), refusing a non-loopback URL without
  its flag, reading the token and printing it nowhere, and one API refusal
  of each kind (a limit, a path outside, a malformed job) arriving as its
  exit code, its message, or its tool error with `field` intact. MCP also
  keeps its own: an unknown argument is refused, no tool takes a path or a
  flag, `delete_executions` is unlisted and refused while writes are off, and
  the list follows `allow_write`.
- **The security suite**, in `tests/server/` (plus `tests/services/` and
  `tests/jobs/` where a rule lives there), with a fake runner, as every test
  is. A first pass lists which of these exist already (the queue routes alone
  have 686 lines of tests) and each rule gets one test where it is enforced,
  named for the rule; nothing is added twice. The rules:
  - **Names and references:** `..`, `../x`, `a/b`, `a\b`, absolute paths,
    percent-encoded slashes and dots, very long names, upper case, a dot, a job
    ID never given, and, for a path segment, `Q0001/watch`.
  - **Symbolic links** in `data/jobs/` (as a job file, as a write's target, as
    `.trash/` or `.backups/` themselves) and in the input directory.
  - **Job text:** the cases of F1 and F6, `input` and `output.directory` and
    `config_file` as the draft listed them, text over the size limit, and
    nesting.
  - **Writes and deletes** with `--allow-write` off and on, a stale or missing
    `expected_sha256`, a queued or running job's file, and a name that
    differs only in letter case.
  - **Auth and binding:** every route but `/v1/health` refuses a request with
    no token, found structurally by walking the app's routes, not from a
    list; a wrong token, the token in another letter case, a `bearer` scheme,
    the token in the query string or another header, an unknown
    `X-Dtc-Caller` (`invalid_input`, audited as `api`), a `Host` that is
    neither loopback nor the bound address, a token file others can read, a
    non-loopback `--host` without `--allow-remote-bind` (gRPC binds `--host` too,
    and has no host of its own), a non-loopback `--server-url` without
    `--allow-remote-server`, an SSE watch with no token or a wrong one (401,
    before any snapshot), and a gRPC call with no `authorization` metadata or
    a wrong one (`UNAUTHENTICATED`).
  - **Credentials:** no job key reaches a flag in `SECRET_FLAGS`, checked
    structurally over `OVERRIDE_TARGETS` and the keywords `JobPlanner` gives
    `DrawThingsGenerateArguments`; a run inserted with an unredacted
    `--api-key` and `--remote-shared-secret` is served redacted by
    `/executions`, the events, and the MCP tools; and the token, set to a
    value that is easy to find, appears in no response, event, gRPC message,
    log line, audit row, manifest, or the bytes of the state database after a
    session that submits, cancels, refuses, and crashes.
  - **Limits:** each at the limit and over it, with `--allow-write` on, and
    through `validate`, `create`, `replace`, and `submit` alike; a resume
    counted by the runs it has left; the worst case counting a video job's
    drift check and a correcting job's correction
    ([Milestone 09](milestone-09-color-preservation.md)); the body limit.
  - **Process arguments:** every path handed to `ffmpeg` and `ffprobe` (the
    commands the media layer builds, captured with its fake runner) is
    absolute, so none can read as an option or a protocol, and the
    `draw-things-cli` arguments come only from `OVERRIDE_TARGETS` and the
    planner.
  - **Logs:** a job's log file holds none of the server's own lines.
- **Audit coverage.** One parametrized test drives each action (submit,
  cancel, resume, park, unpark, hold, release, create, replace, delete a job,
  delete an execution), accepted and refused, with each caller, and reads the
  log back: one row for each, with its caller, a target of the shape B allows,
  and none holding a sentinel placed in a prompt, a YAML text, a job name, or
  a reference. A crash is a row (`internal_error`). Each front end's test
  above proves it sends its own caller; the rows prove the rest, since every
  one of these actions goes through the API.

### D. Documentation (F12)

- **Fix what drifted.** The root `README.md`'s quick start (`dtc serve`, then
  `dtc queue add`, in place of `run-job`), the user guide's `run-job`
  mentions and its server section's run-lock paragraph, and
  `docs/architecture.md`'s stale "Milestones 3 onward (planned)" section and
  its Phase 3 section, so each describes the code as built. The Phase 2
  sections of the architecture, which describe what `run-job` did then, stay
  as history and say so.
- **One section on what an agent can do.** In the user guide's MCP section:
  the token is full access; the MCP tools are a guardrail, not a sandbox, and
  an agent that can read `config/server-token` can call the API as anyone;
  what `--allow-write` turns on (and that `run-server.sh` turns it on); where
  `.trash/`, `.backups/`, and the audit log are; the limits; what a stop, a
  cancel, and a resume lose. It links to the facts it does not restate.
- **A check that keeps the docs honest.** A test reads the fenced `bash`
  blocks of `README.md`, `docs/user-guide.md`, and
  `docs/user-guide-for-http-based-mcp.md`, and fails on a `dtc` command or
  `--option` the Typer app does not have (so `run-job` cannot return), and on a
  relative link that does not resolve. The curl guide is the owner's
  uncommitted work: it is checked once `git` tracks it (a file `git` does not
  track is skipped, so a checkout without it passes), and any change to it is
  the owner's to make.
- **`dtc generate`, until Milestone 12.** It starts `draw-things-cli`
  itself for one image or video, holding the run lock the server holds while
  it runs, so it never overlaps a job. The phase document's exit criterion,
  `AGENTS.md`, and the architecture say that, and that the server's worker is
  the only thing that starts it *for a job*, until Milestone 12 moves
  `dtc generate` onto the queue.

## Owner decisions

Asked after this plan was first written (2026-10-03); each had a recommendation.

1. **Where the containment check lives: in `JobParser`, for every consumer**
   (recommended, chosen). One rule in one place, nothing can read a path
   before it is checked, and `dtc validate-job` and the TUI's job list stop
   showing a job as valid that can no longer run (Milestone 03 retired every
   other way to run one). Tests that name an input outside the input
   directory change. Rejected: only in the API's services, before
   `load_job_text`, which leaves the CLI and the TUI reading any path and
   writes the rule twice (a pre-parse in the services and `check_job_rules`).
2. **`dtc generate` goes through the queue, in a new Milestone 12** (not the
   recommended option, which was to keep it and document it as the one
   exception). It has no job file to snapshot, so Milestone 12 needs a design
   of its own and is not planned here. Until it is built, `dtc generate` runs
   as it does, and this milestone documents it as the exception
   ([D](#d-documentation-f12)). Not chosen: retiring it.
3. **gRPC and SSE both stay for good** (not the recommended option, which was
   to leave the question open for a later milestone). The TUI and `dtc queue
   add --wait` keep gRPC; MCP keeps SSE. This closes the question
   Milestone 10 left for "after Milestone 10", so this milestone hardens
   both transports permanently, and their tests stay. Not chosen: retiring
   gRPC first, and a Milestone 12 for it.

## Decisions taken in this plan

- **Convert at the parse, not at each route.** Alternatives: a `ValueError`
  handler in the app (it would also relabel real bugs as 422 and answers no
  audit row), a wrapper in each route (misses F7, and the next route), and
  converting at the roughly forty `raise ValueError` sites in `core/` and
  `jobs/inputs/` (the loaders' contract and their messages are used by the
  CLI as well, for no gain over one choke point).
- **Test a rule where it lives, and a front end for what it adds.** The first
  plan asked every rule to be tested through the API, MCP, `dtc queue add`,
  and the TUI's `/queue add`. The rules live only in the API; the other three
  pass them on, so four copies would test the API four times and the
  pass-through only incidentally. Each front end instead proves what it owns
  (its caller, its URL and ID checks, its token handling, and that an API
  refusal arrives intact).
- **The audit log records a target only once it is known** (not a bounded
  prefix of what was sent): caller text has no place in the column, and
  `outcome` says what happened to a reference that did not resolve.
- **The unauthenticated 405 stays.** Registering the write routes always,
  to let the token check run first, was rejected: the exit criteria say
  they do not exist while writes are off. Whether writes are on is the one
  fact it tells an unauthenticated caller, and `/v1/capabilities` tells every
  token holder the same.
- **No quotas.** See the accepted findings above.

## Acceptance criteria

- No input of any caller makes a route answer 500: each malformed input of
  the no-500 table gets its stable `{code, message}` (`invalid_input` with
  the field when it has one), through every route, `dtc queue add`, the TUI,
  and MCP, which show the real message. A genuine bug answers `internal_error`.
- A path outside the input directory, or the output directory, is never
  opened or read, by any consumer (`dtc validate-job` and the TUI's job list
  included), and answers the same `outside_directory` whether it exists or
  not; one that is lexically outside is not even stat-ed, and a link inside
  that points out is refused after being resolved, never opened.
- The audit log has one row for every submit, cancel, resume, park, unpark,
  hold, release, create, replace, delete, and execution deleted, refused ones
  and crashes included (`internal_error`); a `target` is a resolved file
  name or a canonical ID, or null, and at most 256 characters; and no row
  holds a prompt, YAML, a command, or a credential.
- A request body of over the limit is refused before it is parsed, on every
  route.
- While the worker's lock and `submission_lock` are held, `/v1/health`
  answers within a second, with a job-file write, a validation, and a gRPC
  watch in flight.
- No credential appears in any response, event, gRPC message, log line
  (a traceback included), audit row, manifest, or the state database.
- Every rule of
  [Milestone 02](milestone-02-http-api.md#rules-for-jobs-the-api-runs) and
  every limit has a boundary test where it is enforced; each front end has
  the checks listed above; the whole suite passes.
- No known path leads from agent input to a file outside `data/jobs/`
  (writes), the input directory (inputs), or the output directory (outputs),
  or to a `draw-things-cli` argument a job file cannot express.
- Every finding of the review is fixed, or recorded in the changelog with a
  reason (F1 to F12 and the accepted ones above).
- The README, the user guide, and the architecture describe what was built,
  the user guide has the section on what an agent can do, and the docs check
  passes.
- `make check` passes.
