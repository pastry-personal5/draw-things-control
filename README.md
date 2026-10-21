# Framecontrol

[![Release](https://img.shields.io/github/v/tag/pastry-personal5/frame-control?label=release)](docs/releases/4.0.0.md)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](docs/development-rules.md)

Long-horizon video generation by autoregressive image-to-video chaining, powered
by the locally installed [Draw Things](https://github.com/drawthingsai/draw-things-community)
CLI. `fc serve` owns generation; the CLI, terminal UI, HTTP API, and MCP server
submit and monitor jobs through it.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `draw-things-cli`.

```bash
uv sync
uv run fc --help
```

See the [user guide](docs/user-guide.md) for configuration, generation, jobs,
the TUI, API, and MCP.

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Development rules](docs/development-rules.md)

## License

Apache-2.0
