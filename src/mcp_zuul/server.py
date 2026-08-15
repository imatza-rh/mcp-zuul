"""MCPServer instance and lifespan management."""

import asyncio
import concurrent.futures
import logging
import sys
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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


def _remove_tool(server: MCPServer, name: str) -> bool:
    """Remove a tool by name. Returns False if tool does not exist."""
    try:
        server.remove_tool(name)
        return True
    except ToolError:
        return False


async def _list_tool_names(server: MCPServer) -> list[str]:
    """List registered tool names."""
    return [t.name for t in await server.list_tools()]


@asynccontextmanager
async def lifespan(server: MCPServer):
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
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await kerberos_auth(client, config.base_url)
                    break
                except httpx.ConnectError as e:
                    if is_ssl_error(e):
                        raise RuntimeError(
                            "SSL certificate verification failed during Kerberos authentication. "
                            "Set ZUUL_VERIFY_SSL=false for self-signed certificates"
                        ) from e
                    if attempt >= max_retries - 1:
                        log.error("Kerberos auth failed after %d attempts: %s", max_retries, e)
                        raise
                    last_err: Exception = e
                except Exception as e:
                    if attempt >= max_retries - 1:
                        log.error("Kerberos auth failed after %d attempts: %s", max_retries, e)
                        raise
                    last_err = e
                # Retry with increasing delay (5s, 10s)
                delay = 5 * (attempt + 1)
                log.warning(
                    "Kerberos auth failed (attempt %d/%d): %s. Retrying in %ds",
                    attempt + 1,
                    max_retries,
                    last_err,
                    delay,
                )
                await asyncio.sleep(delay)

        # Remove write tools when in read-only mode (default).
        # autohold_create and autohold_delete are management operations
        # (not pipeline-affecting) and remain available in read-only mode.
        _WRITE_TOOLS = {
            "enqueue",
            "promote",
            "dequeue",
            "reenqueue_buildset",
        }
        if config.read_only:
            for name in _WRITE_TOOLS:
                _remove_tool(server, name)
            log.info("Read-only mode: write tools disabled")

        # Apply tool filtering
        if config.enabled_tools:
            all_tools = await _list_tool_names(server)
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


try:
    _version = version("mcp-zuul")
except PackageNotFoundError:
    _version = "0.0.0-dev"

mcp = MCPServer(
    "zuul-ci",
    instructions="Zuul CI server providing build analysis, log search, pipeline status, and job configuration",
    version=_version,
    lifespan=lifespan,
)
