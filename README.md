# draw-things-control

A Python CLI that drives the locally installed
[Draw Things CLI](https://github.com/drawthingsai/draw-things-community):
generate images or video, and run YAML jobs that chain runs, each starting
from the previous run's output.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and `draw-things-cli`.

```bash
uv sync
uv run dtc --help

# Preview a command without running it
uv run dtc generate --model flux_2_klein_4b_q6p.ckpt --prompt "a small red cube" --output cube.png --dry-run

# Run a job
cp config/global-config.example.yaml config/global-config.yaml   # then edit the paths
uv run dtc validate-job data/example-job.yaml
uv run dtc run-job data/example-job.yaml
```

## Documentation

- [User guide](docs/user-guide.md): commands, jobs, and outputs
- [Architecture](docs/architecture.md): modules, process supervision, exit codes
- [Development rules](docs/development-rules.md): style, checks, docs, git
- [Plans and changelogs](docs/README.md): phases 1 to 3

## Development

```bash
make check    # Ruff, Black, and tests
```

## License

Apache-2.0
