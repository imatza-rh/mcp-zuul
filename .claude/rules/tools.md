---
globs: src/mcp_zuul/tools/**
---
MCP tool functions. Patterns:
- Tools accept `url` param as alternative to `uuid` + `tenant`
- Use `_resolve()` from _common.py for tenant/UUID resolution
- **URL validation**: ANY parameter that accepts a URL from the caller MUST be validated. This includes `url`, `direct_log_url`, and any future `*_url` params. Routes: `_resolve()` calls `_check_url_host()` for build/buildset URLs. `_validate_log_url()` validates log URLs (scheme + hostname). When adding URL params, grep for `: str = ""` across all tool files and verify every URL-accepting path is covered. (2026-07-27 incident: `direct_log_url` bypassed all validation, creating SSRF.)
- Streaming responses capped at 20 MB (job-output.json) / 10 MB (logs)
- Auth safety: never pass credentials in redirects (_BearerAuth handles this)
- Read-only mode (ZUUL_READ_ONLY=true) removes write tools at startup
- Use @handle_errors decorator on all tools
- **New tool checklist** (RENDERED GATE before committing a new tool):
  ```
  New tool: [name]
  - [ ] @mcp.tool + @handle_errors decorators
  - [ ] Export in __init__.py (import + __all__)
  - [ ] Tool count updated: __init__.py docstring, CLAUDE.md, README.md (2 places)
  - [ ] Docstring matches actual behavior (no aspirational claims)
  - [ ] Response fields sanitized (use _sanitize_url for URLs, no raw config values with potential credentials)
  - [ ] Error paths log exception details (not just "failed")
  - [ ] Test covers: success, auth error, unreachable, edge cases
  - [ ] `test_tool_counts_match` passes
  ```
- **Modified tool checklist** (RENDERED GATE before committing new params on existing tools):
  ```
  Modified tool: [name] param: [param_name]
  - [ ] If param accepts a URL: validated via _validate_log_url() or _check_url_host()
  - [ ] If param changes default behavior: existing tests updated OR new tests added
  - [ ] If param affects response shape: test_response_sizes.py updated
  - [ ] If param threads through to a formatter: grep ALL call sites of that formatter — every caller must pass the param (2026-08-05: get_change_status had brief param but only wired it on the not-in-pipeline path, missing the in-pipeline fmt_status_item call)
  - [ ] Docstring updated with new param
  ```
