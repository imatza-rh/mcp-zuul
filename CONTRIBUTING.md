# Contributing to mcp-zuul

Contributions welcome! Please open an issue first to discuss significant changes.

## Development Setup

```bash
git clone https://github.com/imatza-rh/mcp-zuul.git
cd mcp-zuul
uv sync --extra dev
```

## Running Tests

```bash
uv run pytest tests/ -v              # all tests
uv run pytest tests/ -v -k "name"    # single test
uv run ruff check src/ tests/        # lint
uv run ruff format src/ tests/       # format
uv run mypy src/mcp_zuul/            # type check
```

Or use `make check` to run all three (lint, typecheck, test).

## Adding a New Tool

1. Add the tool function to `src/mcp_zuul/tools.py` with the `@mcp.tool()` and `@handle_errors` decorators
2. Use `_READ_ONLY` or `_WRITE` annotations
3. All tools must return JSON strings (use `json.dumps()`)
4. Accept optional `tenant` param resolved via `_tenant(ctx, tenant)`
5. For build/buildset tools, accept `url` param via `_resolve()`
6. Add tests in `tests/` using `respx` for HTTP mocking and `mock_ctx` fixture
7. Update the tool count in `README.md` and `CLAUDE.md`

## Adding a New Prompt or Resource

- Prompts go in `src/mcp_zuul/prompts.py` with `@mcp.prompt()`
- Resources go in `src/mcp_zuul/resources.py` with `@mcp.resource()`
- Both use the same helpers (`api`, `safepath`, `clean`, `_tenant`)

## Commit Convention

Commits follow `[SCOPE] Subject` format:
- Scopes: FEATURE, BUGFIX, DOCS, STYLE, REFACTOR, PERFORMANCE, TEST, CHORE, CI, SECURITY
- Subject: imperative mood, max 50 chars
- Body: wrap at 72 chars, explain *why* not *what*

## Code Style

- Ruff enforces formatting and lint rules (see `pyproject.toml`)
- Line length: 100 characters
- Target: Python 3.11+
- All tools return JSON strings via `json.dumps()`
- Use `clean()` to strip None values from dicts (saves tokens)
- Use `safepath()` for URL path segments (prevents traversal)
