# AGENTS.md

Read [the development rules](docs/development-rules.md) before changing
anything. Use the [documentation index](docs/README.md) to find only the
project document needed for the task.

## Safeguards

- Never read the root `draw-things-cli` binary, `.venv/`, caches, or generated
  gRPC stubs. Search with `rg` or `git grep`.
- Never edit, delete, or deduplicate `data/params/*.json`,
  `data/params/*.yaml`, or `config/global-config.yaml`.
- Use the checked-in `dtc` MCP tools only when the owner asks; they can act on
  the real queue and history.
- Use Context7 for current library, framework, SDK, API, CLI, or cloud-service
  documentation; do not use it for project logic or general programming.
- Keep documentation current, never rewrite old changelog entries, run
  `make check`, and commit only when asked.
