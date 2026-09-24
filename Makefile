.PHONY: run check lint format test

run:
	uv run dtc --help

lint:
	uv run --extra dev ruff check .
	uv run --extra dev black --check .

format:
	uv run --extra dev black .

test:
	uv run python -m unittest discover -s tests -t . -v

check: lint test
