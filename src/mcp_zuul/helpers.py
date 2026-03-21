"""Shared helpers for Zuul MCP server."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from mcp.server.fastmcp import Context

from .auth import kerberos_auth
from .config import Config

log = logging.getLogger("zuul-mcp")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


@dataclass
class AppContext:
    """Shared application state injected via FastMCP lifespan."""

    client: httpx.AsyncClient
    log_client: httpx.AsyncClient
    config: Config


def app(ctx: Context) -> AppContext:
    """Extract AppContext from the MCP request context."""
    return ctx.request_context.lifespan_context


def tenant(ctx: Context, t: str) -> str:
    """Resolve tenant name, falling back to default."""
    resolved = t or app(ctx).config.default_tenant
    if not resolved:
        raise ValueError("tenant is required (no ZUUL_DEFAULT_TENANT set)")
    return resolved


def safepath(value: str) -> str:
    """Sanitize a user-supplied value for use in a URL path.

    Preserves slashes (needed for Zuul project names like org/repo)
    but rejects path traversal attempts.
    """
    if ".." in value.split("/"):
        raise ValueError(f"Invalid path segment: {value!r}")
    return quote(value, safe="/")


async def api(ctx: Context, path: str, params: dict | None = None) -> Any:
    """Make an authenticated GET request to the Zuul API."""
    a = app(ctx)
    resp = await a.client.get(f"/api{path}", params=params)

    # Re-authenticate if the session expired (Kerberos only).
    if resp.status_code in (401, 302) and a.config.use_kerberos:
        log.info("Session expired, re-authenticating via Kerberos")
        await kerberos_auth(a.client, a.config.base_url)
        resp = await a.client.get(f"/api{path}", params=params)

    resp.raise_for_status()
    return resp.json()


async def fetch_log_url(a: AppContext, url: str) -> httpx.Response:
    """Fetch a log URL with automatic Kerberos re-auth if needed."""
    api_host = urlparse(a.config.base_url).hostname
    log_host = urlparse(url).hostname
    http = a.client if log_host == api_host else a.log_client
    resp = await http.get(url, follow_redirects=True)
    if resp.status_code in (401, 302) and a.config.use_kerberos and http is a.client:
        log.info("Log fetch: session expired, re-authenticating via Kerberos")
        await kerberos_auth(a.client, a.config.base_url)
        resp = await http.get(url, follow_redirects=True)
    return resp


def error(msg: str) -> str:
    """Return a JSON-encoded error message."""
    return json.dumps({"error": msg})


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


_ZUUL_URL_RE = re.compile(r"/t/([^/]+)/(build|buildset)/([^/?#]+)")
_ZUUL_CHANGE_URL_RE = re.compile(r"/t/([^/]+)/status/change/([^/?#]+)")


def parse_zuul_url(url: str) -> tuple[str, str, str] | None:
    """Parse a Zuul web URL into (tenant, resource_type, id).

    Supports build, buildset, and change status URLs.
    Returns None if the URL doesn't match any known pattern.

    Examples::

        parse_zuul_url("https://zuul.example.com/t/tenant/build/abc123")
        # -> ("tenant", "build", "abc123")

        parse_zuul_url("https://zuul.example.com/zuul/t/t1/buildset/def456")
        # -> ("t1", "buildset", "def456")

        parse_zuul_url("https://zuul.example.com/t/t1/status/change/12345,abc")
        # -> ("t1", "change", "12345,abc")
    """
    m = _ZUUL_URL_RE.search(url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _ZUUL_CHANGE_URL_RE.search(url)
    if m:
        return m.group(1), "change", m.group(2)
    return None


def clean(d: dict) -> dict:
    """Remove None values to save tokens."""
    return {k: v for k, v in d.items() if v is not None}
