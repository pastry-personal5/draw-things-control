# AGENTS.md

Instructions and conventions for AI coding agents working on `draw-things-control`.

## Project Overview

This is a Python 3.12 project (`draw-things-control`) that controls the Draw Things image generation application. It provides programmatic access to generation features via Python scripts.

## Technology Stack

- **Language**: Python 3.12+
- **Package Manager**: uv (via `uv.lock` and `pyproject.toml`)
- **Virtual Environment**: `.venv` (managed by uv)
- **Project Layout**: Flat modules with clear boundaries; `main.py` is the entry point
- **CLI**: Typer in `main.py`; the application use case is `generation_service.py`
- **Arguments**: `draw_things_arguments.py` owns validated options and command serialization
- **Process I/O**: `draw_things_runner.py` supervises processes; `process_output.py` classifies output
- **Logging**: Loguru for process output and status messages
- **Linter**: Ruff (configured in `pyproject.toml`)
- **Formatter**: Black (configured in `pyproject.toml`)

## Development Conventions

- Use `uv sync` to install dependencies — do not use `pip install` directly
- Run code via `uv run python main.py` from the project root
- Python version must be 3.12 (see `.python-version`)
- Keep `pyproject.toml` and `uv.lock` in sync when adding dependencies
- All new code must follow PEP 8 style conventions

## Documentation

- **README.md**: Project overview, setup instructions, usage guide
- **AGENTS.md**: Agent-specific instructions (this file)
- **docs/research/**: Background research notes

### Phase and Milestone Documents

The app is developed phase by phase. Each phase contains one or more
milestones.

```
docs/
  phase-1/
    README.md                        # phase document
    milestone-01-<theme>.md          # milestone documents
    milestone-02-<theme>.md
  phase-2/
    README.md
    milestone-01-<theme>.md
```

- **Phase directory**: `docs/phase-<n>/`, where `<n>` is an unpadded integer
  starting at 1 (`phase-1`, `phase-2`, ...).
- **Phase document**: `docs/phase-<n>/README.md`. It states the phase goal,
  scope and non-goals, the ordered list of milestones (linked), and the exit
  criteria for completing the phase.
- **Milestone document**: `docs/phase-<n>/milestone-<xx>-<theme>.md`, where
  `<xx>` is a two-digit number (`01`, `02`, ...) that restarts at `01` in each
  phase, and `<theme>` is a short lowercase kebab-case description (for
  example, `milestone-01-cli-foundation.md`).
- Each milestone document covers: goal, scope, planned changes, acceptance
  criteria, and status (`planned`, `in-progress`, or `done`).
- Create the phase document before its first milestone document, and add a
  link to every new milestone in the phase document.
- Keep documents current: update a milestone's status and the phase document
  when work lands. Do not renumber existing milestones; append new ones.

### Phase Changelog

Important owner decisions, design decisions, and changes are recorded in one
changelog per phase: `docs/phase-<n>/phase-<n>-changelog.md`
(`docs/phase-1/phase-1-changelog.md`, `docs/phase-2/phase-2-changelog.md`, ...).

- Record three kinds of entries, each tagged in the heading line:
  - **Owner decision**: an explicit choice by the project owner (for example,
    "unlimited line length", "runs are chained"). Include the owner's reasoning
    when it was given.
  - **Design decision**: an architectural or format choice made while planning
    or building (for example, "prompts pair by position"), with the
    alternatives considered and why they were rejected.
  - **Change**: a notable change to behavior, file formats, commands, or
    project structure.
- Entries are newest first, under a date heading (`## YYYY-MM-DD`), one bullet
  or short paragraph each, and name the milestone they belong to when there is
  one (for example, `[M01]`).
- Add the entry in the same change that makes the decision or the change; do
  not batch entries up for later.
- Never delete or rewrite past entries. If a decision is reversed, add a new
  entry that supersedes the old one and references it.
- Create the changelog together with the phase document
  (`docs/phase-<n>/README.md`), and link it from there.
- Routine work (typo fixes, refactors with no visible effect) does not need an
  entry.

## Context7 MCP Usage

When the user asks about a library, framework, SDK, API, CLI tool, or cloud service — even well-known ones like React, Next.js, Prisma, Express, Tailwind, or Spring Boot — use the Context7 MCP tools to fetch current documentation. This is required per project instructions.

### Steps:
1. Start with `resolve-library-id` using the library name and what to look up
2. Pick the best match (ID format: `/org/project`) by exact name match, description relevance, code snippet count, source reputation, and benchmark score
3. `query-docs` with the selected library ID scoped to a single concept
4. Answer using the fetched docs

Do not use Context7 for: refactoring, writing scripts from scratch, debugging business logic, code review, or general programming concepts.

## Code Style

### Owner decisions

- **Maximum line length: unlimited.** This is an explicit owner decision and
  takes precedence over PEP 8's usual 79-character guidance and any default
  formatter wrapping behavior. Keep a line intact when that is clearer; do not
  introduce manual wrapping solely to satisfy a character limit.
- **Formatter: Black.** This is an explicit owner decision; Ruff is used
  only for linting, not formatting.
- The canonical machine-readable setting is `line-length = 65535` in both
  `[tool.ruff]` and `[tool.black]` in `pyproject.toml`: both tools require a
  positive number, and this is Ruff's largest supported value. Keep the two
  values equal, and do not lower them unless the owner revises this decision.

- Use `def main()` as the entry point pattern
- Guard with `if __name__ == "__main__":`
- Prefer type hints where applicable
- Keep functions small and single-purpose
- No trailing whitespace; use Unix line endings
- Line length is unlimited in practice (`line-length = 65535` in
  `[tool.ruff]` and `[tool.black]`); see the owner decision above.

## Testing and Linting

- Use `uv run --extra dev ruff check .` for linting — see Ruff configuration in `pyproject.toml`
- Ruff is preconfigured with pycodestyle, pyflakes, isort, bugbear, and comprehensions
- Line length is unlimited in practice (`line-length = 65535`) by explicit
  owner decision
- Run `make format` (`uv run --extra dev black .`) to format code; `make check` fails if Black would reformat anything
- Tests use standard-library `unittest` and Typer's `CliRunner`
- Place tests in `tests/` and run them with `make check`

## Git Conventions

- Commit messages follow conventional commits format: `feat(scope): description`, `fix(scope): description`, etc.
- Branch naming: `main` is the primary branch
- No remote configured yet; set one up when pushing to a hosted provider
