"""Uniform error handling decorator for Zuul MCP tools."""

import functools
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from .helpers import error, is_ssl_error

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


_DNS_HINTS = ("getaddrinfo failed", "name or service not known", "nodename nor servname")
_REFUSED_HINTS = ("connection refused",)
_UNREACHABLE_HINTS = ("network is unreachable", "no route to host")


def _connect_detail(e: httpx.ConnectError) -> str:
    """Extract a diagnostic detail from a ConnectError for error messages."""
    msg = str(e)
    lower = msg.lower()
    if any(h in lower for h in _DNS_HINTS):
        return "DNS resolution failed"
    if any(h in lower for h in _REFUSED_HINTS):
        return "connection refused (service may be down)"
    if any(h in lower for h in _UNREACHABLE_HINTS):
        return "network unreachable"
    # Fallback: include raw message (capped)
    return msg[:200] if msg else "unknown connection error"


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
        except httpx.DecodingError:
            return error(
                "Log file decompression failed (corrupted gzip). "
                "Use diagnose_build which reads job-output.json (usually not corrupted) "
                "and falls back to text grep automatically."
            )
        except httpx.ConnectError as e:
            if is_ssl_error(e):
                return error(
                    "SSL certificate verification failed. "
                    "If using self-signed certificates, set ZUUL_VERIFY_SSL=false"
                )
            return error(f"Cannot connect to Zuul API: {_connect_detail(e)}")
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
