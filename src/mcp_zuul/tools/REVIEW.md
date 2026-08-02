# Tool Review Checklists

Per-module verification checklists for reviewing changes to tool submodules.

## All Tools

- [ ] `@mcp.tool` + `@handle_errors` decorators present
- [ ] Exported in `__init__.py` (import + `__all__`)
- [ ] Returns JSON string via `json.dumps()`
- [ ] `clean()` applied to response dicts
- [ ] Docstring matches actual behavior

## `_builds.py` — Build and Buildset Tools

- [ ] URL params validated via `_resolve()` → `_check_url_host()`
- [ ] Non-failure builds short-circuit (SUCCESS/SKIPPED)
- [ ] `log_url` checked before log fetch
- [ ] `_ref_meta()` used for change/project metadata
- [ ] Streaming caps: `stream_log` (10 MB), `fetch_log_url` (20 MB)
- [ ] `fmt_build` / `fmt_buildset` for response formatting

## `_logs.py` — Log Reading Tools

- [ ] `direct_log_url` validated via `_validate_log_url()`
- [ ] `log_url` from build validated via `_validate_log_url()`
- [ ] Grep patterns safe from ReDoS (pre-built match set + timeout)
- [ ] `max_lines` / `max_matches` capped to prevent unbounded responses
- [ ] `strip_ansi()` applied to text output
- [ ] Gzip fallback: retry with `Accept-Encoding: identity` on DecodingError

## `_status.py` — Status and Analytics Tools

- [ ] `get_change_status` URL parsing handles both multi-tenant and single-tenant formats
- [ ] Comma+SHA suffix stripped from status URLs
- [ ] Full-status fallback when `/status/change/N` returns empty
- [ ] `find_flaky_jobs`: infra failures tracked separately via `infra_failure_rate`
- [ ] `list_tenants` / `get_status`: no unbounded data in response

## `_config.py` — Configuration and Infrastructure Tools

- [ ] `list_jobs` / `list_projects` have `limit` parameter (default 200)
- [ ] `get_freeze_job` / `get_freeze_jobs`: `safepath()` on all URL segments
- [ ] Nodeset, semaphore data: `clean()` applied

## `_tests.py` — Test Results

- [ ] `defusedxml.ElementTree` used (not stdlib `xml.etree`)
- [ ] XML content capped at `_MAX_XML_BYTES`
- [ ] Concurrent fetches capped via `asyncio.Semaphore`

## `_write.py` — Write Operations

- [ ] Gated by `_READ_ONLY` check (removed when `ZUUL_READ_ONLY=true`)
- [ ] `annotations` include `read_only_hint=False`
- [ ] Destructive ops: `destructive_hint=True`

## `_logjuicer.py` — LogJuicer Anomaly Detection

- [ ] Uses `log_client` (no auth headers) to avoid token leakage
- [ ] Report ID sanitized against path traversal
- [ ] `LOGJUICER_URL` checked before making requests

## `_console.py` — Console Streaming

- [ ] JWT auth via WS message body (not HTTP headers)
- [ ] Kerberos: session cookies forwarded via `_cookie_header()`
- [ ] `ssl=True` for default context (websockets 16 rejects `ssl=None`)
- [ ] Line reassembly across chunk boundaries via `pending` buffer
- [ ] Re-auth on 401 via `_auth_lock` / `_auth_generation`
