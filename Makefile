.PHONY: run check lint test

run:
	uv run python main.py --help

lint:
	uv run --extra dev ruff check .

test:
	uv run python -m unittest discover -s tests -v

check: lint test
