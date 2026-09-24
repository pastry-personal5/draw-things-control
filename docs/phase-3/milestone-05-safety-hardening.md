# Milestone 05: Safety Hardening

**Phase:** [Phase 3: API Server and MCP Server for AI](README.md)
**Status:** planned
**Depends on:** [Milestone 03: Job file management](milestone-03-job-file-management.md), [Milestone 04: MCP server](milestone-04-mcp-server.md)

## Goal

Make the agent-facing surface safe by construction: bounded work, confined
paths, no secrets in output, and proof that what agents did is recorded.

## Scope

In scope:

- Tests that the audit log from [Milestone 02](milestone-02-http-api.md#audit-log)
  covers every submission and write
- A security test suite for confinement, redaction, and auth
- A review of every place agent input reaches the file system or a process,
  including that the limits from [Milestone 02](milestone-02-http-api.md#limits)
  cannot be bypassed

Out of scope:

- Network hardening (TLS, rate limiting per client): the server is loopback
  only by default
- Multi-user roles
- Sandboxing `draw-things-cli` itself

## Planned changes

### Confinement review and tests

A test suite (`tests/`) that tries to break out, run against the API and the
MCP tools:

- Names: `..`, `../x`, `a/b`, `a\b`, absolute paths, NUL, very long names,
  upper case, and dots.
- Symlinks in `data/` and in the input directory pointing outside.
- Job inputs referencing `/etc/passwd`, `~`, `..`, or a symlink out of the
  input directory.
- Writes and deletes with `--allow-write` off, and on.
- Auth: no header, wrong token, token with different case, token in a query
  string (not accepted).
- Redaction: submit a job whose command would include `--api-key` and
  `--remote-shared-secret`; check responses, events, the database, logs, and
  manifests for the values.
- MCP: no tool takes a path or a flag; an unknown argument is rejected.

Findings from the review are fixed in this milestone, or recorded as
follow-ups in the changelog with a reason.

### Documentation

`docs/user-guide.md` gets a Server and MCP section (the root `README.md` stays
short and only links to it): how to start `serve`, where the
token is, how to configure an MCP client (a sample entry), the write flag and
its effect, the limits, and where `.trash/` and `.backups/` are.

## Acceptance criteria

- Every limit from Milestone 02 is covered by a boundary test in the security
  suite (at the limit, over it, and with `--allow-write` on).
- The audit log has an entry for every submit, cancel, resume, create,
  replace, and delete, including refused ones, and none contains prompts,
  YAML, commands, or credentials (a test drives each action through the API
  and through MCP and reads the log back).
- The whole security suite passes.
- No known path from agent input to a file outside `data/` (writes) or the
  input directory (reads), or to an unvalidated `draw-things-cli` argument.
- README documents the server, the MCP client entry, the write flag, and the
  limits.
- `make check` passes.
