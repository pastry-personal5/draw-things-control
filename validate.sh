#!/usr/bin/env bash

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-"$project_root/.cache/uv"}"

uv run dtc \
validate-job \
./data/jobs/duo.yaml 
