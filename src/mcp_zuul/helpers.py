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
    re-authenticates via Kerberos on 401.
    """
    a = app(ctx)
    url = f"/api{path}"

    for attempt in range(2):
        resp = await a.client.get(url, params=params)

        # Re-authenticate if the session expired (Kerberos only).
        # Note: 302 is never seen here because client uses follow_redirects=True;
        # httpx follows the OIDC redirect chain and we see the final 401.
        if resp.status_code == 401 and a.config.use_kerberos:
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
    if resp.status_code == 401 and a.config.use_kerberos:
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
    if resp.status_code == 401 and a.config.use_kerberos:
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


async def _stream_response(
    a: AppContext,
    url: str,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, bool]:
    """Core streaming function with size limit and Kerberos re-auth.

    Single implementation backing both ``fetch_log_url`` (returns Response)
    and ``stream_log`` (returns bytes + truncated flag).

    Returns:
        Tuple of (response, truncated). Does not raise on 404 — callers
        should check ``response.status_code``.
    """
    http = _pick_client(a, url)

    async def _fetch() -> tuple[httpx.Response, bool]:
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async with http.stream("GET", url, follow_redirects=True, headers=headers) as resp:
            status = resp.status_code
            hdrs = resp.headers
            req = resp.request
            if status != 404:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        overshoot = size - max_bytes
                        chunks.append(chunk[: len(chunk) - overshoot])
                        truncated = True
                        break
                    chunks.append(chunk)
        return httpx.Response(
            status_code=status, headers=hdrs, content=b"".join(chunks), request=req
        ), truncated

    resp, truncated = await _fetch()

    # Re-authenticate on 401 (Kerberos only, API client only)
    if resp.status_code == 401 and a.config.use_kerberos and http is a.client:
        async with a._auth_lock:
            log.info("Session expired, re-authenticating via Kerberos")
            await kerberos_auth(a.client, a.config.base_url)
        resp, truncated = await _fetch()

    return resp, truncated


async def fetch_log_url(a: AppContext, url: str) -> httpx.Response:
    """Fetch a log URL with streaming size limit and Kerberos re-auth.

    Downloads up to _MAX_FETCH_BYTES (20 MB) via streaming to prevent
    unbounded memory consumption from large log files.
    """
    try:
        resp, _ = await _stream_response(a, url, max_bytes=_MAX_FETCH_BYTES)
        return resp
    except httpx.DecodingError:
        # Corrupted gzip — retry without compression so the server
        # sends raw bytes instead of a broken Content-Encoding: gzip.
        log.info("DecodingError fetching %s, retrying without compression", url)
        try:
            resp, _ = await _stream_response(
                a, url, max_bytes=_MAX_FETCH_BYTES, headers={"Accept-Encoding": "identity"}
            )
            return resp
        except httpx.DecodingError:
            # Server ignored Accept-Encoding: identity and sent corrupt
            # Content-Encoding: gzip again.  Re-raise so callers can
            # fall back to text-based diagnosis.
            log.warning("DecodingError persists after identity retry for %s", url)
            raise


async def stream_log(a: AppContext, url: str) -> tuple[bytes, bool]:
    """Stream a log file with Kerberos re-auth, size-limited to 10 MB.

    Returns:
        Tuple of (content_bytes, truncated_bool).

    Raises:
        httpx.HTTPStatusError: on non-404 HTTP errors
        FileNotFoundError: when the log file returns 404
    """
    resp, truncated = await _stream_response(a, url, max_bytes=_MAX_LOG_BYTES)
    if resp.status_code == 404:
        raise FileNotFoundError(url)
    if resp.status_code >= 400:
        resp.raise_for_status()
    return resp.content, truncated


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
