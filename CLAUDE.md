# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

MCP server for Zuul CI — 20 read-only tools, 3 prompts, and 3 resources exposing builds, logs, pipelines, jobs, infrastructure, and live status via the Model Context Protocol. Published on PyPI as `mcp-zuul`. Uses stdio transport.

## Commands

```bash
# Install dev dependencies
uv sync --extra dev

# Run the server locally
ZUUL_URL=https://softwarefactory-project.io/zuul uv run mcp-zuul

# Tests
uv run pytest tests/ -v                    # all tests
uv run pytest tests/test_tools_builds.py -v  # single test file
uv run pytest tests/ -v -k "test_name"     # single test by name

# Lint and format
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/     # check only
uv run ruff format src/ tests/             # auto-fix

# Type check
uv run mypy src/mcp_zuul/
```

## Architecture

All source lives in `src/mcp_zuul/`. The package uses `hatchling` as build backend with `src` layout.

### Module Dependency Flow

```
__init__.py   →  imports tools, prompts, resources (registers decorators), exports main()
server.py     →  FastMCP instance ("zuul-ci"), lifespan (creates httpx clients)
tools.py      →  20 @mcp.tool() functions with ToolAnnotations(readOnlyHint=True)
prompts.py    →  3 @mcp.prompt() templates (debug_build, compare_builds, check_change)
resources.py  →  3 @mcp.resource() templates (zuul://{tenant}/build|job|project/...)
helpers.py    →  AppContext dataclass, api() HTTP wrapper, parse_zuul_url(), utility functions
config.py     →  Config dataclass loaded from env vars (ZUUL_URL, ZUUL_DEFAULT_TENANT, etc.)
auth.py       →  Kerberos/SPNEGO authentication (drives OIDC redirect chain)
formatters.py →  Token-efficient response formatters (fmt_build, fmt_buildset, fmt_status_item)
errors.py     →  @handle_errors decorator wrapping all tools with uniform error→JSON handling
```

### Key Patterns

- **Two httpx clients**: `client` (API calls, has base_url + auth headers) and `log_client` (log file fetches from external hosts, no base_url). Both created in `server.py:lifespan`.
- **AppContext**: Injected via FastMCP lifespan, accessed in tools via `app(ctx)` helper.
- **Tenant resolution**: Every tool accepts optional `tenant` param; `helpers.tenant()` falls back to `ZUUL_DEFAULT_TENANT` env var.
- **URL-based input**: Build/buildset/change tools accept a `url` param as alternative to `uuid` + `tenant`. `parse_zuul_url()` extracts tenant and resource ID from Zuul web URLs.
- **`_resolve()`**: Shared helper in tools.py that resolves resource ID + tenant from either explicit params or URL.
- **`safepath()`**: URL path sanitization — preserves slashes for Zuul project names (e.g., `org/repo`) but blocks `..` traversal.
- **`clean()`**: Strips `None` values from dicts to minimize token usage in responses.
- **All tools return JSON strings**, never raw dicts. Errors also return JSON via `helpers.error()`.
- **ToolAnnotations**: All tools annotated with `readOnlyHint=True`, `destructiveHint=False`, `idempotentHint=True`, `openWorldHint=True`.

### Testing

Tests use `pytest-asyncio` (auto mode) with `respx` for HTTP mocking. The `conftest.py` provides:
- `mock_ctx` fixture: MagicMock MCP Context with real httpx clients wired to `AppContext`
- Factory functions: `make_build()`, `make_buildset()`, `make_status_item()`, `make_job_output_json()`

Tests mock HTTP at the `respx` level, not at the tool level — tools are called directly with the mock context.

### Config

Ruff: line-length 100, target Python 3.11, lint rules: E/W/F/I/UP/B/SIM/TCH/RUF.
Mypy: `check_untyped_defs = true`, `warn_return_any = false`.
CI tests against Python 3.11, 3.12, 3.13.
