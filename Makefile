.PHONY: help install test lint format typecheck check build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies
	uv sync --extra dev

test: ## Run tests
	uv run pytest tests/ -v

lint: ## Run linter
	uv run ruff check src/ tests/

format: ## Auto-format code
	uv run ruff format src/ tests/

typecheck: ## Run type checker
	uv run mypy src/mcp_zuul/

check: lint typecheck test ## Run all checks (lint + typecheck + test)

build: ## Build Docker image
	docker build -t mcp-zuul .

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
