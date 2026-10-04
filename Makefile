.PHONY: run check lint format typecheck test test-verbose proto

# Keep uv's cache in the checkout so commands never depend on a machine-wide
# cache path. Callers may still supply UV_CACHE_DIR for a shared CI cache.
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_CACHE_DIR

# One copy of the generated stubs per front end that needs a gRPC client (Milestone 02 design decision): front
# ends never import each other (tests/test_architecture.py), so cli/ and tui/ cannot import server/generated's
# copy. server/ is also generated into, since it alone imports the generated *server* code (Servicer, add_..._to_
# server) the same file carries alongside the client Stub every copy shares. mcp_server/ has none: it watches a queue
# entry over the HTTP API's SSE watch (Milestone 10).
PROTO_SRC = src/draw_things_control/server/proto/monitor.proto
PROTO_OUTS = src/draw_things_control/server/generated src/draw_things_control/cli/generated src/draw_things_control/tui/generated

run:
	uv run dtc --help

proto:
	for out in $(PROTO_OUTS); do \
		rm -rf $$out; \
		mkdir -p $$out; \
		uv run --extra dev python -m grpc_tools.protoc \
			-Isrc/draw_things_control/server/proto \
			--python_out=$$out \
			--grpc_python_out=$$out \
			--pyi_out=$$out \
			$(PROTO_SRC); \
		sed -i.bak 's/^import monitor_pb2 as monitor__pb2$$/from . import monitor_pb2 as monitor__pb2/' $$out/monitor_pb2_grpc.py; \
		rm -f $$out/monitor_pb2_grpc.py.bak; \
	done

lint:
	uv run --extra dev ruff check .
	uv run --extra dev ruff format --check .

format:
	uv run --extra dev ruff format .

typecheck:
	uv run --extra dev pyright

test:
	uv run python -m unittest discover -s tests -t .

test-verbose:
	uv run python -m unittest discover -s tests -t . -v

check: proto lint typecheck test
