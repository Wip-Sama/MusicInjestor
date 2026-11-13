.PHONY: setup test lint format check run run-with-provider

setup:
	uv sync
	uv run pre-commit install --hook-type pre-commit --hook-type pre-push

test:
	uv run pytest

lint:
	uv run ruff check --fix
	uv run ty check

format:
	uv run ruff format

pre-commit:
	uv run pre-commit run --all-files

pre-push:
	uv run pre-commit run --hook-stage pre-push --all-files

run:
	uv run python main.py

run-with-provider:
	uv run python provider/run_with_bot.py

check: lint test

