# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.3] - 2026-03-23

### Changed
- **BREAKING**: `elapsed`, `remaining`, `enqueue_time` in `get_status` and `get_change_status` now in seconds (were milliseconds)
- **BREAKING**: Running jobs get fresh `elapsed`/`remaining` recomputed from `start_time` instead of Zuul's stale scheduler snapshot
- Jobs in `get_status` and `get_change_status` now include always-present `status` field: SUCCESS, FAILURE, RUNNING, WAITING, QUEUED
- Relative `stream_url` values are absolutified with the Zuul base URL in `get_change_status`

### Added
- `get_job_durations` tool — batch avg/min/max duration for multiple jobs in one call (new tool, 35→36 total)
- `elapsed_str`, `remaining_str` — human-readable duration strings ("1h 23m") per job in status responses
- `chain_summary` at the item level — pipeline progress counts, critical-path remaining time via dependency-graph walk
- Cycle detection in chain summary dependency traversal

### CI
- Supply chain scanning via `pip-audit` in lint job
- Dependabot auto-merge gated to patch/minor only (was ungated)
- Docker workflow runs tests + lint before building
- UV cache improvements (`cache-python: true`)
- Coverage XML export and markdown summary in CI

## [0.3.2] - 2026-03-22

### Security
- Auth token protection via `_BearerAuth` (httpx.Auth subclass) — prevents token leakage on cross-origin redirects
- Streaming size caps: `fetch_log_url` (20 MB), `stream_log` (10 MB) — prevents unbounded memory from large logs
- `defusedxml.ElementTree` for JUnit XML parsing — prevents entity expansion attacks
- `asyncio.Lock` serializes concurrent Kerberos re-auth — prevents session corruption
- Non-JSON response handling in `api()`, `api_post()`, `api_delete()` — clear errors on reverse proxy HTML responses
- Precise stream truncation — includes partial last chunk up to the exact size limit
- Guard against `gssapi ctx.step()` returning None token

### Added
- Default `limit=200` for `list_jobs` and `list_projects` — prevents unbounded LLM responses
- `asyncio.Semaphore(10)` for `list_buildsets` concurrent detail fetches
- Single-tenant Zuul URL support in `parse_zuul_url`
- `_parse_playbooks()` shared helper for failure analysis
- `_truncate_invocation()` helper with size cap for module args
- CONTRIBUTING.md, SECURITY.md, CHANGELOG.md
- Makefile with standard targets (test, lint, format, typecheck, check, build, clean)
- GitHub issue and PR templates
- Test coverage gate at 85% (currently 89%)
- `.coverage` in .gitignore

### Changed
- `.env.example` expanded with all 13 config variables

## [0.3.1] - 2026-03-22

### Added
- `diagnose_build` tool — one-call failure diagnosis combining structured failures with log context
- Grep dedup for context blocks in `get_build_log`
- Richer failure output with `cmd` and `invocation` fields

### Changed
- Compact passing playbook output in `get_build_failures` (phase/name/failed only)

## [0.3.0] - 2026-03-22

### Added
- `get_build_test_results` tool — JUnit XML test result parsing
- `get_build_anomalies` tool — LogJuicer ML-based log anomaly detection
- Write operations: `enqueue`, `dequeue`, `autohold_create`, `autohold_delete`
- `ZUUL_READ_ONLY` flag (default true) gates write tool availability

### Changed
- Improved error messages, flaky detection, and ref handling

## [0.2.1] - 2026-03-21

### Added
- `get_freeze_job` tool — resolved job config after inheritance
- Prompt enhancements with flaky signal detection
- Dependabot auto-merge workflow

### Fixed
- Project resource URI handling for slashes in project names
- Deduped log streaming logic

## [0.2.0] - 2026-03-21

### Added
- `get_freeze_jobs` — resolved job dependency graph
- `find_flaky_jobs` — flaky job detection with pass/fail statistics
- `tail_build_log` — fast log tail (last N lines)
- `list_nodes`, `list_labels`, `list_semaphores`, `list_autoholds` — infrastructure tools
- `get_connections`, `get_components` — system info tools
- `get_build_times` — build duration trends
- `get_tenant_info` — tenant capabilities
- MCP resources: `zuul://{tenant}/build|job|project/...`
- MCP prompts: `compare_builds`, `check_change`
- HTTP transport support (`MCP_TRANSPORT=sse|streamable-http`)
- Tool filtering (`ZUUL_ENABLED_TOOLS`, `ZUUL_DISABLED_TOOLS`)
- Kerberos/SPNEGO authentication

### Fixed
- Strip None values from resource output

## [0.1.1] - 2026-03-20

### Added
- Docker multi-platform image (amd64 + arm64)
- MCP registry publishing workflow
- Glama MCP score badge

### Fixed
- MCP registry schema compatibility

## [0.1.0] - 2026-03-20

### Added
- Initial release with 20 tools
- `list_builds`, `get_build`, `get_build_failures`, `get_build_log`, `browse_build_logs`
- `list_buildsets`, `get_buildset`
- `get_status`, `get_change_status`, `list_pipelines`
- `list_tenants`, `list_jobs`, `get_job`, `get_project`, `list_projects`
- `get_config_errors`
- `debug_build` prompt template
- URL-based input (`url` param as alternative to `uuid` + `tenant`)
- Kerberos/SPNEGO authentication support
- PyPI package: `mcp-zuul`

[0.3.3]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.3
[0.3.2]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.2
[0.3.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.1
[0.3.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.0
[0.2.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.2.1
[0.2.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.2.0
[0.1.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.1.1
[0.1.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.1.0
