"""Live console streaming from running builds (optional, requires websockets)."""

import asyncio
import collections
import json
import logging
import ssl

from mcp.server.fastmcp import Context

from ..auth import kerberos_auth
from ..errors import handle_errors
from ..helpers import AppContext, app, error, safepath, strip_ansi
from ..server import mcp
from ._common import _READ_ONLY, _resolve

log = logging.getLogger("zuul-mcp")


def _import_websockets():
    """Lazy import of websockets library."""
    import websockets

    return websockets


def _cookie_header(a: AppContext) -> dict[str, str]:
    """Build HTTP headers with session cookies from the httpx client.

    Kerberos auth stores session cookies on the httpx client; these must
    be forwarded to the WebSocket HTTP upgrade request since the websockets
    library doesn't share the httpx cookie jar.
    """
    if not a.client.cookies:
        return {}
    cookie_val = "; ".join(f"{n}={v}" for n, v in a.client.cookies.items())
    return {"Cookie": cookie_val}


async def _ws_stream(
    websockets,
    ws_url: str,
    ssl_ctx: ssl.SSLContext | bool | None,
    additional_headers: dict[str, str] | None,
    init_msg: dict,
    lines: int,
    timeout: int,
) -> tuple[collections.deque[str], int]:
    """Connect to WebSocket, send init message, and buffer console lines.

    Returns (buffer, total_lines).
    Raises websockets exceptions on connection/protocol errors.
    """
    async with websockets.connect(
        ws_url,
        ssl=ssl_ctx,
        additional_headers=additional_headers or None,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        await ws.send(json.dumps(init_msg))

        buffer: collections.deque[str] = collections.deque(maxlen=lines)
        total_lines = 0
        pending = ""
        try:
            async with asyncio.timeout(timeout):
                async for message in ws:
                    text = (
                        message
                        if isinstance(message, str)
                        else message.decode("utf-8", errors="replace")
                    )
                    text = pending + text
                    parts = text.split("\n")
                    pending = parts.pop()
                    for line in parts:
                        if line:
                            buffer.append(strip_ansi(line))
                            total_lines += 1
        except TimeoutError:
            pass
        if pending:
            buffer.append(strip_ansi(pending))
            total_lines += 1

    return buffer, total_lines


@mcp.tool(title="Stream Build Console", annotations=_READ_ONLY)
@handle_errors
async def stream_build_console(
    ctx: Context,
    uuid: str = "",
    tenant: str = "",
    url: str = "",
    lines: int = 100,
    timeout: int = 10,
) -> str:
    """Read live console output from a RUNNING build.

    Connects to Zuul's WebSocket console-stream endpoint and captures
    output for ``timeout`` seconds, returning the last ``lines`` lines
    (tail behavior). This tool is for RUNNING builds only. For completed
    builds, use tail_build_log or get_build_log instead.

    Optional — requires ``pip install mcp-zuul[console]``.

    Args:
        uuid: Build UUID (from get_change_status)
        tenant: Tenant name (uses default if empty)
        url: Zuul build URL (alternative to uuid + tenant)
        lines: Number of lines to return from the end (default 100, max 500)
        timeout: Seconds to buffer before returning (default 10, max 30)
    """
    build_uuid, t = _resolve(ctx, uuid, tenant, url, "build")
    lines = min(max(lines, 1), 500)
    timeout = min(max(timeout, 3), 30)

    try:
        websockets = _import_websockets()
    except ImportError:
        return error(
            "websockets library not installed. Install with: pip install mcp-zuul[console]"
        )

    a = app(ctx)

    # Build WebSocket URL from API base URL
    base = a.config.base_url
    if base.startswith("https://"):
        ws_url = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_url = "ws://" + base[len("http://"):]
    else:
        return error(f"Cannot build WebSocket URL from base: {base}")
    ws_url = f"{ws_url}/api/tenant/{safepath(t)}/console-stream"

    # SSL context for wss:// connections.
    ssl_ctx: ssl.SSLContext | bool | None = None
    if ws_url.startswith("wss://"):
        if a.config.verify_ssl:
            ssl_ctx = True
        else:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    # First message: uuid + logfile (required by Zuul) + optional JWT token
    init_msg: dict = {"uuid": build_uuid, "logfile": "console.log"}
    if a.config.auth_token:
        init_msg["token"] = a.config.auth_token

    # Session cookies from Kerberos auth (sent during HTTP upgrade)
    ws_headers = _cookie_header(a)

    try:
        buffer, total_lines = await _ws_stream(
            websockets, ws_url, ssl_ctx, ws_headers, init_msg, lines, timeout
        )
    except websockets.InvalidStatus as e:
        code = getattr(getattr(e, "response", None), "status_code", 0)

        # Re-authenticate on 401 when using Kerberos (session expired)
        if code == 401 and a.config.use_kerberos:
            gen = a._auth_generation
            async with a._auth_lock:
                if a._auth_generation == gen:
                    log.info("WebSocket 401, re-authenticating via Kerberos")
                    await kerberos_auth(a.client, a.config.base_url)
                    a._auth_generation += 1
            ws_headers = _cookie_header(a)
            try:
                buffer, total_lines = await _ws_stream(
                    websockets, ws_url, ssl_ctx, ws_headers, init_msg, lines, timeout
                )
            except websockets.InvalidStatus as e2:
                code2 = getattr(getattr(e2, "response", None), "status_code", 0)
                return error(
                    f"WebSocket auth failed after Kerberos re-auth: HTTP {code2}. "
                    "Check Kerberos ticket (kinit) and tenant auth configuration."
                )
            except (
                websockets.ConnectionClosedError,
                TimeoutError,
                ConnectionRefusedError,
                OSError,
            ):
                raise
        elif code == 401:
            return error(
                "WebSocket auth failed (401). "
                "For Kerberos: set ZUUL_USE_KERBEROS=true and run kinit. "
                "For JWT: set ZUUL_AUTH_TOKEN."
            )
        elif code == 403:
            return error(
                "WebSocket auth failed (403). "
                "Check auth token (ZUUL_AUTH_TOKEN) or tenant auth configuration."
            )
        elif code == 404:
            return error(
                f"Console stream not available for build {build_uuid}. "
                "Build may have completed — use tail_build_log instead."
            )
        else:
            return error(f"WebSocket connection failed: HTTP {code}")
    except websockets.ConnectionClosedError as e:
        rcvd = getattr(e, "rcvd", None)
        code = getattr(rcvd, "code", 0)
        reason = getattr(rcvd, "reason", "")
        if code == 4000:
            return error(f"Console stream rejected (4000): {reason}")
        if code == 4011:
            return error(f"Console stream error (4011): {reason}")
        return error(f"Console stream connection closed: code={code} {reason}".strip())
    except TimeoutError:
        return error("Console stream connection timed out")
    except (ConnectionRefusedError, OSError) as e:
        return error(f"Cannot connect to console stream: {e}")

    if not buffer:
        return error(
            f"No console output received for build {build_uuid}. "
            "The build may have completed or the executor is unreachable."
        )

    return json.dumps(
        {
            "build_uuid": build_uuid,
            "tenant": t,
            "total_lines_received": total_lines,
            "lines_returned": len(buffer),
            "tail": total_lines > len(buffer),
            "timeout_seconds": timeout,
            "console": "\n".join(buffer),
        }
    )
