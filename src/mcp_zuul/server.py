"""FastMCP server instance and lifespan management."""

import concurrent.futures
import logging
import sys
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP

from .auth import kerberos_auth
from .config import Config
from .helpers import AppContext, is_ssl_error

# Logging (stderr only — mandatory for stdio transport)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zuul-mcp")


class _BearerAuth(httpx.Auth):
    """httpx Auth that sends a Bearer token on every request.

    Cross-origin redirect protection is handled by httpx itself:
    ``_redirect_headers()`` strips the ``Authorization`` header when
    following redirects to a different origin (unless it's an
    HTTP-to-HTTPS upgrade on the same host).
    """

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_flow(self, request: httpx.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


def _remove_tool(server: FastMCP, name: str) -> bool:
    """Remove a tool by name, tolerating FastMCP internal API changes."""
    try:
        server._tool_manager.remove_tool(name)
        return True
    except (AttributeError, KeyError):
        return False


def _list_tool_names(server: FastMCP) -> list[str]:
    """List registered tool names, tolerating FastMCP internal API changes."""
    try:
        return [t.name for t in server._tool_manager.list_tools()]
    except AttributeError:
        log.warning("Cannot list tools - FastMCP internal API may have changed")
        return []


@asynccontextmanager
async def lifespan(server: FastMCP):
    config = Config.from_env()
    headers = {"Accept": "application/json"}
    auth = _BearerAuth(config.auth_token) if config.auth_token else None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    async with (
        httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            auth=auth,
            timeout=config.timeout,
            follow_redirects=True,
            verify=config.verify_ssl,
        ) as client,
        httpx.AsyncClient(
            timeout=config.timeout,
            follow_redirects=True,
            verify=config.verify_ssl,
        ) as log_client,
    ):
        if config.use_kerberos:
            try:
                await kerberos_auth(client, config.base_url)
            except httpx.ConnectError as e:
                if is_ssl_error(e):
                    raise RuntimeError(
                        "SSL certificate verification failed during Kerberos authentication. "
                        "Set ZUUL_VERIFY_SSL=false for self-signed certificates"
                    ) from e
                raise

        # Remove write tools when in read-only mode (default)
        _WRITE_TOOLS = {
            "enqueue",
            "enqueue_ref",
            "dequeue",
            "autohold_create",
            "autohold_delete",
            "reenqueue_buildset",
        }
        if config.read_only:
            for name in _WRITE_TOOLS:
                _remove_tool(server, name)
            log.info("Read-only mode: write tools disabled")

        # Apply tool filtering
        if config.enabled_tools:
            all_tools = _list_tool_names(server)
            for name in all_tools:
                if name not in config.enabled_tools:
                    _remove_tool(server, name)
            log.info("Tools enabled: %s", ", ".join(config.enabled_tools))
        elif config.disabled_tools:
            for name in config.disabled_tools:
                if not _remove_tool(server, name):
                    log.warning("Cannot disable unknown tool: %s", name)
            log.info("Tools disabled: %s", ", ".join(config.disabled_tools))

        log.info("Zuul MCP connected to %s", config.base_url)
        try:
            yield AppContext(
                client=client,
                log_client=log_client,
                config=config,
                grep_executor=executor,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


mcp = FastMCP("zuul-ci", lifespan=lifespan)
