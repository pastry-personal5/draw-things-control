# Documentation

`draw-things-control` is built for long-horizon video generation by autoregressive image-to-video chaining, with the Draw Things app doing the generating underneath.

- [User guide](user-guide.md): using the CLI and the terminal UI
- [Testing the API with curl](user-guide-for-http-based-mcp.md): exercising the HTTP API that `dtc mcp`'s tools call, by hand
- [Architecture](architecture.md): modules, process supervision, exit codes
- [Development rules](development-rules.md): style, checks, documentation, git
- Plans and decisions, by phase:
  - [Phase 1](archive/phase-1/README.md): jobs and chained runs (done)
  - [Phase 2](archive/phase-2/README.md): terminal UI, state store, run lock (done)
  - [Phase 3](phase-3/README.md): API server and MCP server (in progress)
  Finished phases live in [archive/](archive/).
- [Research](research/): saved upstream help, the original example command, and background notes such as [what draw-things-cli can resume](research/draw-things-cli-resume.md), [where a chain's colors drift](research/color-drift.md), and [MCP token use and events](research/mcp-tokens-and-events.md)
