"""Configuration management for Zuul MCP server."""

import logging
import os
import sys
from dataclasses import dataclass

log = logging.getLogger("zuul-mcp")


@dataclass
class Config:
    """Server configuration loaded from environment variables."""

    base_url: str
    default_tenant: str
    auth_token: str | None
    timeout: int
    verify_ssl: bool
    use_kerberos: bool

    @classmethod
    def from_env(cls) -> "Config":
        base_url = os.environ.get("ZUUL_URL", "").rstrip("/")
        if not base_url:
            log.error("ZUUL_URL environment variable is required")
            sys.exit(1)
        raw_timeout = os.environ.get("ZUUL_TIMEOUT", "30")
        try:
            timeout = int(raw_timeout)
        except ValueError:
            log.error("ZUUL_TIMEOUT must be an integer (seconds), got: %s", raw_timeout)
            sys.exit(1)
        use_kerberos = os.environ.get("ZUUL_USE_KERBEROS", "false").lower() == "true"
        auth_token = os.environ.get("ZUUL_AUTH_TOKEN")
        if use_kerberos and auth_token:
            log.error("ZUUL_USE_KERBEROS and ZUUL_AUTH_TOKEN are mutually exclusive")
            sys.exit(1)
        if use_kerberos:
            try:
                import gssapi  # noqa: F401
            except ImportError:
                log.error(
                    "ZUUL_USE_KERBEROS=true but 'gssapi' is not installed. "
                    "Install with: pip install mcp-zuul[kerberos]"
                )
                sys.exit(1)
        return cls(
            base_url=base_url,
            default_tenant=os.environ.get("ZUUL_DEFAULT_TENANT", ""),
            auth_token=auth_token,
            timeout=timeout,
            verify_ssl=os.environ.get("ZUUL_VERIFY_SSL", "true").lower() == "true",
            use_kerberos=use_kerberos,
        )
