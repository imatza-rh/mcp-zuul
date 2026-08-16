.PHONY: help install test lint format typecheck check audit ci generate-docs build clean release release-check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies
	uv sync --extra dev

test: ## Run tests with coverage
	uv run pytest tests/ -v --cov=mcp_zuul --cov-fail-under=85 --cov-report=term-missing

lint: ## Run linter
	uv run ruff check src/ tests/

format: ## Auto-format code
	uv run ruff format src/ tests/

typecheck: ## Run type checker
	uv run mypy src/mcp_zuul/

audit: ## Audit dependencies for known CVEs
	uv run pip-audit

docs: ## Serve docs locally (http://127.0.0.1:8001)
	uvx --with mkdocs-material mkdocs serve -a 127.0.0.1:8001

generate-docs: ## Auto-generate docs/tools.md from registered tool metadata
	uv run python scripts/generate_tool_docs.py

ci: lint typecheck audit test ## Run full CI suite locally

check: ci ## Alias for ci

build: ## Build Docker image
	docker build -t mcp-zuul .

clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

release: ## Release a version (make release V=0.5.0 or V=patch)
	@test -n "$(V)" || (echo "Usage: make release V=<version|patch|minor|major>" && exit 1)
	./release.sh $(V)

release-check: ci ## Dry-run release validation
	uv run ruff format --check src/ tests/
	uv build
	@echo "Release validation passed. Run 'make release V=<version>' to publish."
