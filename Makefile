.PHONY: setup quality format hooks test
setup:
	uv sync --frozen --group dev
quality:
	uv run --frozen python scripts/quality/check.py
format:
	uv run --frozen ruff format service mcp tests scripts/quality
hooks:
	uv run --frozen pre-commit install --hook-type pre-commit --hook-type pre-push
test:
	uv run --frozen pytest -v
