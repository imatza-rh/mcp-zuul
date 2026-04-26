# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.6.0] - 2026-04-26

### Added
- New tool: `stream_build_console` — live console output from RUNNING builds via Zuul's WebSocket console-stream endpoint. Connects to the WebSocket, buffers for N seconds (default 10, max 30), and returns the last M lines (default 100, max 500) using a circular buffer for tail behavior. Accepts `uuid` or `url` parameter like other build tools
- New optional extra: `[console]` installs `websockets>=14.0` for live streaming support. Users who don't need live streaming skip the dependency entirely. The tool returns a clear install message when called without the dependency
- `_no_log_url_error` now suggests `stream_build_console` when log tools are called on IN_PROGRESS builds, guiding users to the right tool for running jobs

### Highlights
- **Protocol**: Auth via JWT token in the WebSocket message body (not HTTP headers), verified against upstream Zuul source. Supports both authenticated and public tenants
- **Chunk reassembly**: Zuul streams raw 4KB chunks from the executor's finger protocol that can split mid-line. A pending-line buffer reassembles lines across chunk boundaries to prevent garbled output
- **SSL**: Handles both `wss://` (with configurable certificate verification via `ZUUL_VERIFY_SSL`) and `ws://` connections
- **Error handling**: Specific messages for auth failure (403), completed build (404), validation error (close code 4000), streaming error (4011), connection timeout, and connection refused

## [0.5.1] - 2026-03-29

### Added
- `fmt_build` computes human-readable `elapsed` field for IN_PROGRESS builds in non-brief mode, eliminating manual UTC arithmetic when monitoring running builds via `get_change_status`
- Connection error messages now classify the underlying cause (DNS resolution failed, connection refused, network unreachable) instead of generic "Cannot connect to Zuul API"

### Fixed
- `get_build_log` with negative `lines` parameter no longer produces empty/wrong output - values are clamped to valid range
- File-level corrupted gzip in log tools now shows the same helpful "use diagnose_build" message as HTTP-level gzip errors
- Removed dead exception types (`gzip.BadGzipFile`, `zlib.error`, `EOFError`, `OSError`) from `_fetch_job_output` catch block - these are now handled inside `_decompress_gzip`

## [0.5.0] - 2026-03-28

### Added
- `browse_build_logs` accepts optional `max_lines` parameter to limit file output with pagination (`total_lines`, `has_more`)
- `get_build_log` and `tail_build_log` automatically detect gzip content and retry with `.gz` suffix on 404
- Log-not-found errors now include a directory listing of available files, saving round-trips to `browse_build_logs`
- `get_change_status` enriches not-in-pipeline builds with `report_url` and `status_hint` when the buildset is still in progress
- Release pre-flight checks now require a CHANGELOG.md entry for the target version

### Fixed
- `browse_build_logs` returned binary garbage on `.gz` files instead of decompressed text
- `fmt_buildset(brief=False)` always called `fmt_build(brief=True)`, stripping `log_url`, `start_time`, `end_time`, `ref_url`, and `nodeset` from builds
- `list_buildsets(include_builds=True)` internal cap of 10 results set `has_more=False` even when more data existed
- `_extract_file_paths` now scans `extracted_errors` and `inner_failures` fields, catching file paths hidden in truncated output

### Changed
- Unified gzip decompression in `_fetch_job_output` to use shared `_decompress_gzip()` helper

### Security
- Release script hides PyPI token from bash trace output

## [0.4.2] - 2026-03-27

### Added
- Release automation script (`release.sh`) - single command for the full release pipeline: version bump, validation, commit, tag, PyPI publish, GitHub Release, and MCP Registry
- `Makefile` target: `make release V=patch|minor|major|X.Y.Z`
- `extract_errors()` scans full stdout/stderr for error patterns BEFORE `smart_truncate` discards the middle section, preserving root causes in a new `extracted_errors` field on failed tasks
- `extract_inner_failures()` parses nested Ansible `fatal:` blocks from container_exec output, extracting task name, host, msg, rc, cmd, and stderr_excerpt into a structured `inner_failures` field
- Classifier now scans `inner_failures` and `extracted_errors` fields for pattern matching, so infra errors hidden inside nested playbook output are correctly classified

### Fixed
- `stream_log` retries with `Accept-Encoding: identity` on corrupted gzip (`DecodingError`), matching existing `fetch_log_url` behavior
- UNREACHABLE classifier false positive - pattern matched `unreachable=0` in PLAY RECAP lines; changed to match `UNREACHABLE!` only
- `extract_errors()` now scans both stdout and stderr (was silently dropping stderr when stdout had matches)
- `_collect_error_text` size cap now applied to `inner_failures` and `extracted_errors` loops (was unbounded)
- Corrupted gzip error message now recommends `diagnose_build` instead of `get_build_log` (which hits the same corrupted file)

## [0.4.1] - 2026-03-26

### Security
- URL-decode `log_name` and `path` parameters before path traversal check - percent-encoded sequences (`%2e%2e/%2f`) can no longer bypass `..` detection
- Reject user-supplied regex patterns with nested quantifiers (e.g. `(a+)+`) before compilation to prevent ReDoS thread consumption
- CI: ignore CVE-2026-4539 (pygments ReDoS, CVSS 3.3 Low, transitive dev dep) with staleness guard that forces re-evaluation on update

### Added
- `get_build_failures` and `diagnose_build` now surface `ref_url`, `project`, `change`, and `files_in_failure` (repo-relative file paths extracted from failure output) to help cross-reference failing files against the change's file list

