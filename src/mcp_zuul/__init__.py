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
    import sys

    try:
        config = Config.from_env()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    kwargs: dict = {"transport": config.transport}
    if config.transport != "stdio":
        kwargs["host"] = config.host
        kwargs["port"] = config.port
    mcp.run(**kwargs)
