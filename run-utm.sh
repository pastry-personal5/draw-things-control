#!/usr/bin/env bash
# Start the loopback API and the guest-facing MCP listener together.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
api_pid=""
mcp_pid=""

stop_services() {
    if [[ -n "$mcp_pid" ]]; then
        kill -TERM "$mcp_pid" 2>/dev/null || true
        wait "$mcp_pid" 2>/dev/null || true
    fi
    if [[ -n "$api_pid" ]]; then
        kill -TERM "$api_pid" 2>/dev/null || true
        wait "$api_pid" 2>/dev/null || true
    fi
}

trap stop_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$project_root/run-host-network-device-server.sh" &
api_pid=$!
"$project_root/run-mcp-server.sh" &
mcp_pid=$!

while kill -0 "$api_pid" 2>/dev/null && kill -0 "$mcp_pid" 2>/dev/null; do
    sleep 1
done

status=0
if ! kill -0 "$api_pid" 2>/dev/null; then
    wait "$api_pid" || status=$?
else
    wait "$mcp_pid" || status=$?
fi
exit "$status"
