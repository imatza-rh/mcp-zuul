# Security Policy

## Reporting Vulnerabilities

**Do not open public issues for security vulnerabilities.**

Please report vulnerabilities via [GitHub Security Advisories](https://github.com/imatza-rh/mcp-zuul/security/advisories/new).

You will receive an acknowledgment within 48 hours and a detailed response within 7 days.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| < 0.3   | No        |

## Security Controls

mcp-zuul implements the following security measures:

- **Auth token protection**: `_BearerAuth` (httpx Auth subclass) on the API client. httpx strips Authorization headers on cross-origin redirects. Log fetches use a separate unauthenticated client (`log_client`) as defense-in-depth.
- **Kerberos session safety**: `asyncio.Lock` serializes re-authentication to prevent concurrent session corruption.
- **Streaming size caps**: Log downloads are capped at 10 MB (`stream_log`) and 20 MB (`fetch_log_url`) via streaming to prevent memory exhaustion.
- **XML safety**: JUnit XML parsing uses `defusedxml` (not stdlib `xml.etree.ElementTree`) to prevent entity expansion attacks on untrusted test artifacts.
- **Path traversal protection**: `safepath()` rejects `..` segments in URL paths. `browse_build_logs` and `get_build_log` validate `log_name` and `path` parameters.
- **Regex timeout**: User-supplied grep patterns run in a thread executor with a 10-second timeout to prevent catastrophic backtracking.
- **Read-only by default**: Write tools (enqueue, dequeue, autohold) are removed from the server entirely when `ZUUL_READ_ONLY=true` (default). LLMs cannot invoke tools that don't exist.
- **Input validation**: All user-facing parameters are validated before use. Limits enforced on pagination (max 100), log lines (max 500), and response sizes.

## Best Practices for Users

- Never hardcode `ZUUL_AUTH_TOKEN` in config files — use environment variables
- Use `ZUUL_VERIFY_SSL=true` (default) in production
- Keep `ZUUL_READ_ONLY=true` (default) unless write operations are explicitly needed
- For Docker, forward tokens without values: `-e ZUUL_AUTH_TOKEN` (inherits from host)
- Use `ZUUL_ENABLED_TOOLS` to restrict tool exposure to only what's needed
