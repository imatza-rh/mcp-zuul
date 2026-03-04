# mcp-zuul

MCP server for [Zuul CI](https://zuul-ci.org/) — read-only access to builds, logs, status, and jobs.

Works with any Zuul instance (Software Factory, OpenDev, etc.) via the [Zuul REST API](https://zuul-ci.org/docs/zuul/latest/rest-api.html).

## Tools

| Tool | Description |
|------|-------------|
| `list_tenants` | List tenants with project counts |
| `get_status` | Live pipeline status (filtered to active items) |
| `get_change_status` | Status for a specific Gerrit change or GitHub PR |
| `list_builds` | Search builds by project, job, result, change, etc. |
| `get_build` | Full build details |
| `get_build_log` | Fetch + parse logs (summary/full/grep modes) |
| `list_buildsets` | Search buildsets |
| `get_buildset` | Buildset with all builds and events |
| `list_jobs` | List/filter jobs |
| `get_job` | Job config and variants |
| `get_project` | Project pipeline and job config |
| `list_pipelines` | Pipelines with trigger types |

## Configuration

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `ZUUL_URL` | Yes | Zuul base URL (e.g. `https://softwarefactory-project.io/zuul`) |
| `ZUUL_DEFAULT_TENANT` | No | Default tenant name (e.g. `rdoproject.org`) |
| `ZUUL_AUTH_TOKEN` | No | Bearer token for authenticated instances |
| `ZUUL_TIMEOUT` | No | HTTP timeout in seconds (default: 30) |
| `ZUUL_VERIFY_SSL` | No | SSL verification (default: `true`) |

## Setup

### Docker (recommended)

Build:
```bash
docker build -t mcp-zuul .
```

Add to Claude Code, Claude Desktop, or any MCP client:

```json
{
  "mcpServers": {
    "zuul": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
        "-e", "ZUUL_URL=https://softwarefactory-project.io/zuul",
        "-e", "ZUUL_DEFAULT_TENANT=rdoproject.org",
        "mcp-zuul"
      ]
    }
  }
}
```

### uvx (no install needed)

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

### pip install

```bash
pip install mcp-zuul
```

```json
{
  "mcpServers": {
    "zuul": {
      "command": "mcp-zuul",
      "env": {
        "ZUUL_URL": "https://softwarefactory-project.io/zuul",
        "ZUUL_DEFAULT_TENANT": "rdoproject.org"
      }
    }
  }
}
```

### Claude Code CLI

```bash
claude mcp add -t stdio \
  -e ZUUL_URL=https://softwarefactory-project.io/zuul \
  -e ZUUL_DEFAULT_TENANT=rdoproject.org \
  zuul -- mcp-zuul
```

### Multiple Zuul instances

Configure separate MCP server entries for each instance:

```json
{
  "mcpServers": {
    "zuul-rdo": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
        "-e", "ZUUL_URL=https://softwarefactory-project.io/zuul",
        "-e", "ZUUL_DEFAULT_TENANT=rdoproject.org",
        "mcp-zuul"
      ]
    },
    "zuul-internal": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
        "-e", "ZUUL_URL=https://my-internal-zuul.example.com/zuul",
        "-e", "ZUUL_DEFAULT_TENANT=my-tenant",
        "-e", "ZUUL_AUTH_TOKEN=my-token",
        "mcp-zuul"
      ]
    }
  }
}
```

## Log analysis

The `get_build_log` tool has three modes:

- **summary** (default): Last 100 lines + all ERROR/FAILURE/UNREACHABLE lines from the full log
- **full**: Paginated 200-line chunks with offset
- **grep**: Regex filter returning matching lines with line numbers

```
"Show me the last 5 failed builds"
"Get the log for build <uuid> and find the error"
"Grep the log for 'UNREACHABLE' or 'timeout'"
```

## Development

```bash
git clone https://github.com/imatza-rh/mcp-zuul.git
cd mcp-zuul
uv sync
ZUUL_URL=https://softwarefactory-project.io/zuul uv run mcp-zuul
```

## License

Apache-2.0
