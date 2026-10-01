# AGENTS.md

Instructions for AI coding agents working on `draw-things-control`. The full
rules are in [docs/development-rules.md](docs/development-rules.md); read it
before changing anything.

## Project overview

A Python 3.12 project whose mission is long-horizon video generation by autoregressive image-to-video chaining. It uses the Draw Things application beneath, through the locally installed `draw-things-cli`. Phase 1 (CLI and jobs) is done; Phase 2 (TUI, state store, run lock) is done; Phase 3 (API and MCP servers) is in progress: milestones 01 to 06, 08, and 09 are done. See
[docs/architecture.md](docs/architecture.md) for the modules across all three phases and
[docs/user-guide.md](docs/user-guide.md) for usage.

## Technology stack

- **Language and tools**: Python 3.12+, uv (`uv.lock`, `pyproject.toml`), `.venv` managed by uv
- **Layout**: no source in the project root. One package, `src/draw_things_control/`, with `core/`, `jobs/`, `state/`, `services/`, `cli/`, `tui/` and, later, `server/`, `mcp_server/`; tests mirror it in `tests/`
- **Entry point**: `dtc` (Typer, `cli/app.py`) or `python -m draw_things_control`
- **Imports**: absolute; `cli`/`tui`/`server` -> `services` -> `state` -> `jobs` -> `core` (`tests/test_architecture.py` checks it); front ends never import each other, except that `dtc tui` in `cli/app.py` starts the TUI app (`tui` never imports `cli`)
- **Logging**: Loguru
- **Lint, format, and types**: Ruff lints and formats (configured in `pyproject.toml`); no Black; pyright (`standard`) checks `src/` and `tests/`

## Essentials

- `uv sync` to install; run with `uv run dtc`; never `pip install`.
- **Line length is unlimited** (owner decision): `line-length = 65535` in Ruff, for linting and formatting. Do not wrap lines to satisfy a limit.
- **Never edit, delete, or deduplicate `data/params/*.json` or `data/params/*.yaml`.**
- Directories come from `ProjectPaths` (`core/paths.py`) and are passed down; never patch a path in a test.
- `make check` must pass; `make format` applies Ruff's formatter. Tests use `unittest` and Typer's `CliRunner`, and never start the real `draw-things-cli`.
- Use the Context7 MCP tools for current library, framework, SDK, or CLI documentation. Not for refactoring, scripts from scratch, business-logic debugging, code review, or general programming concepts.
- Architecture rules: front ends call services and observe events, never parse logs; one `draw-things-cli` at a time; no credentials in events, storage, logs, or responses.
- Keep docs current in the same change. Phases, milestones, and changelog entries follow [the documentation rules](docs/development-rules.md#documentation); never rewrite past changelog entries.
- Conventional commits (`feat(scope): ...`); commit only when asked.

## Token efficiency

This file is loaded in every session, so keep it short. Long-form material belongs in `docs/`.

- **Skip generated and bulky files.**
  - Never read the `draw-things-cli` binary at the repo root (~160MB), or anything under `.venv/`, `__pycache__/`, `.ruff_cache/`.
  - Never read or search `src/draw_things_control/server/generated/` — gitignored gRPC stubs `make proto` regenerates from `server/proto/monitor.proto`, which stays the single source of truth.
  - Search with `rg` or `git grep`, which skip gitignored files automatically.
  - Don't open `uv.lock`, `LICENSE`, or `docs/archive/` (finished Phase 1/Phase 2 plans) unless the task needs them.
- **Read docs narrowly.** Start with `docs/README.md`'s index, then read only the doc the task needs. For a long doc such as `docs/user-guide.md` or `docs/architecture.md`, list its headings first with `rg -n '^#' <file>` and read just the relevant section.
- **Keep check output small.** While iterating, run one `make` target (`lint`, `typecheck`, or `test`) instead of `make check`, and filter long output, e.g. `uv run --extra dev pyright 2>&1 | rg -i error`.
- **Iterate narrowly, verify once.** Run only the affected test while iterating, e.g. `uv run python -m unittest tests.cli.test_cli -v` or add `-k <pattern>` to the `discover` invocation in `make test`; run `make check` once before calling the change done.
- **Ask before building on an undecided item.** Redoing work is the most expensive outcome.
- **Edit, don't rewrite.** Change files with targeted edits, don't re-read a file just to confirm an edit landed, and check `git diff --stat` before reading a full diff.
- **Reply briefly.** Summarize command output and diffs instead of pasting them; this repo is small enough to search directly rather than delegating searches to subagents.
