<!-- mcp-name: io.github.imatza-rh/mcp-zuul -->

# mcp-zuul

[![PyPI](https://img.shields.io/pypi/v/mcp-zuul)](https://pypi.org/project/mcp-zuul/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-zuul)](https://pypi.org/project/mcp-zuul/)
[![License](https://img.shields.io/github/license/imatza-rh/mcp-zuul)](https://github.com/imatza-rh/mcp-zuul/blob/main/LICENSE)
[![CI](https://github.com/imatza-rh/mcp-zuul/actions/workflows/ci.yml/badge.svg)](https://github.com/imatza-rh/mcp-zuul/actions/workflows/ci.yml)
[![MCP](https://glama.ai/mcp/servers/imatza-rh/mcp-zuul/badges/score.svg)](https://glama.ai/mcp/servers/imatza-rh/mcp-zuul)

An [MCP](https://modelcontextprotocol.io/) server for [Zuul CI](https://zuul-ci.org/). Debug build failures by asking questions, not clicking through web UIs.

34 tools (29 read-only + 4 write + 1 LogJuicer), 3 prompt templates, and 3 resources — covering builds, logs, pipelines, jobs, infrastructure, and live status. Supports stdio, SSE, and streamable-http transports. Works with Claude Code, Claude Desktop, Cursor, and any MCP-compatible client.

```
You:   "Why did the latest gate job fail?"
Claude: → get_build_failures(uuid="abc123")
        → get_build_log(uuid="abc123", log_name="controller/logs/ci_script_008_run.log",
                        grep="error|failed|timed out", context=2)

        Root cause: cert-manager pod in Completed state blocked oc wait.
        Confidence: Confirmed — verified in ci_script_008_run.log:325-329.
```

## Quick Start

**uvx** (no install, recommended):
```bash
claude mcp add zuul -- uvx mcp-zuul
```
Then set the required env var:
```bash
claude mcp add -e ZUUL_URL=https://softwarefactory-project.io/zuul \
               -e ZUUL_DEFAULT_TENANT=rdoproject.org \
               zuul -- uvx mcp-zuul
```

**pip**:
```bash
pip install mcp-zuul
```

**Docker**:
```bash
docker build -t mcp-zuul .
```

See [Setup](#setup) for full configuration options including Kerberos and multi-instance.

## Features

**Structured failure analysis** — `get_build_failures` parses Zuul's `job-output.json` and returns exactly which Ansible task failed, on which host, with error message, return code, and stderr. No log scrolling needed.

**Read any log file** — `get_build_log` isn't limited to `job-output.txt`. Pass `log_name` to read any file in the build's log directory (ci_script logs, ansible.log, deployment logs) with full grep, tail, and line-range support.

**Precise log navigation** — Jump to exact line ranges with `start_line`/`end_line`. After finding an error at line 6148, read lines 6130-6160 instead of scrolling through 200-line chunks.

**Smart grep** — Regex search with context lines. Auto-converts common shell-grep `\|` syntax to Python regex `|` so patterns like `error\|failed\|timeout` just work.

**Live pipeline awareness** — `get_change_status` returns live job progress with elapsed times, estimated completion, and pre-failure detection (`pre_fail` field). When the change isn't in pipeline, automatically fetches the latest completed buildset.

**Kerberos/SPNEGO auth** — First-class support for Zuul instances behind OIDC + Kerberos. Drives the full SPNEGO redirect chain automatically. Session cookies persist and re-authenticate transparently on expiry.

**URL-based input** — Paste a Zuul build URL directly. Tools auto-parse the tenant and UUID from URLs like `https://zuul.example.com/t/tenant/build/abc123` — no manual extraction needed.

**Flaky job detection** — `find_flaky_jobs` analyzes recent build history and computes pass/fail statistics to identify intermittent failures automatically.

**Job dependency graph** — `get_freeze_jobs` returns the fully-resolved job graph for a pipeline/project/branch, showing all jobs with their dependencies after inheritance resolution.

**Streamable HTTP transport** — Run as a persistent HTTP server with `MCP_TRANSPORT=streamable-http` for remote/shared deployment. Supports stdio (default), SSE, and streamable-http.

**Tool filtering** — Reduce LLM tool-selection noise with `ZUUL_ENABLED_TOOLS` or `ZUUL_DISABLED_TOOLS`. Only expose the tools your workflow needs.

**Write operations** — Enqueue/dequeue changes and manage autoholds. Disabled by default (`ZUUL_READ_ONLY=true`), write tools are removed from the server entirely so LLMs don't even see them until explicitly enabled.

**LogJuicer integration** — `get_build_anomalies` uses ML-based log analysis to find unusual lines by comparing failed logs against successful baselines. Optional — requires `LOGJUICER_URL`.

**Token-efficient output** — All responses strip None values and use compact formatters. `tail_build_log` returns just the last N lines — the fastest way to check why a build failed.

## Tools

### Builds & Failures

| Tool | What it does |
|------|-------------|
| `list_builds` | Search builds by project, pipeline, job, change, result. Includes `buildset_uuid` for cross-referencing. |
| `get_build` | Full build details — nodeset, log URL, artifacts, error detail. Accepts `url` or `uuid`. |
| `get_build_failures` | **Start here for failures.** Structured task-level data from `job-output.json` — failed play, task, host, msg, rc, stderr/stdout. Accepts `url` or `uuid`. |
| `get_build_log` | Read and search log files. Modes: `summary` (tail + error lines), `full` (paginated), `grep` (regex + context), `start_line`/`end_line` (exact range). Supports `log_name` for any file. Accepts `url` or `uuid`. |
| `tail_build_log` | **Fastest failure check.** Last N lines of a log (default 50, max 500). More token-efficient than `get_build_log` summary mode. Accepts `url` or `uuid`. |
| `browse_build_logs` | List log directory contents or fetch specific files (inventory, artifacts, must-gather). Max 512KB per file. Accepts `url` or `uuid`. |

### Buildsets

| Tool | What it does |
|------|-------------|
| `list_buildsets` | Search buildsets. Use `include_builds=true` to inline full build details (saves round-trips). |
| `get_buildset` | Full buildset with all builds and events. Accepts `url` or `uuid`. |

### Pipeline & Status

| Tool | What it does |
|------|-------------|
| `get_status` | Live pipeline status — what's queued, running, with job progress and ETA. Filterable by pipeline and project. |
| `get_change_status` | Status for a change/PR/MR. In pipeline: live jobs with elapsed times. Not in pipeline: auto-fetches latest completed buildset. Accepts `url` or `change`. |
| `list_pipelines` | All pipelines with their trigger types. |

### Jobs & Projects

| Tool | What it does |
|------|-------------|
| `list_tenants` | All tenants with project counts. |
| `list_jobs` | List jobs with optional name filter. |
| `get_job` | Job configuration — parent, nodeset, timeout, variants, source project. |
| `get_project` | Which pipelines and jobs are configured for a project. |
| `list_projects` | List all projects in a tenant with optional name filter. |
| `get_config_errors` | **Check this when jobs aren't running.** Configuration errors, missing refs, broken configs. Filterable by project. |
| `get_freeze_jobs` | Resolved job dependency graph for a pipeline/project/branch. Shows exactly which jobs will run with inheritance resolved. |
| `get_freeze_job` | **Resolved job config after inheritance.** Final merged nodeset, playbooks, variables, and timeout for a specific job. Answers "what will this job actually do?" |
| `find_flaky_jobs` | Analyze recent build history for intermittent failures. Computes pass/fail rate and flags jobs as flaky (>20% failure with mixed results). |
| `get_build_times` | Build duration trends with avg/min/max stats. Detect performance regressions or timeout-prone jobs. |
| `get_tenant_info` | Tenant capabilities — auth realms, job history support, websocket URL. |

### Infrastructure

| Tool | What it does |
|------|-------------|
| `list_nodes` | Nodepool nodes with state (ready, in-use, building), provider, and label. Includes state summary. |
| `list_labels` | Available nodepool labels — what node types jobs can request. |
| `list_semaphores` | Resource locks with current holders and max capacity. Check when jobs wait unexpectedly. |
| `list_autoholds` | Active autohold requests — nodes held after failure for debugging. |
| `get_connections` | Configured source connections — Gerrit, GitHub, GitLab instances with driver and hostname. |
| `get_components` | System components — schedulers, executors, mergers, web servers with state and version. |

### Write Operations

Disabled by default (`ZUUL_READ_ONLY=true`). Set `ZUUL_READ_ONLY=false` to enable. Requires auth token or Kerberos.

| Tool | What it does |
|------|-------------|
| `enqueue` | Enqueue a change or ref into a pipeline for testing. |
| `dequeue` | Remove a change or ref from a pipeline. **Destructive.** |
| `autohold_create` | Create an autohold request — hold nodes after failure for debugging. |
| `autohold_delete` | Delete an autohold request. **Destructive.** |

### Test Results & Log Analysis

| Tool | What it does |
|------|-------------|
| `get_build_test_results` | **Parse JUnit XML test results.** Discovers test files via `zuul-manifest.json`, returns structured pass/fail/skip counts with failure details. Works with tempest, tobiko, and any JUnit XML output. |
| `get_build_anomalies` | ML-based log anomaly detection via [LogJuicer](https://github.com/logjuicer/logjuicer). Compares failed logs against successful baselines. Requires `LOGJUICER_URL`. |

## Prompts

Pre-built prompt templates that pre-load context and guide analysis:

| Prompt | What it does |
|--------|-------------|
| `debug_build` | Fetches build details + structured failures, checks for flaky signal from recent history, then guides root cause analysis. |
| `compare_builds` | Loads two builds side-by-side with inline failure data for differential analysis — "why did this start failing?" |
| `check_change` | Determines live pipeline status or latest results for a change, with appropriate next steps. |

## Resources

Browsable context that clients can attach to conversations without tool calls:

| Resource | URI Pattern |
|----------|-------------|
| Build details | `zuul://{tenant}/build/{uuid}` |
| Job configuration | `zuul://{tenant}/job/{name}` |
| Project configuration | `zuul://{tenant}/project/{org}/{repo}` |

## Setup

### MCP client configuration

All clients use the same JSON structure. Add to your client's MCP config file:

**Claude Code** (`~/.claude.json` → `mcpServers`):
```json
{
  "mcpServers": {
    "zuul": {
      "command": "uvx",
      "args": ["mcp-zuul"],
      "env": {
        "ZUUL_URL": "https://softwarefactory-project.io/zuul",
        "ZUUL_DEFAULT_TENANT": "rdoproject.org"
      }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** (`.cursor/mcp.json`), and other MCP clients use the same format.

Or via CLI:
```bash
claude mcp add -e ZUUL_URL=https://softwarefactory-project.io/zuul \
               -e ZUUL_DEFAULT_TENANT=rdoproject.org \
               zuul -- uvx mcp-zuul
```

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ZUUL_URL` | Yes | — | Zuul base URL (e.g. `https://softwarefactory-project.io/zuul`) |
| `ZUUL_DEFAULT_TENANT` | No | — | Default tenant (saves passing `tenant` on every call) |
| `ZUUL_AUTH_TOKEN` | No | — | Bearer token for authenticated instances |
| `ZUUL_USE_KERBEROS` | No | `false` | Enable Kerberos/SPNEGO authentication |
| `ZUUL_TIMEOUT` | No | `30` | HTTP timeout in seconds |
| `ZUUL_VERIFY_SSL` | No | `true` | SSL certificate verification |
| `MCP_TRANSPORT` | No | `stdio` | Transport: `stdio`, `sse`, or `streamable-http` |
| `MCP_HOST` | No | `127.0.0.1` | HTTP server bind address (non-stdio transports) |
| `MCP_PORT` | No | `8000` | HTTP server port (non-stdio transports) |
| `ZUUL_ENABLED_TOOLS` | No | — | Comma-separated list of tools to enable (disables all others) |
| `ZUUL_DISABLED_TOOLS` | No | — | Comma-separated list of tools to disable (mutually exclusive with above) |
| `ZUUL_READ_ONLY` | No | `true` | Set to `false` to enable write operations (enqueue, dequeue, autohold) |
| `LOGJUICER_URL` | No | — | LogJuicer base URL for ML-based log anomaly detection |

### Token authentication

Pass `ZUUL_AUTH_TOKEN` via host environment — **never hardcode tokens in config files** (visible in `ps` output):

```bash
export ZUUL_AUTH_TOKEN=<your-token>
```

For Docker, forward without a value to inherit from host:
```json
"args": ["run", "-i", "--rm", "-e", "ZUUL_AUTH_TOKEN", "mcp-zuul"]
```

### Kerberos / SPNEGO

For Zuul behind OIDC + Kerberos. Requires a valid Kerberos ticket (`kinit`) and the `gssapi` package:

```bash
pip install mcp-zuul[kerberos]    # or: uvx --with "mcp-zuul[kerberos]" mcp-zuul
```

```json
{
  "zuul-internal": {
    "command": "mcp-zuul",
    "env": {
      "ZUUL_URL": "https://internal-zuul.example.com/zuul",
      "ZUUL_USE_KERBEROS": "true",
      "ZUUL_VERIFY_SSL": "false"
    }
  }
}
```

For Docker, mount the Kerberos ticket cache:
```bash
docker run -i --rm \
  -v /etc/krb5.conf:/etc/krb5.conf:ro \
  -v /tmp/krb5cc_$(id -u):/tmp/krb5cc_$(id -u):ro \
  -e KRB5CCNAME=/tmp/krb5cc_$(id -u) \
  -e ZUUL_URL=https://internal-zuul.example.com/zuul \
  -e ZUUL_USE_KERBEROS=true \
  mcp-zuul
```

### Multiple instances

Add separate entries per Zuul instance:
```json
{
  "mcpServers": {
    "zuul-rdo": {
      "command": "uvx", "args": ["mcp-zuul"],
      "env": { "ZUUL_URL": "https://softwarefactory-project.io/zuul", "ZUUL_DEFAULT_TENANT": "rdoproject.org" }
    },
    "zuul-internal": {
      "command": "mcp-zuul",
      "env": { "ZUUL_URL": "https://internal.example.com/zuul", "ZUUL_USE_KERBEROS": "true" }
    }
  }
}
```

## Usage Examples

### Debug a build failure

```
"Why did the latest build of my-project fail?"
```
→ `list_builds(project="my-project", result="FAILURE", limit=1)` → `get_build_failures(uuid="...")` → root cause with task name, error, and return code.

### Deep-dive into logs

```
"The structured data says 'non-zero return code' but no error detail.
 Check the ci_script logs."
```
→ `browse_build_logs(uuid="...", path="controller/ci-framework-data/logs/")` → finds `ci_script_008_run.log` → `get_build_log(uuid="...", log_name="controller/ci-framework-data/logs/ci_script_008_run.log", grep="error|timed out|Error 1", context=2)` → exact error with surrounding context.

### Navigate to a specific error

```
"Show me lines 6478-6484 of the job output"
```
→ `get_build_log(uuid="...", start_line=6478, end_line=6484)` → exactly those 7 lines.

### Check live pipeline status

```
"Is change 54321 in any pipeline?"
```
→ `get_change_status(change="54321")` → live jobs with elapsed times and ETA, or latest completed buildset if not in pipeline.

### Compare build results across a pipeline

```
"Show me all builds from the latest buildset"
```
→ `list_builds` to get `buildset_uuid` → `get_buildset(uuid="...")` → all sibling builds with results and durations.

### Paste a Zuul URL directly

```
"What went wrong with this build?
 https://zuul.example.com/t/tenant/build/abc123def"
```
→ `get_build_failures(url="https://zuul.example.com/t/tenant/build/abc123def")` → tenant and UUID auto-extracted.

### Debug why a job isn't running

```
"My project's check pipeline seems broken — jobs aren't triggering"
```
→ `get_config_errors(project="org/my-project")` → configuration errors, missing refs, or repo access issues.

### Check node availability

```
"Jobs are stuck in queue — are there nodes available?"
```
→ `list_nodes()` → node states with by_state summary → `list_labels()` → available node types.

### Detect flaky jobs

```
"Is this job flaky? It keeps failing intermittently"
```
→ `find_flaky_jobs(job_name="my-deploy-job", limit=30)` → pass/fail stats, failure rate, flaky=true/false.

### See what jobs run for a project

```
"What jobs are configured for openstack-operator in the check pipeline?"
```
→ `get_freeze_jobs(pipeline="check", project="openstack-k8s-operators/openstack-operator")` → resolved job graph with dependencies.

### Quick log tail

```
"Show me the last 30 lines of the build log"
```
→ `tail_build_log(uuid="...", lines=30)` → just the tail, minimal tokens.

### What nodeset does my job use after inheritance?

```
"What nodeset and playbooks will deploy-job actually use?"
```
→ `get_freeze_job(pipeline="check", project="org/repo", job_name="deploy-job")` → resolved nodeset, playbooks, variables, timeout after all parent inheritance.

## Development

```bash
git clone https://github.com/imatza-rh/mcp-zuul.git
cd mcp-zuul
uv sync --extra dev

# Run locally
ZUUL_URL=https://softwarefactory-project.io/zuul uv run mcp-zuul

# Run tests
uv run pytest tests/ -v

# Lint and format
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type check
uv run mypy src/mcp_zuul/

# Build Docker image
docker build -t mcp-zuul .
```

**Architecture:** Multi-module package in `src/mcp_zuul/` — `config.py` (env vars, transport, tool filtering, read-only mode), `auth.py` (Kerberos/SPNEGO), `server.py` (FastMCP + lifespan + tool filtering + write-tool gating), `helpers.py` (API client with GET/POST/DELETE, URL parsing, log streaming), `formatters.py` (token-efficient output), `errors.py` (uniform error handling), `tools.py` (34 tools), `prompts.py` (3 prompts), `resources.py` (3 resources). See `CLAUDE.md` for full architecture description.

## Contributing

Contributions welcome. Please open an issue first to discuss significant changes.

```bash
# Fork, clone, and install dev dependencies
uv sync --extra dev

# Make changes, then verify
uv run pytest tests/ -v
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/mcp_zuul/
```

## License

Apache-2.0
