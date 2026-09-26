# Development Rules

The rules for changing `draw-things-control`, for people and AI agents alike.
[AGENTS.md](../AGENTS.md) is the short entry point for agents and links here.

## Environment

- Python 3.12 (see `.python-version`), managed with uv.
- Install with `uv sync`. Never use `pip install` directly.
- Run code from the project root: `uv run dtc ...` (or `uv run python -m draw_things_control ...`.
- Keep `pyproject.toml` and `uv.lock` in sync when adding a dependency.
- Before using a library, framework, SDK, or CLI tool, fetch its current
  documentation through the Context7 MCP tools (`resolve-library-id`, then
  `query-docs` scoped to one concept). Do not use Context7 for refactoring,
  scripts from scratch, business-logic debugging, code review, or general
  programming concepts.

## Code style

- **Line length is unlimited** (owner decision). `line-length = 65535` in
  `[tool.ruff]`, which both Ruff's linter and its formatter read; do not lower
  it unless the owner revises the decision. Do not wrap lines to satisfy a
  limit.
- **Ruff lints and formats** (owner decision). Black is not used.
- PEP 8 otherwise; type hints where applicable; small, single-purpose
  functions.
- Entry points use `def main()` and `if __name__ == "__main__":`.
- No trailing whitespace; Unix line endings.
- Match the surrounding code's naming, comment density, and idiom.

## Project layout

- No source files in the project root. All code is the `draw_things_control`
  package under `src/` (uv src layout), with tests in `tests/` mirroring it.
- Subpackages: `core/` (runner, arguments, generation, configuration), `jobs/`
  (job definition, service, manifests, input handling), `cli/` (Typer app),
  and, as phases land, `state/`, `tui/`, `server/`, `mcp_server/`.
- Dependencies point one way: `cli`, `tui`, `server` -> `state` -> `jobs` ->
  `core`. Front ends never import each other, except that the `dtc tui`
  command in `cli/app.py` starts the TUI app (`tui` never imports `cli`); `mcp_server` talks to
  `server` over HTTP only.
- Imports are absolute (`from draw_things_control.core... import ...`).
- Entry point: the `dtc` console script, or `python -m draw_things_control`.
- `dt-config/*.json`, `dt-config/*.yaml`, and `config/global-config.yaml`
  are user files. Never edit, delete, or deduplicate them from code, tests,
  or tools.
- See [architecture.md](architecture.md) for module responsibilities.

## Testing and checks

- Tests use standard-library `unittest` and Typer's `CliRunner`, and live in
  `tests/`.
- No test starts the real `draw-things-cli`; inject a fake runner.
- Test files live in `tests/<subpackage>/`, each directory has an `__init__.py`, and shared helpers are in `tests/fixtures.py`.
- `make check` runs Ruff's linter, Ruff's formatter in check mode, and the tests. It must pass
  before a change is committed.
- `make format` applies Ruff's formatter.

## Documentation

Keep documents current in the same change that alters behavior.

| Document | Purpose |
|----------|---------|
| `README.md` | Concise overview and quick start |
| [user-guide.md](user-guide.md) | How a person uses the CLI |
| [architecture.md](architecture.md) | How the code is organized and supervised |
| this file | Rules for changing the project |
| `AGENTS.md` | Short instructions for AI agents |
| `docs/research/` | Saved upstream help and background notes |
| `docs/phase-<n>/` | Plans and decisions, described below |

### Phases and milestones

```
docs/phase-<n>/README.md                        # phase document
docs/phase-<n>/milestone-<xx>-<theme>.md        # milestone documents
docs/phase-<n>/phase-<n>-changelog.md           # changelog
```

- `<n>` is an unpadded integer from 1. `<xx>` is two digits and restarts at
  `01` in each phase. `<theme>` is short lowercase kebab-case.
- The phase document states the goal, scope, non-goals, the linked ordered
  milestones, and the exit criteria. Create it before the first milestone,
  together with the changelog, and link the changelog from it.
- A milestone document covers goal, scope, planned changes, acceptance
  criteria, and status: `planned`, `in-progress`, or `done`.
- Link every new milestone from its phase document. Update statuses when work
  lands. Never renumber milestones; append new ones.
- Phase documents carry a `**Status:**` line under the title.

### Changelog

One changelog per phase. Entries are newest first under a `## YYYY-MM-DD`
heading, one bullet or short paragraph each, tagged in bold and naming the
milestone when there is one (for example `[M01]`):

- **Owner decision**: an explicit choice by the owner, with their reasoning
  when given.
- **Design decision**: an architectural or format choice, with the
  alternatives considered and why they were rejected.
- **Change**: a notable change to behavior, file formats, commands, or
  structure.

Add the entry in the same change that makes the decision. Never delete or
rewrite past entries; a reversed decision gets a new entry that supersedes and
references the old one. Routine work (typos, refactors with no visible effect)
needs no entry.

### Writing

- Sentence-case headings; one `#` title per file.
- Commands go in fenced `bash` blocks and are copy-pasteable from the project
  root.
- Refer to a CLI option as `--option`, a key or value as `key`, a file or
  path as `path/to/file`.
- State a fact once and link to it elsewhere.

## Git

- `main` is the primary branch. No remote is configured yet.
- Conventional commits: `feat(scope): description`, `fix(scope): ...`,
  `docs(scope): ...`, `build`, `test`, `refactor`.
- Commit or push only when the owner asks.
