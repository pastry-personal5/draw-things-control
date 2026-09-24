.PHONY: run check lint format test

run:
	uv run python main.py --help

lint:
	uv run --extra dev ruff check .
	uv run --extra dev black --check .

format:
	uv run --extra dev black .

test:
	uv run python -m unittest discover -s tests -v

check: lint test
