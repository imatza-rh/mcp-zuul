# mcp-zuul

Debug build failures by asking questions, not clicking through web UIs.

**mcp-zuul** is an [MCP](https://modelcontextprotocol.io/) server for [Zuul CI](https://zuul-ci.org/). It exposes 47 tools, 3 prompts, and 3 resources covering builds, logs, pipelines, jobs, infrastructure, and live status.

Works with Claude Code, Claude Desktop, Cursor, Codex, Windsurf, and any MCP-compatible client.

## One command, no install

```bash
claude mcp add zuul -e ZUUL_URL=https://your-zuul.example.com -- uvx mcp-zuul
```

## Why mcp-zuul?

| | mcp-zuul | Raw Zuul API | Zuul web UI |
|---|---|---|---|
| **Failure analysis** | Structured — task, host, error, rc | Raw JSON, parse yourself | Click through log pages |
| **Log search** | Regex + context lines + line ranges | Not available | Browser Ctrl+F |
| **Flaky detection** | Automatic pass/fail statistics | Manual query + calculate | Not available |
| **Test results** | Parsed JUnit XML with failure details | Not available | External link |
| **Anomaly detection** | ML-based via LogJuicer | Not available | Not available |
| **Live status** | Job progress, ETA, pre-failure alerts | Polling API | Manual refresh |

## Quick links

- [Getting Started](getting-started.md) - Install and configure in 2 minutes
- [Tools Reference](tools.md) - All 47 tools with descriptions
- [Authentication](authentication.md) - Token, Kerberos/SPNEGO setup
- [Transport & Deployment](transport.md) - stdio, SSE, streamable-http
- [Troubleshooting](troubleshooting.md) - Common issues and fixes
