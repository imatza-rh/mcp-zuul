"""Zuul CI MCP Server — read-only access to builds, logs, status, and jobs."""

# Import tools and prompts modules to register @mcp.tool() and @mcp.prompt() decorators
from . import prompts as _prompts  # noqa: F401
from . import tools as _tools  # noqa: F401
from .config import Config
from .helpers import AppContext, clean, strip_ansi
from .server import mcp

__all__ = ["AppContext", "Config", "clean", "main", "mcp", "strip_ansi"]


def main():
    mcp.run(transport="stdio")
