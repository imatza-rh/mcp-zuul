# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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

[0.3.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.1
[0.3.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.0
[0.2.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.2.1
[0.2.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.2.0
[0.1.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.1.1
[0.1.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.1.0
