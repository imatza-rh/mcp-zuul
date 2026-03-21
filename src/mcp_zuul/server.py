"""FastMCP server instance and lifespan management."""

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


@asynccontextmanager
async def lifespan(server: FastMCP):
    config = Config.from_env()
    headers = {"Accept": "application/json"}
    if config.auth_token:
        headers["Authorization"] = f"Bearer {config.auth_token}"
    async with (
        httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
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
        log.info("Zuul MCP connected to %s", config.base_url)
        yield AppContext(client=client, log_client=log_client, config=config)


mcp = FastMCP("zuul-ci", lifespan=lifespan)
