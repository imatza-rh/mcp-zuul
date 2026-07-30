# Transport & Deployment

mcp-zuul supports three MCP transport modes, configured via the `MCP_TRANSPORT` environment variable.

## stdio (default)

Standard input/output. The client launches the server as a subprocess. This is the default and works with all MCP clients.

```bash
claude mcp add zuul -e ZUUL_URL=https://example.com/zuul -- uvx mcp-zuul
```

## SSE (Server-Sent Events)

Run as a persistent HTTP server. Useful for shared or remote deployment.

```bash
MCP_TRANSPORT=sse MCP_PORT=8000 uvx mcp-zuul
```

Then configure the client to connect via HTTP:

```json
{
  "zuul": {
    "url": "http://localhost:8000/sse"
  }
}
```

## Streamable HTTP

The latest MCP transport. Supports bidirectional communication over HTTP with streaming responses.

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8000 uvx mcp-zuul
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_TRANSPORT` | `stdio` | Transport mode: `stdio`, `sse`, or `streamable-http` |
| `MCP_HOST` | `127.0.0.1` | HTTP server bind address (non-stdio) |
| `MCP_PORT` | `8000` | HTTP server port (non-stdio) |

## Tool filtering

Reduce LLM tool-selection noise by exposing only the tools your workflow needs:

```bash
# Only enable specific tools
ZUUL_ENABLED_TOOLS=diagnose_build,list_builds,get_build_log

# Or disable specific tools
ZUUL_DISABLED_TOOLS=stream_build_console,get_build_anomalies
```

`ZUUL_ENABLED_TOOLS` and `ZUUL_DISABLED_TOOLS` are mutually exclusive. Tools are removed from the server entirely — LLMs don't see them.

## Docker deployment

```bash
docker run -i --rm \
  -e ZUUL_URL=https://example.com/zuul \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_PORT=8000 \
  -p 8000:8000 \
  ghcr.io/imatza-rh/mcp-zuul
```

Multi-platform images (amd64 + arm64) are published on every tag push.
