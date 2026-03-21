"""Zuul CI MCP Server — read-only access to builds, logs, status, and jobs."""

# Import modules to register @mcp.tool(), @mcp.prompt(), and @mcp.resource() decorators
from . import prompts as _prompts  # noqa: F401
from . import resources as _resources  # noqa: F401
from . import tools as _tools  # noqa: F401
from .config import Config
from .helpers import AppContext, clean, strip_ansi
from .server import mcp

__all__ = ["AppContext", "Config", "clean", "main", "mcp", "strip_ansi"]


def main():
    import os

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    kwargs: dict = {"transport": transport}
    if transport != "stdio":
        kwargs["host"] = os.environ.get("MCP_HOST", "127.0.0.1")
        raw_port = os.environ.get("MCP_PORT", "8000")
        try:
            kwargs["port"] = int(raw_port)
        except ValueError:
            import sys

            print(f"MCP_PORT must be an integer, got: {raw_port}", file=sys.stderr)
            sys.exit(1)
    mcp.run(**kwargs)
