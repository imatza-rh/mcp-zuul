.PHONY: help install test lint format typecheck check build clean release

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

docs: ## Serve docs locally (http://127.0.0.1:8001)
	uvx --with mkdocs-material mkdocs serve -a 127.0.0.1:8001

check: lint typecheck test ## Run all checks (lint + typecheck + test)

build: ## Build Docker image
	docker build -t mcp-zuul .

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

release: ## Release a version (make release V=0.5.0 or V=patch)
	@test -n "$(V)" || (echo "Usage: make release V=<version|patch|minor|major>" && exit 1)
	./release.sh $(V)

release-check: check ## Dry-run release validation (lint + typecheck + test + format + build)
	uv run ruff format --check src/ tests/
	@security find-generic-password -a pypi -s mcp-zuul -w >/dev/null 2>&1 \
		|| (echo "WARNING: PyPI token not found in keychain (release will fail at publish step)" && exit 1)
	uv build
	@echo "Release validation passed. Run 'make release V=<version>' to publish."
