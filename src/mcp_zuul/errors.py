"""Uniform error handling decorator for Zuul MCP tools."""

import functools
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from .helpers import error

log = logging.getLogger("zuul-mcp")

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_body(text: str, limit: int = 200) -> str:
    """Extract a clean error message from an HTTP response body.

    Strips HTML tags and collapses whitespace so error messages
    are useful for LLM consumers instead of containing raw markup.
    """
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip()


def handle_errors(
    func: Callable[..., Coroutine[Any, Any, str]],
) -> Callable[..., Coroutine[Any, Any, str]]:
    """Wrap tool functions with uniform error handling."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            body = _clean_body(e.response.text)
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
