# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

MCP server for Zuul CI — 36 tools (31 read-only + 4 write + 1 LogJuicer), 3 prompts, and 3 resources exposing builds, logs, pipelines, jobs, infrastructure, and live status via the Model Context Protocol. Published on PyPI as `mcp-zuul`. Supports stdio, SSE, and streamable-http transports.

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
__init__.py        →  imports tools, prompts, resources (registers decorators), exports main()
server.py          →  FastMCP instance ("zuul-ci"), lifespan (creates httpx clients), _BearerAuth
tools/             →  Package: 36 @mcp.tool() functions split by domain
  __init__.py      →  Re-exports for backward compat (tests import from mcp_zuul.tools)
  _common.py       →  Shared constants, _resolve(), _fetch_job_output(), re-exports from parsers
  _builds.py       →  6 build tools: list_builds, get_build, get_build_failures, diagnose_build, etc.
  _logs.py         →  3 log tools: get_build_log, browse_build_logs, tail_build_log
  _status.py       →  6 status/analytics: list_tenants, get_status, get_change_status, find_flaky_jobs, etc.
  _config.py       →  15 config/infra: list_jobs, get_job, get_project, list_nodes, get_freeze_job, etc.
  _write.py        →  4 write ops: enqueue, dequeue, autohold_create, autohold_delete
  _tests.py        →  1 test results: get_build_test_results + JUnit XML parsing
  _logjuicer.py    →  1 LogJuicer: get_build_anomalies
prompts.py         →  3 @mcp.prompt() templates (debug_build, compare_builds, check_change)
resources.py       →  3 @mcp.resource() templates (zuul://{tenant}/build|job|project/...)
helpers.py         →  AppContext dataclass, api() HTTP wrapper, parse_zuul_url(), utility functions
config.py          →  Config dataclass loaded from env vars (ZUUL_URL, MCP_TRANSPORT, ZUUL_ENABLED_TOOLS, etc.)
auth.py            →  Kerberos/SPNEGO authentication (drives OIDC redirect chain)
parsers.py         →  parse_playbooks(), smart_truncate(), extract_inner_recap(), extract_inner_failures(), extract_errors(), grep_log_context()
formatters.py      →  Token-efficient formatters (fmt_build, fmt_buildset, fmt_project, fmt_job_variants, etc.)
errors.py          →  @handle_errors decorator wrapping all tools with uniform error→JSON handling
classifier.py      →  Failure classification (INFRA_FLAKE, REAL_FAILURE, CONFIG_ERROR, UNKNOWN)
```

### Key Patterns

- **Two httpx clients**: `client` (API calls, has base_url + `_BearerAuth`) and `log_client` (log file fetches from external hosts, no base_url/auth). Both created in `server.py:lifespan`. `_pick_client()` selects based on log host vs API host.
- **Auth safety**: `_BearerAuth` (httpx.Auth subclass) ensures tokens are stripped on cross-origin redirects. An `asyncio.Lock` (`_auth_lock` on AppContext) serializes Kerberos re-auth to prevent concurrent session corruption.
- **Streaming with size caps**: `fetch_log_url()` streams up to 20 MB (prevents unbounded memory from large `job-output.json`). `stream_log()` streams up to 10 MB and returns `(bytes, truncated_bool)` so callers can warn users.
- **AppContext**: Injected via FastMCP lifespan, accessed in tools via `app(ctx)` helper. Holds both clients, config, and the auth lock.
- **Tenant resolution**: Every tool accepts optional `tenant` param; `helpers.tenant()` falls back to `ZUUL_DEFAULT_TENANT` env var.
- **URL-based input**: Build/buildset/change tools accept a `url` param as alternative to `uuid` + `tenant`. `parse_zuul_url()` extracts tenant and resource ID from Zuul web URLs. Supports both multi-tenant (`/t/<tenant>/build/...`) and single-tenant (`/build/...`) URL formats.
- **`_resolve()`**: Shared helper in tools.py that resolves resource ID + tenant from either explicit params or URL.
- **`safepath()`**: URL path sanitization — preserves slashes for Zuul project names (e.g., `org/repo`) but blocks `..` traversal.
- **`clean()`**: Strips `None` values from dicts to minimize token usage in responses.
- **All tools return JSON strings**, never raw dicts. Errors also return JSON via `helpers.error()`.
- **XML parsing**: Uses `defusedxml` (not stdlib `xml.etree.ElementTree`) for JUnit XML to prevent entity expansion attacks on untrusted test artifacts.
- **ToolAnnotations**: Read-only tools: `readOnlyHint=True`. Write tools: `readOnlyHint=False`, with `destructiveHint=True` for dequeue/autohold_delete.
- **Read-only mode**: `ZUUL_READ_ONLY=true` (default) removes write tools at startup. Set to `false` to enable enqueue/dequeue/autohold operations.
- **Transport**: Configurable via `MCP_TRANSPORT` env var — `stdio` (default), `sse`, or `streamable-http`. HTTP transport enables remote/shared deployment.
- **Tool filtering**: `ZUUL_ENABLED_TOOLS` or `ZUUL_DISABLED_TOOLS` (mutually exclusive) remove tools at startup via `ToolManager.remove_tool()`. Reduces LLM tool-selection noise.
- **LogJuicer**: Optional ML-based log anomaly detection via `LOGJUICER_URL`. Uses `log_client` (no auth headers) to avoid leaking Zuul tokens to external services.

### Testing

Tests use `pytest-asyncio` (auto mode) with `respx` for HTTP mocking. The `conftest.py` provides:
- `mock_ctx` fixture: MagicMock MCP Context with real httpx clients wired to `AppContext`
- Factory functions: `make_build()`, `make_buildset()`, `make_status_item()`, `make_job_output_json()`

Tests mock HTTP at the `respx` level, not at the tool level — tools are called directly with the mock context.

### Config

Ruff: line-length 100, target Python 3.11, lint rules: E/W/F/I/UP/B/SIM/TCH/RUF.
Mypy: `check_untyped_defs = true`, `warn_return_any = false`.
CI tests against Python 3.11, 3.12, 3.13.

### Release

Version lives in `pyproject.toml` and `server.json` (2 occurrences). Automated via `release.sh`.

```bash
./release.sh 0.5.0          # explicit version
./release.sh patch          # auto-bump (patch|minor|major)
make release V=minor        # via Makefile
```

The script runs: pre-flight checks → validate (test+lint+format+mypy) → bump versions → commit → push → tag (triggers Docker) → PyPI publish → GitHub Release (from CHANGELOG.md) → MCP Registry workflow. Requires CHANGELOG.md entry for the target version.

After release, update tool counts and version in CLAUDE.md if tools were added.
