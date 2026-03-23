"""Shared helpers for Zuul MCP server."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
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
    _auth_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


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

    Retries once on 500/503 (transient server errors, LB hiccups) and
    re-authenticates via Kerberos on 401/302.
    """
    a = app(ctx)
    url = f"/api{path}"

    for attempt in range(2):
        resp = await a.client.get(url, params=params)

        # Re-authenticate if the session expired (Kerberos only).
        if resp.status_code in (401, 302) and a.config.use_kerberos:
            async with a._auth_lock:
                log.info("Session expired, re-authenticating via Kerberos")
                await kerberos_auth(a.client, a.config.base_url)
            resp = await a.client.get(url, params=params)

        # Retry once on 500/503 (transient server errors, LB hiccups).
        if resp.status_code in (500, 503) and attempt == 0:
            log.info("API returned %d for %s, retrying in 2s", resp.status_code, path)
            await asyncio.sleep(2)
            continue

        break

    resp.raise_for_status()
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        ct = resp.headers.get("content-type", "")
        raise ValueError(f"API returned non-JSON response (content-type: {ct})") from exc


async def api_post(ctx: Context, path: str, body: dict) -> Any:
    """Make an authenticated POST request to the Zuul API."""
    a = app(ctx)
    if a.config.read_only:
        raise ValueError("Write operations disabled (ZUUL_READ_ONLY=true)")
    resp = await a.client.post(f"/api{path}", json=body)
    if resp.status_code in (401, 302) and a.config.use_kerberos:
        async with a._auth_lock:
            await kerberos_auth(a.client, a.config.base_url)
        resp = await a.client.post(f"/api{path}", json=body)
    resp.raise_for_status()
    if not resp.text:
        return {}
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        ct = resp.headers.get("content-type", "")
        raise ValueError(f"API returned non-JSON response (content-type: {ct})") from exc


async def api_delete(ctx: Context, path: str) -> Any:
    """Make an authenticated DELETE request to the Zuul API."""
    a = app(ctx)
    if a.config.read_only:
        raise ValueError("Write operations disabled (ZUUL_READ_ONLY=true)")
    resp = await a.client.delete(f"/api{path}")
    if resp.status_code in (401, 302) and a.config.use_kerberos:
        async with a._auth_lock:
            await kerberos_auth(a.client, a.config.base_url)
        resp = await a.client.delete(f"/api{path}")
    resp.raise_for_status()
    if not resp.text:
        return {}
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        ct = resp.headers.get("content-type", "")
        raise ValueError(f"API returned non-JSON response (content-type: {ct})") from exc


_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_FETCH_BYTES = 20 * 1024 * 1024  # 20 MB (for JSON log files)


def _pick_client(a: AppContext, url: str) -> httpx.AsyncClient:
    """Pick the right HTTP client based on log host vs API host."""
    api_host = urlparse(a.config.base_url).hostname
    log_host = urlparse(url).hostname
    return a.client if log_host == api_host else a.log_client


async def _stream_with_limit(a: AppContext, url: str, max_bytes: int) -> tuple[bytes, bool]:
    """Stream a URL with size limit. Returns (content, truncated).

    Raises:
        httpx.HTTPStatusError: on non-404 HTTP errors
        FileNotFoundError: when the URL returns 404
    """
    http = _pick_client(a, url)
    chunks: list[bytes] = []
    size = 0
    truncated = False

    async with http.stream("GET", url) as resp:
        if resp.status_code in (401, 302) and a.config.use_kerberos and http is a.client:
            await resp.aclose()
            async with a._auth_lock:
                await kerberos_auth(a.client, a.config.base_url)
            async with http.stream("GET", url) as resp2:
                if resp2.status_code == 404:
                    raise FileNotFoundError(url)
                resp2.raise_for_status()
                async for chunk in resp2.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        # Include partial chunk up to the limit
                        overshoot = size - max_bytes
                        chunks.append(chunk[: len(chunk) - overshoot])
                        truncated = True
                        break
                    chunks.append(chunk)
        else:
            if resp.status_code == 404:
                raise FileNotFoundError(url)
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    overshoot = size - max_bytes
                    chunks.append(chunk[: len(chunk) - overshoot])
                    truncated = True
                    break
                chunks.append(chunk)
    return b"".join(chunks), truncated


