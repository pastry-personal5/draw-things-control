.PHONY: run check lint format typecheck test

run:
	uv run dtc --help

lint:
	uv run --extra dev ruff check .
	uv run --extra dev ruff format --check .

format:
	uv run --extra dev ruff format .

typecheck:
	uv run --extra dev pyright

test:
	uv run python -m unittest discover -s tests -t . -v

check: lint typecheck test
