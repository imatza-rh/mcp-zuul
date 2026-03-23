"""Configuration management for Zuul MCP server."""

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("zuul-mcp")


@dataclass
class Config:
    """Server configuration loaded from environment variables.

    Raises ValueError on invalid configuration instead of sys.exit(),
    so the caller (main() or embedder) can handle errors appropriately.
    """

    base_url: str
    default_tenant: str
    auth_token: str | None
    timeout: int
    verify_ssl: bool
    use_kerberos: bool
    transport: str
    enabled_tools: list[str] | None
    disabled_tools: list[str] | None
    host: str
    port: int
    read_only: bool
    logjuicer_url: str | None

    @classmethod
    def from_env(cls) -> "Config":
        base_url = os.environ.get("ZUUL_URL", "").rstrip("/")
        if not base_url:
            raise ValueError("ZUUL_URL environment variable is required")
        raw_timeout = os.environ.get("ZUUL_TIMEOUT", "30")
        try:
            timeout = int(raw_timeout)
        except ValueError:
            raise ValueError(
                f"ZUUL_TIMEOUT must be an integer (seconds), got: {raw_timeout}"
            ) from None
        use_kerberos = os.environ.get("ZUUL_USE_KERBEROS", "false").lower() == "true"
        auth_token = os.environ.get("ZUUL_AUTH_TOKEN")
        if use_kerberos and auth_token:
            raise ValueError("ZUUL_USE_KERBEROS and ZUUL_AUTH_TOKEN are mutually exclusive")
        if use_kerberos:
            try:
                import gssapi  # noqa: F401
            except ImportError:
                raise ValueError(
                    "ZUUL_USE_KERBEROS=true but 'gssapi' is not installed. "
                    "Install with: pip install mcp-zuul[kerberos]"
                ) from None
        transport = os.environ.get("MCP_TRANSPORT", "stdio")
        if transport not in ("stdio", "sse", "streamable-http"):
            raise ValueError(
                f"MCP_TRANSPORT must be stdio, sse, or streamable-http, got: {transport}"
            )
        raw_port = os.environ.get("MCP_PORT", "8000")
        try:
            port = int(raw_port)
        except ValueError:
            raise ValueError(f"MCP_PORT must be an integer, got: {raw_port}") from None

        enabled_raw = os.environ.get("ZUUL_ENABLED_TOOLS", "")
        disabled_raw = os.environ.get("ZUUL_DISABLED_TOOLS", "")
        enabled_tools = [t.strip() for t in enabled_raw.split(",") if t.strip()] or None
        disabled_tools = [t.strip() for t in disabled_raw.split(",") if t.strip()] or None
        if enabled_tools and disabled_tools:
            raise ValueError("ZUUL_ENABLED_TOOLS and ZUUL_DISABLED_TOOLS are mutually exclusive")

        return cls(
            base_url=base_url,
            default_tenant=os.environ.get("ZUUL_DEFAULT_TENANT", ""),
            auth_token=auth_token,
            timeout=timeout,
            verify_ssl=os.environ.get("ZUUL_VERIFY_SSL", "true").lower() == "true",
            use_kerberos=use_kerberos,
            transport=transport,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=port,
            read_only=os.environ.get("ZUUL_READ_ONLY", "true").lower() != "false",
            logjuicer_url=os.environ.get("LOGJUICER_URL", "").rstrip("/") or None,
        )