async def fetch_log_url(a: AppContext, url: str) -> httpx.Response:
    """Fetch a log URL with streaming size limit and Kerberos re-auth.

    Downloads up to _MAX_FETCH_BYTES (20 MB) via streaming to prevent
    unbounded memory consumption from large log files.
    """
    http = _pick_client(a, url)

    try:
        return await _fetch_log_stream(http, a, url, max_bytes=_MAX_FETCH_BYTES)
    except httpx.DecodingError:
        # Corrupted gzip — retry without compression so the server
        # sends raw bytes instead of a broken Content-Encoding: gzip.
        log.info("DecodingError fetching %s, retrying without compression", url)
        return await _fetch_log_stream(
            http, a, url, max_bytes=_MAX_FETCH_BYTES,
            headers={"Accept-Encoding": "identity"},
        )


async def _fetch_log_stream(
    http: httpx.AsyncClient,
    a: AppContext,
    url: str,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Internal: stream a log URL with size limit and optional custom headers."""
    chunks: list[bytes] = []
    size = 0

    async with http.stream("GET", url, follow_redirects=True, headers=headers) as resp:
        if resp.status_code in (401, 302) and a.config.use_kerberos and http is a.client:
            await resp.aclose()
            async with a._auth_lock:
                log.info("Log fetch: session expired, re-authenticating via Kerberos")
                await kerberos_auth(a.client, a.config.base_url)
            async with http.stream("GET", url, follow_redirects=True, headers=headers) as resp2:
                resp2_status = resp2.status_code
                resp2_headers = resp2.headers
                resp2_request = resp2.request
                if resp2_status == 404:
                    pass  # skip streaming, return empty content below
                else:
                    async for chunk in resp2.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            overshoot = size - max_bytes
                            chunks.append(chunk[: len(chunk) - overshoot])
                            break
                        chunks.append(chunk)
            return httpx.Response(
                status_code=resp2_status,
                headers=resp2_headers,
                content=b"".join(chunks),
                request=resp2_request,
            )

        resp_status = resp.status_code
        resp_headers = resp.headers
        resp_request = resp.request
        if resp_status == 404:
            pass  # skip streaming, return empty content below
        else:
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    overshoot = size - max_bytes
                    chunks.append(chunk[: len(chunk) - overshoot])
                    break
                chunks.append(chunk)

    return httpx.Response(
        status_code=resp_status,
        headers=resp_headers,
        content=b"".join(chunks),
        request=resp_request,
    )


async def stream_log(a: AppContext, url: str) -> tuple[bytes, bool]:
    """Stream a log file with Kerberos re-auth, size-limited to 10 MB.

    Returns:
        Tuple of (content_bytes, truncated_bool).

    Raises:
        httpx.HTTPStatusError: on non-404 HTTP errors
        FileNotFoundError: when the log file returns 404
    """
    return await _stream_with_limit(a, url, _MAX_LOG_BYTES)


def error(msg: str) -> str:
    """Return a JSON-encoded error message."""
    return json.dumps({"error": msg})


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


_ZUUL_URL_RE = re.compile(r"/t/([^/]+)/(build|buildset)/([^/?#]+)")
_ZUUL_CHANGE_URL_RE = re.compile(r"/t/([^/]+)/status/change/([^/?#]+)")
# Single-tenant URLs without /t/<tenant>/ prefix
_ZUUL_SINGLE_TENANT_RE = re.compile(r"/(build|buildset)/([^/?#]+)")


def parse_zuul_url(url: str) -> tuple[str, str, str] | None:
    """Parse a Zuul web URL into (tenant, resource_type, id).

    Supports build, buildset, and change status URLs, including
    single-tenant deployments without the ``/t/<tenant>/`` prefix
    (returns empty tenant, resolved via ZUUL_DEFAULT_TENANT).

    Examples::

        parse_zuul_url("https://zuul.example.com/t/tenant/build/abc123")
        # -> ("tenant", "build", "abc123")

        parse_zuul_url("https://zuul.example.com/zuul/t/t1/buildset/def456")
        # -> ("t1", "buildset", "def456")

        parse_zuul_url("https://zuul.example.com/t/t1/status/change/12345,abc")
        # -> ("t1", "change", "12345,abc")

        parse_zuul_url("https://zuul.example.com/build/abc123")
        # -> ("", "build", "abc123")  # tenant resolved from ZUUL_DEFAULT_TENANT
    """
    m = _ZUUL_URL_RE.search(url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _ZUUL_CHANGE_URL_RE.search(url)
    if m:
        return m.group(1), "change", m.group(2)
    # Single-tenant URLs (no /t/ prefix)
    m = _ZUUL_SINGLE_TENANT_RE.search(url)
    if m:
        return "", m.group(1), m.group(2)
    return None


def clean(d: dict) -> dict:
    """Remove None values to save tokens."""
    return {k: v for k, v in d.items() if v is not None}
