# draw-things-control

Long-horizon video generation by autoregressive image-to-video chaining, powered
by the locally installed [Draw Things](https://github.com/drawthingsai/draw-things-community)
CLI. `dtc serve` owns generation; the CLI, terminal UI, HTTP API, and MCP server
submit and monitor jobs through it.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `draw-things-cli`.

```bash
uv sync
uv run dtc --help
```

See the [user guide](docs/user-guide.md) for configuration, generation, jobs,
the TUI, API, and MCP.

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Development rules](docs/development-rules.md)

## License

Apache-2.0
