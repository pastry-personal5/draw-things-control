# AGENTS.md

Instructions for AI coding agents working on `draw-things-control`. The full
rules are in [docs/development-rules.md](docs/development-rules.md); read it
before changing anything.

## Project overview

A Python 3.12 project that controls the Draw Things image generation
application through the locally installed `draw-things-cli`. Phase 1 (CLI and jobs) is done; Phase 2 (TUI, state store, run lock) and Phase 3 (API and MCP servers) are planned. See
[docs/architecture.md](docs/architecture.md) for the modules across all three phases and
[docs/user-guide.md](docs/user-guide.md) for usage.

## Technology stack

- **Language and tools**: Python 3.12+, uv (`uv.lock`, `pyproject.toml`), `.venv` managed by uv
- **Layout**: no source in the project root. One package, `src/draw_things_control/`, with `core/`, `jobs/`, `cli/` and, later, `state/`, `tui/`, `server/`, `mcp_server/`; tests mirror it in `tests/`
- **Entry point**: `dtc` (Typer, `cli/app.py`) or `python -m draw_things_control`
- **Imports**: absolute; `cli`/`tui`/`server` -> `jobs`, `state` -> `core`; front ends never import each other
- **Logging**: Loguru
- **Lint and format**: Ruff lints, Black formats (both configured in `pyproject.toml`)

## Essentials

- `uv sync` to install; run with `uv run dtc`; never `pip install`.
- **Line length is unlimited** (owner decision): `line-length = 65535` in Ruff and Black. Do not wrap lines to satisfy a limit.
- **Never edit, delete, or deduplicate `dt-config/*.json`.**
- `make check` must pass; `make format` applies Black. Tests use `unittest` and Typer's `CliRunner`, and never start the real `draw-things-cli`.
- Use the Context7 MCP tools for current library, framework, SDK, or CLI documentation. Not for refactoring, scripts from scratch, business-logic debugging, code review, or general programming concepts.
- Architecture rules: front ends call services and observe events, never parse logs; one `draw-things-cli` at a time; no credentials in events, storage, logs, or responses.
- Keep docs current in the same change. Phases, milestones, and changelog entries follow [the documentation rules](docs/development-rules.md#documentation); never rewrite past changelog entries.
- Conventional commits (`feat(scope): ...`); commit only when asked.
