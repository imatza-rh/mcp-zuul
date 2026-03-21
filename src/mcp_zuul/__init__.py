"""Zuul CI MCP Server — read-only access to builds, logs, status, and jobs."""

# Import tools module to register all @mcp.tool() decorators
from . import tools as _tools  # noqa: F401
from .config import Config
from .helpers import AppContext, clean, strip_ansi
from .server import mcp

__all__ = ["AppContext", "Config", "clean", "main", "mcp", "strip_ansi"]


def main():
    mcp.run(transport="stdio")
