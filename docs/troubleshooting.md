# Troubleshooting

## Installation

### `krb5-config: not found` or `Python.h: No such file`

The `gssapi` package has no pre-built Linux wheels and compiles from source. Install system packages first:

=== "Fedora/RHEL/CentOS"

    ```bash
    sudo dnf install krb5-devel python3-devel gcc
    ```

=== "Debian/Ubuntu"

    ```bash
    sudo apt install libkrb5-dev python3-dev gcc
    ```

macOS and Windows have pre-built wheels — no extra packages needed.

### `uvx: command not found` in Cursor or Claude Desktop

GUI-based MCP clients don't inherit your shell `PATH`. Find the full path:

```bash
which uvx    # e.g. /usr/bin/uvx or ~/.local/bin/uvx
```

Use that absolute path in your MCP config:

```json
"command": "/usr/bin/uvx"
```

### Permission errors on `~/.local/share/uv/`

If `uv` was previously run with `sudo`, the cache directory may be root-owned:

```bash
sudo chown -R $(whoami) ~/.local/share/uv/
```

## Authentication

### Kerberos ticket expired

Run `kinit` to renew your ticket. mcp-zuul re-authenticates automatically on the next API call.

```bash
kinit your-principal@REALM
```

### Bearer token not working

Ensure the token is set in the environment, not hardcoded in the config file:

```bash
export ZUUL_AUTH_TOKEN=<your-token>
```

Tokens in config files are visible in `ps` output.

## Runtime

### `diagnose_build` returns UNKNOWN

The classifier couldn't match error patterns from available data. The tool now includes a `reflection` field that shows what was checked and what remains uninvestigated:

```json
{
  "reflection": {
    "checked": [
      "job-output.json (structured playbook data)",
      "job-output.txt (fatal/FAILED grep)",
      "job-output.txt (broad error grep: Traceback/Exception/ERROR/timeout)"
    ],
    "unchecked": [
      "inner container/pod logs",
      "alternate log files (e.g. syslog, journal)"
    ],
    "broader_matches": 3,
    "reclassified": true
  }
}
```

Follow up with:

1. `browse_build_logs` to explore the log directory structure
2. `get_build_log` with `log_name` pointing to inner logs
3. `get_build_test_results` for JUnit XML test data

### Tool not found

If a tool isn't available, check:

1. **Write tools**: Disabled by default. Set `ZUUL_READ_ONLY=false`.
2. **Tool filtering**: `ZUUL_ENABLED_TOOLS` or `ZUUL_DISABLED_TOOLS` may be hiding it.
3. **Console streaming**: Requires `pip install mcp-zuul[console]`.
4. **LogJuicer**: Requires `LOGJUICER_URL` to be set.

### Large responses / token limits

Use these strategies to reduce token usage:

- `tail_build_log` instead of `get_build_log` for quick failure checks
- `diagnose_build(brief=True)` for classification + root cause only (~95% smaller)
- `ZUUL_ENABLED_TOOLS` to expose only the tools you need
- `list_builds(limit=5)` to cap result counts
