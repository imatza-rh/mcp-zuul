# Threat Model: mcp-zuul

## 1. System context

mcp-zuul is an MCP (Model Context Protocol) server that provides AI coding
assistants (Claude Code, Cursor, Codex) with read access to Zuul CI systems.
It runs as a local stdio process on the developer's workstation, spawned by
the MCP client. It makes outbound HTTPS requests to one or more Zuul API
endpoints and their associated log servers.

The server authenticates to Kerberos-protected Zuul instances via SPNEGO
(using the developer's existing `kinit` ticket). For public Zuul instances,
no authentication is required.

**Deployment assumptions:**
- Runs as the local user, same privilege level as the MCP client
- Single-tenant: one user per process, no shared state
- Network access: outbound HTTPS only (Zuul API + log servers)
- No persistent storage — all state is in-memory per session
- The MCP client (Claude Code) is trusted; tool inputs come from the AI model
  which itself operates under the client's permission system
- By default, read-only (`ZUUL_READ_ONLY=true`). Write operations (enqueue,
  dequeue, autohold, promote) require explicit opt-in

## 2. Assets

| asset | description | sensitivity |
|---|---|---|
| Kerberos ticket | Developer's TGT used for SPNEGO auth to internal Zuul | high |
| OIDC tokens | JWT acquired during auth flow, cached in-memory | high |
| Build logs | CI job output, may contain test credentials or infra details | medium |
| Job configuration | Zuul job definitions, nodesets, playbooks | low |
| Pipeline status | Current queue state, running builds | low |

## 3. Entry points & trust boundaries

| entry_point | description | trust_boundary | reachable_assets |
|---|---|---|---|
| MCP tool calls | Tool invocations from the AI model via stdio JSON-RPC | MCP client (trusted) → server process | Build logs, Job configuration, Pipeline status |
| Zuul API responses | HTTPS responses from Zuul REST API | Remote server → local process memory | Build logs, Job configuration, Pipeline status |
| Log server responses | HTTPS responses from log storage (Swift, static file servers) | Remote server → local process memory | Build logs |
| Environment variables | ZUUL_URL, ZUUL_DEFAULT_TENANT, ZUUL_READ_ONLY, LOGJUICER_URL | Local env (trusted) → server config | Kerberos ticket (indirectly, via ZUUL_URL target) |
| Kerberos credential cache | ccache read by SPNEGO negotiation | OS credential store → HTTPS auth header | Kerberos ticket, OIDC tokens |

## 4. Threats

| id | threat | actor | surface | asset | impact | likelihood | status | controls | evidence |
|---|---|---|---|---|---|---|---|---|---|
| T1 | Credential leak via log content — build logs may contain embedded secrets, tokens, or credentials that the AI model surfaces in conversation | local_user | Log server responses | Kerberos ticket, OIDC tokens | high | possible | partially_mitigated | Logs are read-only and displayed to the same user who owns the credentials; log servers apply their own redaction | None observed |
| T2 | SSRF via user-controlled URL parameters — tool inputs include `url` parameters that could be crafted to reach unintended internal services | local_user | MCP tool calls | Build logs | medium | rare | mitigated | URL parameters are validated against the configured `base_url`; hostname/scheme stripping for log URLs | None observed |
| T3 | Excessive Zuul API mutation — write tools (enqueue, dequeue, promote, autohold) could disrupt CI pipelines if invoked carelessly | local_user | MCP tool calls | Pipeline status | high | rare | mitigated | `ZUUL_READ_ONLY=true` by default; write tools removed from server at startup unless explicitly enabled; MCP client permission system gates tool calls | None observed |
| T4 | Kerberos ticket exposure in transit — SPNEGO token sent over HTTPS could be intercepted on misconfigured networks | adjacent_network | Kerberos credential cache | Kerberos ticket | high | very_rare | mitigated | All auth flows use HTTPS; SPNEGO tokens are one-use and time-limited | None observed |
| T5 | Denial of service via large log fetch — a tool call requesting a very large log file could exhaust local memory | local_user | Log server responses | Pipeline status | low | possible | partially_mitigated | Log content is streamed and capped (`max_lines`, `max_matches` parameters); `browse_build_logs` enforces 512KB fetch limit | None observed |
| T6 | Prompt injection via log content — malicious content in build logs could manipulate the AI model into unintended actions (data exfiltration, tool misuse) since log content flows directly into the model's context | supply_chain | Log server responses | Build logs, Kerberos ticket | high | rare | partially_mitigated | Server returns raw data without interpretation; the AI model's prompt injection defenses are upstream; MCP client permission system gates tool calls | None observed |
| T7 | Auth state leak between tenants — OIDC tokens or session cookies from one Zuul tenant reused for another | local_user | Zuul API responses | OIDC tokens | medium | very_rare | mitigated | Auth state cleared before each auth flow; per-tenant isolation in config | None observed |

## 5. Deprioritized

| threat | reason |
|---|---|
| Malicious MCP client | The MCP client (Claude Code) runs with the same privileges as this server. A compromised client has direct access to everything this server can reach — defending against it is out of scope |
| Local privilege escalation | Server runs as the invoking user with no elevated privileges. Local privesc is an OS-level concern |
| Supply chain attack on mcp-zuul package | Standard PyPI supply chain risk. Mitigated by pinned dependencies and `uv.lock`. Out of scope for this threat model |
| Data exfiltration by AI model | The AI model could relay sensitive data from tool results to external parties. This is a property of the model/client, not the MCP server |

## 6. Open questions

- Should log content be redacted server-side before returning to the AI model? Currently relies on upstream log-server redaction
- Should `url` and `direct_log_url` parameters be restricted to a configured allowlist of log server domains?
- Should write tools require per-invocation confirmation at the MCP server level (in addition to the client's permission system)?

## 7. Provenance

- mode: bootstrap
- date: 2026-08-12
- target: https://github.com/imatza-rh/mcp-zuul @ v0.10.0
- inputs: source code review (auth.py, config.py, server.py, tools/), pyproject.toml, README.md
- owner: Itay Matza (imatza@redhat.com)

## 8. Recommended mitigations

| mitigation | threat_ids | closes_class | effort |
|---|---|---|---|
| Add log-server domain allowlist for `url`/`direct_log_url` parameters | T2 | yes | S |
| Add optional server-side secret redaction (regex patterns for common token formats) | T1, T6 | partial | M |
| Add per-tool rate limiting for write operations | T3 | partial | S |
