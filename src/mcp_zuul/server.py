"""FastMCP server instance and lifespan management."""

import contextlib
import logging
import sys
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP

from .auth import kerberos_auth
from .config import Config
from .helpers import AppContext

# Logging (stderr only — mandatory for stdio transport)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zuul-mcp")


class _BearerAuth(httpx.Auth):
    """httpx Auth that sends a Bearer token, stripping it on cross-origin redirects."""

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_flow(self, request: httpx.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


@asynccontextmanager
async def lifespan(server: FastMCP):
    config = Config.from_env()
    headers = {"Accept": "application/json"}
    auth = _BearerAuth(config.auth_token) if config.auth_token else None
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
            await kerberos_auth(client, config.base_url)

        # Remove write tools when in read-only mode (default)
        _WRITE_TOOLS = {"enqueue", "dequeue", "autohold_create", "autohold_delete"}
        if config.read_only:
            for name in _WRITE_TOOLS:
                with contextlib.suppress(KeyError):
                    server._tool_manager.remove_tool(name)
            log.info("Read-only mode: write tools disabled")

        # Apply tool filtering
        if config.enabled_tools:
            all_tools = list(server._tool_manager._tools.keys())
            for name in all_tools:
                if name not in config.enabled_tools:
                    server._tool_manager.remove_tool(name)
            log.info("Tools enabled: %s", ", ".join(config.enabled_tools))
        elif config.disabled_tools:
            for name in config.disabled_tools:
                try:
                    server._tool_manager.remove_tool(name)
                except KeyError:
                    log.warning("Cannot disable unknown tool: %s", name)
            log.info("Tools disabled: %s", ", ".join(config.disabled_tools))

        log.info("Zuul MCP connected to %s", config.base_url)
        yield AppContext(client=client, log_client=log_client, config=config)


mcp = FastMCP("zuul-ci", lifespan=lifespan)
