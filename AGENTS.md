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

## Development Conventions

- Use `uv sync` to install dependencies — do not use `pip install` directly
- Run code via `uv run python main.py` from the project root
- Python version must be 3.12 (see `.python-version`)
- Keep `pyproject.toml` and `uv.lock` in sync when adding dependencies
- All new code must follow PEP 8 style conventions

## Documentation

- **README.md**: Project overview, setup instructions, usage guide
- **AGENTS.md**: Agent-specific instructions (this file)

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
- The canonical machine-readable setting is `line-length = 65535` in
  `[tool.ruff]` in `pyproject.toml`: Ruff requires a positive number, and this
  is its largest supported value. Do not lower it unless the owner revises this
  decision.

- Use `def main()` as the entry point pattern
- Guard with `if __name__ == "__main__":`
- Prefer type hints where applicable
- Keep functions small and single-purpose
- No trailing whitespace; use Unix line endings
- Line length is unlimited in practice (`line-length = 65535` in
  `[tool.ruff]`); see the owner decision above.

## Testing and Linting

- Use `uv run --extra dev ruff check .` for linting — see Ruff configuration in `pyproject.toml`
- Ruff is preconfigured with pycodestyle, pyflakes, isort, bugbear, and comprehensions
- Line length is unlimited in practice (`line-length = 65535`) by explicit
  owner decision
- Run `uv run ruff format` to apply consistent formatting
- Tests use standard-library `unittest` and Typer's `CliRunner`
- Place tests in `tests/` and run them with `make check`

## Git Conventions

- Commit messages follow conventional commits format: `feat(scope): description`, `fix(scope): description`, etc.
- Branch naming: `main` is the primary branch
- No remote configured yet; set one up when pushing to a hosted provider