### Fixed
- `get_change_status` handles 404 from `/status/change/` endpoint (some Zuul instances return 404 instead of `[]` for changes not in pipeline) - previously killed the call before fallback logic could run
- SSL certificate errors detected at startup with actionable suggestion (`ZUUL_VERIFY_SSL=false`) instead of raw tracebacks
- Kerberos setup: added Linux prerequisites, CLI setup form, GUI client PATH note, and troubleshooting section to README
- `isinstance` type guard for refs elements in `fmt_status_item` and `get_change_status` - prevents `AttributeError` on non-dict refs from Zuul API
- Removed spurious `KeyError` from `_fetch_job_output` exception list

## [0.4.0] - 2026-03-24

### Security
- Auth generation counter prevents thundering-herd Kerberos re-auth under concurrent tool calls
- Streaming deadline (5 min) caps total log transfer time independently of per-chunk progress
- Grep context blocks now truncate lines to 1000 chars before regex matching (consistent with executor), preventing ReDoS on the main asyncio thread
- LogJuicer report ID sanitized against path traversal before URL construction

### Changed
- **BREAKING**: `clean()` now strips empty strings (`""`) and empty lists (`[]`) in addition to `None` — reduces token output but removes previously-present keys with empty values from JSON responses
- **BREAKING**: `elapsed`, `remaining`, `estimated` in status responses are now human-readable strings (`"2m 30s"`) instead of raw seconds; `elapsed_str`/`remaining_str` removed (redundant)
- **BREAKING**: `voting` field omitted from builds and jobs when `True` (default) — only emitted when `False`. Callers checking `build["voting"]` must use `.get("voting", True)`
- **BREAKING**: `buildset_uuid`, `log_url`, `start_time`, `ref_url` moved to non-brief output in `fmt_build` — `list_builds` no longer includes these fields
- `chain_summary.critical_path_remaining` replaced by `chain_summary.cp_eta` (human-readable string)
- Removed product-specific references from classifier (generic Zuul CI patterns only)

### Performance
- Token output reduced ~50% on `list_builds`, ~30% on `get_status` via conditional field inclusion
- `grep_log_context` uses single-pass regex with cached match indices (O(n) instead of O(n×m))
- `parse_playbooks` strips ANSI once per field, reuses for truncate + recap extraction
- Thread pool executor for user-supplied grep patterns with 10s timeout
- `get_change_status` retries digit-only changes with `refs/merge-requests/N/head` format before buildset lookup (replaces O(n) full-status scan)
- `diagnose_build` fetches job-output.json and job-output.txt in parallel via `asyncio.gather`
- `get_build_test_results` probes fallback paths and fetches XML files in parallel (Semaphore(5))
- Streaming uses per-request `httpx.Timeout(read=300s)` so 5-minute deadline is reachable (client-level 30s was killing large log downloads)

### Fixed
- Gzip decompression in `_fetch_job_output` detects gzip magic bytes (0x1f 0x8b) and uses incremental `zlib.decompressobj` with size cap to prevent gzip bombs
- Gzip fallback in `_fetch_job_output` now catches `gzip.BadGzipFile`, `zlib.error`, `EOFError`, `OSError`, `UnicodeDecodeError`
- `get_change_status` best-effort buildset fallback now catches `TimeoutException` and `ValueError` (was silently dropping the "not_in_pipeline" response on slow APIs)
- `_compute_chain_summary` handles dict-style dependencies (`{"name": "x"}`) and nameless jobs
- `_format_duration` handles `inf`, `nan`, and negative values without crashing
- `fmt_project` handles list-type jobs where first element is not a dict
- `_truncate_invocation` handles dict/list values and avoids dict mutation during iteration
- `parse_playbooks` caps failed tasks at 50 and guards against non-dict host results
- Defensive `.get()` throughout formatters and config tools (prevents KeyError on unexpected API data)

## [0.3.4] - 2026-03-24

### Added
- Failure classifier (`classifier.py`) — categorizes build failures as INFRA_FLAKE, REAL_FAILURE, CONFIG_ERROR, or UNKNOWN with confidence levels and retryability flags
- `diagnose_build` tool — structured failure analysis combining job-output.json parsing, log grep, and classification
- `get_build_test_results` tool — JUnit XML test result extraction from build artifacts
- `get_build_anomalies` tool — ML-based log anomaly detection via LogJuicer
- `parsers.py` module — extracted `parse_playbooks()`, `smart_truncate()`, `extract_inner_recap()`, `grep_log_context()` for shared use across tools and classifier
- Smart stdout truncation with ANSI stripping in job-output.json parsing

### Changed
- Split monolithic `tools.py` into `tools/` package with domain-specific modules (`_builds`, `_logs`, `_status`, `_config`, `_write`, `_tests`, `_logjuicer`)
- `Config` refactored to use `from_env()` classmethod (raises instead of sys.exit)
- Gzip fallback uses suffix loop over `.json.gz` → `.json` with uniform error handling

### Fixed
- `parse_playbooks()` crashes on null stats values from Zuul API (AttributeError on `.get()`)
- Deduplicated `_RUN_END_MARKER` constant (was defined in both `_common.py` and `_logs.py`)
- Replaced `__import__("re")` idiom with normal import in `_common.py`
- Gzip `DecodingError` fallback now tries uncompressed JSON before text grep
- `_no_log_url_error` used consistently across all log tools

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

[0.4.2]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.4.2
[0.4.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.4.1
[0.4.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.4.0
[0.3.4]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.4
[0.3.3]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.3
[0.3.2]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.2
[0.3.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.1
[0.3.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.3.0
[0.2.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.2.1
[0.2.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.2.0
[0.1.1]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.1.1
[0.1.0]: https://github.com/imatza-rh/mcp-zuul/releases/tag/v0.1.0
