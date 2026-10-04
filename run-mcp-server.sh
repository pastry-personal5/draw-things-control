#!/usr/bin/env bash
# MCP over Streamable HTTP for a client that cannot start `dtc mcp` itself, such as OpenClaw in a virtual machine.
# Run it beside run-server.sh: it calls the API there, with the token in config/server-token.

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-"$project_root/.cache/uv"}"

uv run dtc mcp \
--transport=streamable-http \
--host=192.168.64.1 \
--allow-remote-bind \
--server-url=http://192.168.64.1:8765 \
--allow-remote-server
