"""Shared constants, annotations, and helpers used across tool sub-modules."""

import gzip
import json
import logging
import re
import zlib

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from ..helpers import app, error, fetch_log_url, parse_zuul_url
from ..helpers import tenant as _tenant

# Re-export parsers for backward compat (tests import from mcp_zuul.tools)
from ..parsers import (  # noqa: F401
    _extract_inner_recap,
    _grep_log_context,
    _parse_playbooks,
    _smart_truncate,
    parse_playbooks,
)

log = logging.getLogger("zuul-mcp")

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)


def _resolve(
    ctx: Context, uuid: str, tenant: str, url: str, kind: str = "build"
) -> tuple[str, str]:
    """Resolve resource ID and tenant from explicit params or Zuul URL."""
    if url:
        parts = parse_zuul_url(url)
        if not parts:
            raise ValueError(f"Cannot parse Zuul URL: {url}")
        url_tenant, url_kind, url_id = parts
        if url_kind != kind:
            raise ValueError(f"Expected {kind} URL, got {url_kind}")
        return url_id, _tenant(ctx, tenant or url_tenant)
    if not uuid:
        raise ValueError(f"{kind} identifier or url is required")
    return uuid, _tenant(ctx, tenant)


# Log fetching constants
_MAX_LOG_LINES = 200
_MAX_JSON_LOG_BYTES = 20 * 1024 * 1024  # 20 MB (JSON is larger)
_MAX_FILE_BYTES = 512 * 1024  # 512 KB for fetched log files
_ERROR_PATTERNS = re.compile(
    r"(FAILED!|UNREACHABLE|fatal:|Traceback|failed=[1-9])",
)
_ERROR_NOISE = re.compile(r"failed=0|RETRYING:")
_RUN_END_MARKER = re.compile(r"\| RUN END RESULT_")


def _no_log_url_error(build: dict, uuid: str) -> str:
    """Return a helpful error when a build has no log_url yet."""
    result = build.get("result")
    if not result or result == "IN_PROGRESS":
        return error(
            f"Build {uuid} is still in progress (post-run phase) — "
            "logs not yet available. Use get_change_status for live progress "
            "or wait for the build to complete."
        )
    return error(
        f"No log_url for build {uuid} (result: {result}). "
        "Logs may have been lost or the build was aborted before log upload."
    )


async def _fetch_job_output(ctx: Context, log_url: str) -> tuple[list[dict], list[dict], bool]:
    """Fetch and parse job-output.json with gz/json fallback.

    Shared by get_build_failures, diagnose_build, and prompts.
    Returns (playbooks, failed_tasks, json_ok).
    """
    a = app(ctx)
    playbooks: list[dict] = []
    failed_tasks: list[dict] = []
    for suffix in ("job-output.json.gz", "job-output.json"):
        try:
            resp = await fetch_log_url(a, log_url.rstrip("/") + "/" + suffix)
            if resp.status_code != 200:
                continue
            # Skip JSON parsing if content hit the streaming size cap -
            # truncated JSON will always fail to parse.
            if len(resp.content) >= _MAX_JSON_LOG_BYTES:
                log.info(
                    "job-output.json truncated at %d bytes, falling back to text",
                    len(resp.content),
                )
                continue
            content = resp.content
            # Manual gzip decompression: some log servers return raw gzip
            # bytes without Content-Encoding header, so httpx doesn't
            # auto-decompress.  Detect via gzip magic bytes (0x1f 0x8b).
            # Use incremental decompression with a size cap to prevent
            # gzip bombs (small compressed payload → huge decompressed output).
            if suffix.endswith(".gz") and content[:2] == b"\x1f\x8b":
                d = zlib.decompressobj(wbits=31)  # 31 = gzip format
                content = d.decompress(content, _MAX_JSON_LOG_BYTES + 1)
                if len(content) > _MAX_JSON_LOG_BYTES:
                    log.info(
                        "gzip decompressed output exceeds %d bytes, skipping",
                        _MAX_JSON_LOG_BYTES,
                    )
                    continue
            data = json.loads(content)
            if isinstance(data, list):
                playbooks, failed_tasks = parse_playbooks(data)
                return playbooks, failed_tasks, True
        except (
            httpx.DecodingError,
            json.JSONDecodeError,
            KeyError,
            UnicodeDecodeError,
            gzip.BadGzipFile,
            zlib.error,
            EOFError,
            OSError,
        ):
            continue
    return playbooks, failed_tasks, False
