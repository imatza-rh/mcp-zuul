# Getting Started

## Install

=== "uvx (recommended)"

    No install needed - runs directly:

    ```bash
    claude mcp add zuul \
                   -e ZUUL_URL=https://softwarefactory-project.io/zuul \
                   -e ZUUL_DEFAULT_TENANT=rdoproject.org \
                   -- uvx mcp-zuul
    ```

=== "pip"

    ```bash
    pip install mcp-zuul
    ```

=== "Docker"

    ```bash
    docker build -t mcp-zuul .
    ```

=== "LobeHub"

    Send this to your AI agent:

    ```
    Read https://lobehub.com/mcp/imatza-rh-mcp-zuul/skill.md and follow the instructions.
    ```

## Configure

All MCP clients use the same JSON structure:

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

| Client | Config file |
|--------|-------------|
| Claude Code | `~/.claude.json` |
| Claude Desktop | `claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` |

!!! tip
    GUI-based clients don't inherit your shell `PATH`. Use `which uvx` to find the full path and use that as `command`.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ZUUL_URL` | Yes | - | Zuul base URL |
| `ZUUL_DEFAULT_TENANT` | No | - | Default tenant name |
| `ZUUL_AUTH_TOKEN` | No | - | Bearer token for auth |
| `ZUUL_USE_KERBEROS` | No | `false` | Enable Kerberos/SPNEGO |
| `ZUUL_TIMEOUT` | No | `30` | HTTP timeout (seconds) |
| `ZUUL_VERIFY_SSL` | No | `true` | SSL verification |
| `MCP_TRANSPORT` | No | `stdio` | Transport mode |
| `ZUUL_ENABLED_TOOLS` | No | - | Comma-separated allowlist |
| `ZUUL_DISABLED_TOOLS` | No | - | Comma-separated blocklist |
| `ZUUL_READ_ONLY` | No | `true` | Set `false` for write ops |
| `LOGJUICER_URL` | No | - | LogJuicer endpoint |

## Multiple instances

Add separate entries per Zuul instance:

```json
{
  "mcpServers": {
    "zuul-rdo": {
      "command": "uvx", "args": ["mcp-zuul"],
      "env": {
        "ZUUL_URL": "https://softwarefactory-project.io/zuul",
        "ZUUL_DEFAULT_TENANT": "rdoproject.org"
      }
    },
    "zuul-internal": {
      "command": "mcp-zuul",
      "env": {
        "ZUUL_URL": "https://internal.example.com/zuul",
        "ZUUL_USE_KERBEROS": "true"
      }
    }
  }
}
```

## Usage examples

### Debug a build failure

```
"Why did the latest build of my-project fail?"
```

The LLM will call `list_builds` to find recent failures, then `diagnose_build` to get the structured root cause with task name, error, and return code.

### Search logs

```
"The structured data says 'non-zero return code' but no error detail.
 Check the ci_script logs."
```

Uses `browse_build_logs` to find the log file, then `get_build_log` with regex grep to find the exact error.

### Paste a URL

```
"What went wrong with this build?
 https://zuul.example.com/t/tenant/build/abc123def"
```

Tools auto-parse tenant and UUID from Zuul URLs.

### Check live status

```
"Is change 54321 in any pipeline?"
```

Returns live job progress with elapsed times and ETA, or the latest completed buildset.
