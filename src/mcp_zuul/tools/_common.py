"""Shared constants, annotations, and helpers used across tool sub-modules."""

import json
import logging
import re
import zlib

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from ..helpers import app, error, fetch_log_url, parse_iso_timestamp, parse_zuul_url
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

_MAX_DECOMPRESS_BYTES = 10 * 1024 * 1024  # 10 MB cap for decompressed text logs


def _decompress_gzip(data: bytes, max_bytes: int = _MAX_DECOMPRESS_BYTES) -> tuple[bytes, bool]:
    """Decompress gzip data if detected via magic bytes (0x1f 0x8b).

    Returns (data, extra_truncated). Non-gzip data is returned unchanged.
    Decompression is capped at max_bytes to prevent gzip bombs.

    Raises ValueError on corrupted gzip so callers get a clear error.
    """
    if len(data) < 2 or data[:2] != b"\x1f\x8b":
        return data, False
    try:
        d = zlib.decompressobj(wbits=31)
        decompressed = d.decompress(data, max_bytes + 1)
        extra_truncated = len(decompressed) > max_bytes
        if extra_truncated:
            decompressed = decompressed[:max_bytes]
        return decompressed, extra_truncated
    except (zlib.error, EOFError, OSError) as e:
        raise ValueError(f"Failed to decompress gzipped log: {e}") from e


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
        detail = build.get("error_detail")
        msg = (
            f"Build {uuid} is still in progress — "
            "logs not yet available (uploaded after post-run completes)."
        )
        if detail:
            msg += f" Error detail: {detail}"
        msg += " Use get_change_status for live progress or wait for the build to complete."
        return error(msg)
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
            # Decompress file-level gzip (some log servers return raw gzip
            # bytes without Content-Encoding header, so httpx doesn't
            # auto-decompress). _decompress_gzip detects via magic bytes
            # and caps decompressed size. ValueError = corrupt gzip.
            if suffix.endswith(".gz"):
                try:
                    content, gz_truncated = _decompress_gzip(content, _MAX_JSON_LOG_BYTES)
                except ValueError:
                    log.info("Corrupted file-level gzip, trying next suffix")
                    continue
                if gz_truncated:
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
            UnicodeDecodeError,
        ):
            continue
    return playbooks, failed_tasks, False


class TimeFilters:
    """Parsed time filter parameters for list_builds / list_buildsets."""

    __slots__ = ("active", "completed_after", "completed_before", "started_after", "started_before")

    def __init__(
        self,
        completed_after: str = "",
        completed_before: str = "",
        started_after: str = "",
        started_before: str = "",
    ) -> None:
        self.completed_after = parse_iso_timestamp(completed_after) if completed_after else None
        self.completed_before = parse_iso_timestamp(completed_before) if completed_before else None
        self.started_after = parse_iso_timestamp(started_after) if started_after else None
        self.started_before = parse_iso_timestamp(started_before) if started_before else None
        self.active = any(
            (self.completed_after, self.completed_before, self.started_after, self.started_before)
        )

    def fetch_limit(self, user_limit: int) -> int:
        """API fetch limit: overfetch 3x (capped at 300) when filters are active."""
        if not self.active:
            return user_limit
        return min(user_limit * 3, 300)


def _apply_time_filters(
    items: list[dict],
    tf: TimeFilters,
    end_field: str = "end_time",
    start_field: str = "start_time",
) -> list[dict]:
    """Filter items by parsed time boundaries.

    Items missing the relevant timestamp field pass through (not excluded).
    """
    if not tf.active:
        return items
    filtered = []
    for item in items:
        end_time = parse_iso_timestamp(item.get(end_field) or "")
        start_time = parse_iso_timestamp(item.get(start_field) or "")
        if tf.completed_after and end_time and end_time < tf.completed_after:
            continue
        if tf.completed_before and end_time and end_time > tf.completed_before:
            continue
        if tf.started_after and start_time and start_time < tf.started_after:
            continue
        if tf.started_before and start_time and start_time > tf.started_before:
            continue
        filtered.append(item)
    return filtered
