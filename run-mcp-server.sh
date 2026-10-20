#!/usr/bin/env bash
# MCP over Streamable HTTP for a client that cannot start `fc mcp` itself, such as OpenClaw in a virtual machine.
# Run it beside run-host-network-device-server.sh: only this listener faces the guest.

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-"$project_root/.cache/uv"}"

exec uv run fc mcp \
--transport=streamable-http \
--host=192.168.64.1 \
--allow-remote-bind \
--server-url=http://127.0.0.1:8765
