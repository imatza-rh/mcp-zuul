"""Uniform error handling decorator for Zuul MCP tools."""

import functools
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from .helpers import error

log = logging.getLogger("zuul-mcp")


def handle_errors(
    func: Callable[..., Coroutine[Any, Any, str]],
) -> Callable[..., Coroutine[Any, Any, str]]:
    """Wrap tool functions with uniform error handling."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            body = e.response.text[:200] if e.response.text else ""
            return error(f"API returned {e.response.status_code}: {body}")
        except httpx.ConnectError:
            return error("Cannot connect to Zuul API")
        except httpx.TimeoutException:
            return error("Request timed out")
        except FileNotFoundError as e:
            return error(f"Log file not found at {e}")
        except ValueError as e:
            return error(str(e))
        except Exception as e:
            log.exception("Unexpected error in %s", func.__name__)
            return error(f"Internal error: {type(e).__name__}: {e}")

    return wrapper
