.PHONY: run check lint format typecheck test test-verbose test-tui test-tui-verbose test-profile proto

# Keep uv's cache in the checkout so commands never depend on a machine-wide
# cache path. Callers may still supply UV_CACHE_DIR for a shared CI cache.
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_CACHE_DIR

TEST_WORKERS ?= 8
TEST_PROFILE_LIMIT ?= 20
PYTEST = uv run --extra dev pytest

# One copy of the generated stubs per front end that needs a gRPC client (Milestone 02 design decision): front
# ends never import each other (tests/test_architecture.py), so cli/ and tui/ cannot import server/generated's
# copy. server/ is also generated into, since it alone imports the generated *server* code (Servicer, add_..._to_
# server) the same file carries alongside the client Stub every copy shares. mcp_server/ has none: it watches a queue
# entry over the HTTP API's SSE watch (Milestone 10).
PROTO_SRC = src/framechain/server/proto/monitor.proto
PROTO_OUTS = src/framechain/server/generated src/framechain/cli/generated src/framechain/tui/generated

run:
	uv run fc --help

proto:
	@for out in $(PROTO_OUTS); do \
		rm -rf $$out; \
		mkdir -p $$out; \
		uv run -q --extra dev python -m grpc_tools.protoc \
			-Isrc/framechain/server/proto \
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
	@parallel_output=$$(mktemp); isolated_output=$$(mktemp); \
	$(PYTEST) -q -n "$(TEST_WORKERS)" --dist loadfile --ignore=tests/core/test_process_output.py >"$$parallel_output" 2>&1; parallel_result=$$?; \
	if [ "$$parallel_result" -eq 0 ]; then $(PYTEST) -q tests/core/test_process_output.py >"$$isolated_output" 2>&1; isolated_result=$$?; else isolated_result=0; fi; \
	if [ "$$parallel_result" -ne 0 ]; then printf '%s\\n' 'Parallel test suite failed:'; cat "$$parallel_output"; \
	elif [ "$$isolated_result" -ne 0 ]; then printf '%s\\n' 'Isolated process-output test suite failed:'; cat "$$isolated_output"; fi; \
	rm -f "$$parallel_output" "$$isolated_output"; \
	test "$$parallel_result" -eq 0 && test "$$isolated_result" -eq 0

test-verbose:
	$(PYTEST) -vv -n "$(TEST_WORKERS)" --dist loadfile --ignore=tests/core/test_process_output.py
	$(PYTEST) -vv tests/core/test_process_output.py

test-tui:
	@output=$$(mktemp); \
	$(PYTEST) -q -n "$(TEST_WORKERS)" --dist loadfile tests/tui >"$$output" 2>&1; result=$$?; \
	if [ "$$result" -ne 0 ]; then cat "$$output"; fi; \
	rm -f "$$output"; \
	exit "$$result"

test-tui-verbose:
	$(PYTEST) -vv -n "$(TEST_WORKERS)" --dist loadfile tests/tui

test-profile:
	$(PYTEST) -q --durations="$(TEST_PROFILE_LIMIT)"

check: proto
	+$(MAKE) --no-print-directory -j3 lint typecheck test
