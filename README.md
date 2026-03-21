<!-- mcp-name: io.github.imatza-rh/mcp-zuul -->

# mcp-zuul

[![PyPI](https://img.shields.io/pypi/v/mcp-zuul)](https://pypi.org/project/mcp-zuul/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-zuul)](https://pypi.org/project/mcp-zuul/)
[![License](https://img.shields.io/github/license/imatza-rh/mcp-zuul)](https://github.com/imatza-rh/mcp-zuul/blob/main/LICENSE)
[![CI](https://github.com/imatza-rh/mcp-zuul/actions/workflows/ci.yml/badge.svg)](https://github.com/imatza-rh/mcp-zuul/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io/) server for [Zuul CI](https://zuul-ci.org/). Debug build failures by asking questions, not clicking through web UIs.

Read-only access to any Zuul instance — builds, logs, pipelines, jobs, and live status. Works with Claude Code, Claude Desktop, Cursor, and any MCP-compatible client.

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

**Token-efficient output** — All responses strip None values and use compact formatters. Designed for AI context windows, not human eyeballs.

## Tools

### Builds & Failures

| Tool | What it does |
|------|-------------|
| `list_builds` | Search builds by project, pipeline, job, change, result. Includes `buildset_uuid` for cross-referencing. |
| `get_build` | Full build details — nodeset, log URL, artifacts, error detail. |
| `get_build_failures` | **Start here for failures.** Structured task-level data from `job-output.json` — failed play, task, host, msg, rc, stderr/stdout. |
| `get_build_log` | Read and search log files. Modes: `summary` (tail + error lines), `full` (paginated), `grep` (regex + context), `start_line`/`end_line` (exact range). Supports `log_name` for any file. |
| `browse_build_logs` | List log directory contents or fetch specific files (inventory, artifacts, must-gather). Max 512KB per file. |

### Buildsets

| Tool | What it does |
|------|-------------|
| `list_buildsets` | Search buildsets. Use `include_builds=true` to inline full build details (saves round-trips). |
| `get_buildset` | Full buildset with all builds and events. Takes a **buildset UUID**, not a build UUID. |

### Pipeline & Status

| Tool | What it does |
|------|-------------|
| `get_status` | Live pipeline status — what's queued, running, with job progress and ETA. Filterable by pipeline and project. |
| `get_change_status` | Status for a change/PR/MR. In pipeline: live jobs with elapsed times. Not in pipeline: auto-fetches latest completed buildset. |
| `list_pipelines` | All pipelines with their trigger types. |

### Jobs & Projects

| Tool | What it does |
|------|-------------|
| `list_tenants` | All tenants with project counts. |
| `list_jobs` | List jobs with optional name filter. |
| `get_job` | Job configuration — parent, nodeset, timeout, variants, source project. |
| `get_project` | Which pipelines and jobs are configured for a project. |

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

**Architecture:** Multi-module package in `src/mcp_zuul/` — `config.py` (env vars), `auth.py` (Kerberos/SPNEGO), `server.py` (FastMCP + lifespan), `helpers.py` (API client, utilities), `formatters.py` (token-efficient output), `errors.py` (uniform error handling), `tools.py` (14 tools). See `CLAUDE.md` for full architecture description.

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
