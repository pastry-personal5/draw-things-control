# Milestone 11: Safety Hardening

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 07: Job file management](milestone-07-job-file-management.md), [Milestone 10: MCP server](milestone-10-mcp-server.md), [Milestone 03: Queue for people](milestone-03-queue-for-people.md) (built before this one)

## Goal

Show that the agent-facing surface is safe by construction: bounded work,
confined paths, no secrets in output, and a complete record of what agents
did. Each earlier milestone tests its own rules; this one reviews them all
together, from the attacker's side, and fixes what it finds.

## Scope

In scope:

- A review of every place agent input reaches the file system, a process, or
  the state store
- A security test suite, run against the API, the gRPC monitoring service,
  the MCP tools, `dtc queue`, and the TUI's Queue widget, covering each
  rule of Milestones 02, 03, 07, and 08 at its boundary
- Tests that the audit log from
  [Milestone 02](milestone-02-http-api.md#audit-log) covers every submission
  and write
- A final check of the documentation against what was built

Out of scope:

- Network hardening (TLS, rate limiting per client): the server is loopback
  only unless the owner passes `--allow-remote-bind`
- Multi-user roles
- Sandboxing `draw-things-cli` itself

## Planned changes

### Review

Every input is traced to where it ends: path and query parameters, bodies,
headers, MCP arguments, and each key of a job's text, to a file read or
written, a directory created, a `draw-things-cli` argument, a SQL statement,
or a log line. Each must be covered by a rule and a test. Findings are fixed
in this milestone, or recorded as follow-ups in the changelog with a reason.

### Security test suite

In `tests/server/`, `tests/mcp_server/`, `tests/cli/`, and `tests/tui/`, with
a fake runner, as every test is:

- **Names and references:** `..`, `../x`, `a/b`, `a\b`, absolute paths,
  percent-encoded slashes and dots, NUL, very long names, upper case, dots,
  and a job ID that was never given.
- **Symbolic links** in `data/jobs/` (as a job file, and as the target of a
  write) and in the input directory, pointing outside.
- **Job text:** `input` as `/etc/passwd`, `~/x.png`, `../x.png`, or a
  symbolic link out of the input directory; `output.directory` absolute,
  under `~`, or with `..`; `config_file` with a path; text over the size
  limit; deeply nested YAML.
- **Writes and deletes** with `--allow-write` off, and on.
- **Auth and binding:** no header, a wrong token, the token in another
  letter case, the token in the query string, a `Host` that is neither
  loopback nor the bound address, a token file others can read, a
  non-loopback `--host` or `--grpc-port` bind without `--allow-remote-bind`,
  a gRPC call with no `authorization` metadata or a wrong one (ends
  `UNAUTHENTICATED`), and a non-loopback `--server-url` for `dtc mcp` or
  `dtc queue` without `--allow-remote-server`.
- **Credentials.** A job cannot carry one: no job key reaches a flag in
  `SECRET_FLAGS`, which a structural test checks over `OVERRIDE_TARGETS` and
  the arguments `JobPlanner` builds. And a run stored with an unredacted
  `--api-key` and `--remote-shared-secret`, inserted straight into a test
  store, is served redacted by `/executions`, the events, and the MCP
  tools. The token appears in no response, event, gRPC stream, log line,
  audit row, or manifest.
- **MCP:** no tool takes a path or a flag, and an unknown argument is
  rejected.
- **Limits:** each at the limit, over it, and with `--allow-write` on; a
  resume is counted by the runs it has left.
- **Logs:** a job's log file holds none of the server's own lines.

### Audit coverage

A test drives each action (submit, cancel, resume, create, replace, delete),
accepted and refused, through the API, through MCP, and, for those they
have, through `dtc queue` and the TUI's Queue widget, and reads the log
back: one row for each, with its caller, and none holding prompts, YAML,
commands, or credentials.

### Documentation

The user guide's server, MCP, and queue sections, written by Milestones 02,
03, 07, and 08, are checked against what was built: starting `serve`, where
the token is, a sample MCP client entry, `dtc queue`, the write flag and its
effect, the rules and limits, what a stop or a resume loses, and where
`.trash/` and `.backups/` are. The root `README.md` stays
short and gains one line linking to them. `docs/architecture.md`'s Phase 3
section describes the code as built.

## Acceptance criteria

- The whole security suite passes.
- Every limit and every
  [rule for jobs the API runs](milestone-02-http-api.md#rules-for-jobs-the-api-runs)
  has a boundary test, through the API, through MCP, through
  `dtc queue add`, and through the TUI's `/queue add`.
- The audit log has an entry for every submit, cancel, resume, create,
  replace, and delete, including refused ones, and none contains prompts,
  YAML, commands, or credentials.
- No known path leads from agent input to a file outside `data/jobs/`
  (writes), the input directory (inputs), or the output directory
  (outputs), or to a `draw-things-cli` argument a job file cannot express.
- Every finding of the review is fixed, or recorded in the changelog with a
  reason.
- The user guide documents the server, the MCP client entry, `dtc queue`,
  the write flag, and the limits, and `README.md` links to it.
- `make check` passes.
