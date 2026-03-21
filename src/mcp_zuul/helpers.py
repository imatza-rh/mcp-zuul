"""Shared helpers for Zuul MCP server."""

import asyncio
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
    """Make an authenticated GET request to the Zuul API.

    Retries once on 503 (common behind load balancers) and re-authenticates
    via Kerberos on 401/302.
    """
    a = app(ctx)
    url = f"/api{path}"

    for attempt in range(2):
        resp = await a.client.get(url, params=params)

        # Re-authenticate if the session expired (Kerberos only).
        if resp.status_code in (401, 302) and a.config.use_kerberos:
            log.info("Session expired, re-authenticating via Kerberos")
            await kerberos_auth(a.client, a.config.base_url)
            resp = await a.client.get(url, params=params)

        # Retry once on 503 Service Unavailable (transient LB errors).
        if resp.status_code == 503 and attempt == 0:
            log.info("API returned 503 for %s, retrying in 1s", path)
            await asyncio.sleep(1)
            continue

        break

    resp.raise_for_status()
    return resp.json()


async def api_post(ctx: Context, path: str, body: dict) -> Any:
    """Make an authenticated POST request to the Zuul API."""
    a = app(ctx)
    if a.config.read_only:
        raise ValueError("Write operations disabled (ZUUL_READ_ONLY=true)")
    resp = await a.client.post(f"/api{path}", json=body)
    if resp.status_code in (401, 302) and a.config.use_kerberos:
        await kerberos_auth(a.client, a.config.base_url)
        resp = await a.client.post(f"/api{path}", json=body)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


async def api_delete(ctx: Context, path: str) -> Any:
    """Make an authenticated DELETE request to the Zuul API."""
    a = app(ctx)
    if a.config.read_only:
        raise ValueError("Write operations disabled (ZUUL_READ_ONLY=true)")
    resp = await a.client.delete(f"/api{path}")
    if resp.status_code in (401, 302) and a.config.use_kerberos:
        await kerberos_auth(a.client, a.config.base_url)
        resp = await a.client.delete(f"/api{path}")
    resp.raise_for_status()
    return resp.json() if resp.text else {}


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


_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB


async def stream_log(a: AppContext, url: str) -> bytes:
    """Stream a log file with Kerberos re-auth, size-limited to 10 MB.

    Raises:
        httpx.HTTPStatusError: on non-404 HTTP errors
        FileNotFoundError: when the log file returns 404
    """
    api_host = urlparse(a.config.base_url).hostname
    log_host = urlparse(url).hostname
    http = a.client if log_host == api_host else a.log_client

    chunks: list[bytes] = []
    size = 0
    async with http.stream("GET", url) as resp:
        if resp.status_code in (401, 302) and a.config.use_kerberos and http is a.client:
            await resp.aclose()
            await kerberos_auth(a.client, a.config.base_url)
            async with http.stream("GET", url) as resp2:
                if resp2.status_code == 404:
                    raise FileNotFoundError(url)
                resp2.raise_for_status()
                async for chunk in resp2.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_LOG_BYTES:
                        break
                    chunks.append(chunk)
        else:
            if resp.status_code == 404:
                raise FileNotFoundError(url)
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > _MAX_LOG_BYTES:
                    break
                chunks.append(chunk)
    return b"".join(chunks)


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
