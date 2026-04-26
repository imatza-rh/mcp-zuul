"""Live console streaming from running builds (optional, requires websockets)."""

import asyncio
import collections
import json
import ssl

from mcp.server.fastmcp import Context

from ..errors import handle_errors
from ..helpers import app, error, safepath, strip_ansi
from ..server import mcp
from ._common import _READ_ONLY, _resolve


def _import_websockets():
    """Lazy import of websockets library."""
    import websockets

    return websockets


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
        ws_url = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_url = "ws://" + base[len("http://") :]
    else:
        return error(f"Cannot build WebSocket URL from base: {base}")
    ws_url = f"{ws_url}/api/tenant/{safepath(t)}/console-stream"

    # SSL context for wss:// connections.
    # websockets requires ssl=True (default context) or an explicit SSLContext
    # for wss:// URIs — ssl=None is rejected as incompatible.
    ssl_ctx: ssl.SSLContext | bool | None = None
    if ws_url.startswith("wss://"):
        if a.config.verify_ssl:
            ssl_ctx = True  # use ssl.create_default_context()
        else:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    # First message: uuid + logfile (required by Zuul) + optional JWT token
    init_msg: dict = {"uuid": build_uuid, "logfile": "console.log"}
    if a.config.auth_token:
        init_msg["token"] = a.config.auth_token

    try:
        async with websockets.connect(
            ws_url,
            ssl=ssl_ctx,
            open_timeout=10,
            close_timeout=5,
        ) as ws:
            await ws.send(json.dumps(init_msg))

            buffer: collections.deque[str] = collections.deque(maxlen=lines)
            total_lines = 0
            pending = ""  # accumulates partial line across chunk boundaries
            try:
                async with asyncio.timeout(timeout):
                    async for message in ws:
                        text = (
                            message
                            if isinstance(message, str)
                            else message.decode("utf-8", errors="replace")
                        )
                        # Reassemble lines across chunk boundaries: prepend
                        # any leftover from the previous chunk, split on \n,
                        # and keep the last fragment as the new pending.
                        text = pending + text
                        parts = text.split("\n")
                        pending = parts.pop()  # last element: partial or ""
                        for line in parts:
                            if line:
                                buffer.append(strip_ansi(line))
                                total_lines += 1
            except TimeoutError:
                pass  # Expected — we buffer for `timeout` seconds
            # Flush any trailing partial line
            if pending:
                buffer.append(strip_ansi(pending))
                total_lines += 1

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

    except websockets.InvalidStatus as e:
        code = getattr(getattr(e, "response", None), "status_code", 0)
        if code == 403:
            return error(
                "WebSocket auth failed (403). "
                "Check auth token (ZUUL_AUTH_TOKEN) or tenant auth configuration."
            )
        if code == 404:
            return error(
                f"Console stream not available for build {build_uuid}. "
                "Build may have completed — use tail_build_log instead."
            )
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
