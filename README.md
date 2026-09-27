# draw-things-control

The mission of this project is long-horizon video generation by autoregressive
image-to-video chaining: each clip starts from the last frame of the one before,
so a video can run far longer than a single generation allows. The
[Draw Things](https://github.com/drawthingsai/draw-things-community) app does
the generating underneath; this Python CLI and terminal UI drive it through the
locally installed `draw-things-cli`. YAML jobs describe the chain, and single
`dtc generate` runs cover the one-off image or video.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `draw-things-cli`.

```bash
uv sync
uv run dtc --help

# Preview a command without running it
uv run dtc generate --model flux_2_klein_4b_q6p.ckpt --prompt "a small red cube" --output cube.png --dry-run

# Run a job
cp config/global-config.example.yaml config/global-config.yaml   # then edit the paths
uv run dtc validate-job data/jobs/example-job.yaml
uv run dtc run-job data/jobs/example-job.yaml

# Browse, run, and watch jobs, and review past runs, in a terminal UI
uv run dtc tui
```

## Documentation

- [User guide](docs/user-guide.md): commands, jobs, outputs, and the terminal UI
- [Architecture](docs/architecture.md): modules, process supervision, exit codes
- [Development rules](docs/development-rules.md): style, checks, docs, git
- [Plans and changelogs](docs/README.md): phases 1 to 3 (1 and 2 are archived)

## Development

```bash
make check    # Ruff lint, Ruff format check, and tests
```

## License

Apache-2.0
