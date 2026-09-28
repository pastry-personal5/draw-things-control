.PHONY: run check lint format typecheck test proto

PROTO_OUT = src/draw_things_control/server/generated

run:
	uv run dtc --help

proto:
	rm -rf $(PROTO_OUT)
	mkdir -p $(PROTO_OUT)
	uv run --extra dev python -m grpc_tools.protoc \
		-Isrc/draw_things_control/server/proto \
		--python_out=$(PROTO_OUT) \
		--grpc_python_out=$(PROTO_OUT) \
		--pyi_out=$(PROTO_OUT) \
		src/draw_things_control/server/proto/monitor.proto
	# protoc emits an absolute "import monitor_pb2" in the _grpc file, which only resolves when the generated
	# directory is itself on sys.path; rewrite it to a package-relative import instead of polluting sys.path.
	sed -i.bak 's/^import monitor_pb2 as monitor__pb2$$/from . import monitor_pb2 as monitor__pb2/' $(PROTO_OUT)/monitor_pb2_grpc.py
	rm -f $(PROTO_OUT)/monitor_pb2_grpc.py.bak

lint:
	uv run --extra dev ruff check .
	uv run --extra dev ruff format --check .

format:
	uv run --extra dev ruff format .

typecheck:
	uv run --extra dev pyright

test:
	uv run python -m unittest discover -s tests -t . -v

check: proto lint typecheck test
